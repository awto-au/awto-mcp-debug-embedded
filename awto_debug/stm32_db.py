"""STM32 device database — wraps STM32CubeProgrammer's per-device XML files.

Cube ships ``Data_Base/STM32_Prog_DB_0x<DEVID>.xml`` for every supported
device. Each file contains the device name, series, CPU core, and crucially
the on-chip ``F_SIZE`` register address + a default flash size. Parsing this
gives us a runtime lookup keyed by Cortex-M ``IDCODE`` (DBGMCU_IDCODE) without
us hardcoding a family table that drifts as new parts ship.

Usage::

    info = lookup_device(0x450)        # → {"name": "STM32H7xx", "fsize_addr": 0x1FF1E880, ...}
    addr = uid_address("STM32H7")      # → 0x1FF1E800
"""

from __future__ import annotations

import os
import re
import threading
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Database location
# ---------------------------------------------------------------------------

# Search roots in priority order. The cube programmer Data_Base lives under
# stm32cubeide's externaltools plugin or the standalone STM32CubeProgrammer.
_SEARCH_ROOTS = (
    "/opt/st",
    str(Path.home() / ".local/share/stm32cube"),
    "/usr/local/STMicroelectronics",
    "/Applications/STMicroelectronics",  # macOS
    "C:\\Program Files\\STMicroelectronics",  # Windows
    "C:\\Program Files (x86)\\STMicroelectronics",
)

_DB_DIR_OVERRIDE = os.environ.get("STM32_PROG_DB_DIR")
_DB_DIR_CACHE: Optional[Path] = None
_DB_LOCK = threading.Lock()
_DEVICE_CACHE: dict[int, Optional[dict]] = {}


def _find_db_dir() -> Optional[Path]:
    """Locate the cube programmer Data_Base/ directory."""
    global _DB_DIR_CACHE
    with _DB_LOCK:
        if _DB_DIR_CACHE is not None:
            return _DB_DIR_CACHE if _DB_DIR_CACHE.exists() else None
        if _DB_DIR_OVERRIDE:
            p = Path(_DB_DIR_OVERRIDE)
            if p.is_dir():
                _DB_DIR_CACHE = p
                return p
        for root in _SEARCH_ROOTS:
            base = Path(root)
            if not base.exists():
                continue
            # Glob a few levels deep for any Data_Base dir holding a STM32_Prog_DB file
            try:
                for cand in base.glob("**/Data_Base"):
                    if (cand / "STM32_Prog_DB_0x450.xml").exists() or any(
                        cand.glob("STM32_Prog_DB_0x*.xml")
                    ):
                        _DB_DIR_CACHE = cand
                        return cand
            except OSError:
                continue
        _DB_DIR_CACHE = None
        return None


def db_dir() -> Optional[Path]:
    """Public accessor for the resolved cube DB directory (or None)."""
    return _find_db_dir()


# ---------------------------------------------------------------------------
# Family-specific UID base addresses (96-bit Unique Device ID)
# ---------------------------------------------------------------------------
# Keyed by series prefix — ordered most-specific first so longest match wins.
_UID_ADDRS: tuple[tuple[str, int], ...] = (
    ("STM32H7", 0x1FF1E800),
    ("STM32F0", 0x1FFFF7AC),
    ("STM32F1", 0x1FFFF7E8),
    ("STM32F2", 0x1FFF7A10),
    ("STM32F3", 0x1FFFF7AC),
    ("STM32F4", 0x1FFF7A10),
    ("STM32F7", 0x1FF0F420),
    ("STM32G0", 0x1FFF7590),
    ("STM32G4", 0x1FFF7590),
    ("STM32L0", 0x1FF80050),
    ("STM32L1", 0x1FF80050),
    ("STM32L4", 0x1FFF7590),
    ("STM32L5", 0x0BFA0590),
    ("STM32U5", 0x0BFA0700),
    ("STM32WB", 0x1FFF7590),
    ("STM32WL", 0x1FFF7590),
)


def uid_address(series_or_name: str) -> Optional[int]:
    """Return the 96-bit UID base address for an STM32 series. None if unknown."""
    s = (series_or_name or "").upper()
    for prefix, addr in _UID_ADDRS:
        if s.startswith(prefix):
            return addr
    return None


# ---------------------------------------------------------------------------
# Device lookup
# ---------------------------------------------------------------------------

def _parse_device_xml(path: Path) -> dict:
    """Extract the fields we care about from one STM32_Prog_DB_0xNNN.xml file."""
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise RuntimeError(f"failed to parse {path}: {exc}") from exc

    dev = root.find("Device")
    if dev is None:
        raise RuntimeError(f"no <Device> element in {path}")

    def _text(tag: str) -> str:
        el = dev.find(tag)
        return (el.text or "").strip() if el is not None and el.text else ""

    info: dict = {
        "device_id": int(_text("DeviceID") or "0", 16) if _text("DeviceID") else None,
        "name":      _text("Name"),
        "series":    _text("Series"),
        "cpu":       _text("CPU"),
        "vendor":    _text("Vendor"),
        "type":      _text("Type"),
        "db_path":   str(path),
    }

    # Find the Embedded Flash peripheral and pull F_SIZE address + variants.
    fsize_addr: Optional[int] = None
    fsize_default: Optional[int] = None
    bootloader_addr: Optional[int] = None
    flash_variants: list[dict] = []
    flash_base: Optional[int] = None
    for periph in dev.iterfind(".//Peripheral"):
        name_el = periph.find("Name")
        if name_el is None or (name_el.text or "").strip() != "Embedded Flash":
            continue
        fsize_el = periph.find("FlashSize")
        if fsize_el is not None:
            try:
                fsize_addr = int(fsize_el.get("address", "0"), 16) or None
            except ValueError:
                fsize_addr = None
            try:
                fsize_default = int(fsize_el.get("default", "0"), 16) or None
            except ValueError:
                fsize_default = None
        bl_el = periph.find("BootloaderVersion")
        if bl_el is not None:
            try:
                bootloader_addr = int(bl_el.get("address", "0"), 16) or None
            except ValueError:
                bootloader_addr = None
        # Parameters live inside <Configuration> children — recurse.
        seen_variants: set[tuple[int, int, str]] = set()
        for params in periph.iter("Parameters"):
            try:
                addr = int(params.get("address", "0"), 16)
                size = int(params.get("size", "0"), 16)
            except ValueError:
                continue
            name = params.get("name", "")
            key = (addr, size, name)
            if key in seen_variants:
                continue
            seen_variants.add(key)
            if flash_base is None and addr:
                flash_base = addr
            flash_variants.append({
                "name":    name,
                "address": addr,
                "size":    size,
                "size_kb": size // 1024,
            })
        break

    info["fsize_address"] = fsize_addr
    info["fsize_default_bytes"] = fsize_default
    info["bootloader_address"] = bootloader_addr
    info["flash_base"] = flash_base
    info["flash_variants"] = flash_variants
    info["uid_address"] = uid_address(info["series"]) or uid_address(info["name"])
    return info


_DEVID_FILE_RE = re.compile(r"STM32_Prog_DB_0x([0-9A-Fa-f]+)\.xml$")


def lookup_device(device_id: int) -> Optional[dict]:
    """Return parsed device info for the given DBGMCU_IDCODE, or None if unknown."""
    if device_id in _DEVICE_CACHE:
        return _DEVICE_CACHE[device_id]
    db = _find_db_dir()
    if db is None:
        _DEVICE_CACHE[device_id] = None
        return None
    # Cube uses 3-hex-digit padding (0x450, 0x01E). Try both.
    candidates = (
        db / f"STM32_Prog_DB_0x{device_id:03X}.xml",
        db / f"STM32_Prog_DB_0x{device_id:03x}.xml",
    )
    for cand in candidates:
        if cand.exists():
            try:
                info = _parse_device_xml(cand)
            except RuntimeError:
                _DEVICE_CACHE[device_id] = None
                return None
            _DEVICE_CACHE[device_id] = info
            return info
    _DEVICE_CACHE[device_id] = None
    return None


def list_known_device_ids() -> list[int]:
    """Return all device IDs the local cube DB has files for."""
    db = _find_db_dir()
    if db is None:
        return []
    out: list[int] = []
    for f in db.glob("STM32_Prog_DB_0x*.xml"):
        m = _DEVID_FILE_RE.search(f.name)
        if m:
            try:
                out.append(int(m.group(1), 16))
            except ValueError:
                pass
    return sorted(out)
