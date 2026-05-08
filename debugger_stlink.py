"""
debugger_stlink.py — subprocess wrappers for open-source stlink tools.

Wraps: st-info, st-flash, st-util, st-trace
All functions raise RuntimeError with a clean message on failure.

st-info  — probe enumeration and chip info
st-flash — read/write/erase/verify flash
st-util  — GDB server (SWD)
st-trace — SWO trace capture
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import shutil
from pathlib import Path
from typing import Any, Optional

from process_manager import ProcessHandle, get_manager

log = logging.getLogger("awto.stlink")

FLASH_BASE_DEFAULT = "0x08000000"
SRAM_BASE_DEFAULT = "0x20000000"

# Cache known target metadata keyed by ST-Link serial. This lets serial-targeted
# operations avoid re-running probe scans unless a required field is missing.
_TARGET_INFO_CACHE: dict[str, dict[str, Any]] = {}


def _normalize_probe_info(probe: dict[str, Any]) -> dict[str, Any]:
    info: dict[str, Any] = {
        "chip_id": probe.get("chip_id"),
        "device_name": probe.get("device_name"),
    }
    if "flash_size_bytes" in probe:
        info["flash_size_kb"] = int(probe["flash_size_bytes"]) // 1024
    if "ram_size_bytes" in probe:
        info["ram_size_kb"] = int(probe["ram_size_bytes"]) // 1024
    if "firmware" in probe:
        info["firmware"] = probe["firmware"]
    if "voltage" in probe:
        info["voltage"] = probe["voltage"]
    return {key: value for key, value in info.items() if value is not None}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require(cmd: str) -> str:
    """Return full path of *cmd* or raise RuntimeError."""
    path = shutil.which(cmd)
    if not path:
        raise RuntimeError(
            f"'{cmd}' not found on PATH. Install the open-source stlink package "
            "(e.g. 'dnf install stlink' or 'apt install stlink-tools')."
        )
    return path


def _run(cmd: list[str], timeout: int = 30) -> tuple[int, str, str]:
    """Run *cmd*, return (returncode, stdout, stderr). Never raises on non-zero exit."""
    log.debug("run: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Command timed out after {timeout}s: {' '.join(cmd)}")
    except FileNotFoundError:
        raise RuntimeError(f"Command not found: {cmd[0]}")


def _check(cmd: list[str], op: str, timeout: int = 30) -> str:
    """Run *cmd* and raise RuntimeError if it fails. Returns combined output."""
    rc, stdout, stderr = _run(cmd, timeout=timeout)
    combined = (stdout + stderr).strip()
    if rc != 0:
        raise RuntimeError(f"{op} failed (rc={rc}): {combined[:500]}")
    return combined


# ---------------------------------------------------------------------------
# st-info
# ---------------------------------------------------------------------------

def probe_list() -> list[dict[str, Any]]:
    """
    Return list of attached ST-Link probes from st-info --probe.

    Each dict: {serial, firmware, voltage, chip_id, flash_size_bytes,
    ram_size_bytes, device_name}
    Returns empty list if no probes found (not an error).
    """
    _require("st-info")
    rc, stdout, stderr = _run(["st-info", "--probe"])
    combined = stdout + stderr
    probes: list[dict[str, Any]] = []

    # st-info --probe outputs one block per probe
    # Typical line: Found 1 stlink programmers
    #   serial: 004D003...
    #   hla-serial: "\x00M\x00..."
    #   firmware-version: V3J63M24
    #   voltage: 3.24
    #   ...

    if "Found 0 stlink" in combined or rc != 0:
        return []

    current: dict[str, Any] = {}
    for line in combined.splitlines():
        line = line.strip()
        if m := re.match(r"serial:\s+(\S+)", line):
            if current:
                probes.append(current)
            current = {"serial": m.group(1)}
        elif m := re.match(r"firmware-version:\s+(\S+)", line):
            current["firmware"] = m.group(1)
        elif m := re.match(r"voltage:\s+([0-9.]+)", line):
            current["voltage"] = float(m.group(1))
        elif m := re.match(r"targetid:\s+(\S+)", line):
            current["target_id"] = m.group(1)
        elif m := re.match(r"idcode:\s+(\S+)", line):
            current.setdefault("target_id", m.group(1))
        elif m := re.match(r"description:\s+(.+)", line):
            current["target_description"] = m.group(1).strip()
        elif m := re.match(r"chipid:\s+(\S+)", line):
            current["chip_id"] = m.group(1)
        elif m := re.match(r"flash:\s+(\d+)", line):
            current["flash_size_bytes"] = int(m.group(1))
        elif m := re.match(r"sram:\s+(\d+)", line):
            current["ram_size_bytes"] = int(m.group(1))
        elif m := re.match(r"dev-type:\s+(.+)", line):
            current["device_name"] = m.group(1).strip()

    if current:
        probes.append(current)

    for probe in probes:
        serial = probe.get("serial")
        if serial:
            _TARGET_INFO_CACHE[serial] = _normalize_probe_info(probe)

    return probes


def chip_info(serial: Optional[str] = None) -> dict[str, Any]:
    """
    Return chip information for the connected target.

    Args:
        serial: ST-Link serial (optional if only one probe is attached).

    Returns dict with: chip_id, flash_size_kb, ram_size_kb, device_name, firmware, voltage
    """
    if serial and serial in _TARGET_INFO_CACHE:
        return dict(_TARGET_INFO_CACHE[serial])

    if serial is None and len(_TARGET_INFO_CACHE) == 1:
        return dict(next(iter(_TARGET_INFO_CACHE.values())))

    probes = probe_list()
    if serial:
        matches = [probe for probe in probes if probe.get("serial") == serial]
        if not matches:
            raise RuntimeError(f"No ST-Link probe matched serial {serial!r}.")
        probe = matches[0]
    else:
        if not probes:
            raise RuntimeError("Could not read chip info — no ST-Link probes detected.")
        if len(probes) > 1:
            raise RuntimeError("Multiple ST-Link probes detected; specify serial.")
        probe = probes[0]

    info = _normalize_probe_info(probe)
    if serial:
        _TARGET_INFO_CACHE[serial] = dict(info)
    return info


def dump_memory_snapshot(
    output_dir: str,
    serial: Optional[str] = None,
    flash_address: str = FLASH_BASE_DEFAULT,
    flash_length: Optional[int] = None,
    ram_address: str = SRAM_BASE_DEFAULT,
    ram_length: Optional[int] = None,
    include_flash: bool = True,
    include_ram: bool = True,
) -> dict[str, Any]:
    """
    Dump a target memory snapshot to files using st-flash.

    This covers one contiguous internal flash range and one contiguous SRAM
    range. It does not discover extra SRAM banks or external SPI/QSPI RAM.
    """
    if not include_flash and not include_ram:
        raise RuntimeError("Nothing to dump: enable include_flash and/or include_ram.")

    need_flash_size = include_flash and flash_length is None
    need_ram_size = include_ram and ram_length is None

    probe: dict[str, Any] = {}
    if serial and serial in _TARGET_INFO_CACHE:
        probe = dict(_TARGET_INFO_CACHE[serial])

    if need_flash_size or need_ram_size:
        # A scan is only needed when caller did not provide explicit lengths.
        probe = chip_info(serial)
    elif not probe and serial:
        probe = {"serial": serial}
    elif not probe and serial is None:
        probe = chip_info(serial)
    output_root = Path(output_dir)
    output_root.mkdir(parents=True, exist_ok=True)

    files: dict[str, str] = {}
    notes = [
        "RAM dump covers a single contiguous region starting at ram_address.",
        "External SPI/QSPI RAM is not included unless explicitly mapped into that range.",
    ]

    inferred_flash_length = flash_length
    if include_flash and inferred_flash_length is None:
        flash_kb = probe.get("flash_size_kb")
        if not flash_kb:
            raise RuntimeError("Could not infer flash size; pass flash_length explicitly.")
        inferred_flash_length = int(flash_kb) * 1024

    inferred_ram_length = ram_length
    if include_ram and inferred_ram_length is None:
        ram_kb = probe.get("ram_size_kb")
        if not ram_kb:
            raise RuntimeError("Could not infer RAM size; pass ram_length explicitly.")
        inferred_ram_length = int(ram_kb) * 1024

    if include_flash and inferred_flash_length:
        flash_path = output_root / "flash.bin"
        flash_read(str(flash_path), flash_address, inferred_flash_length, serial)
        files["flash"] = str(flash_path)

    if include_ram and inferred_ram_length:
        ram_path = output_root / "ram.bin"
        flash_read(str(ram_path), ram_address, inferred_ram_length, serial)
        files["ram"] = str(ram_path)

    metadata = {
        "serial": serial,
        "chip_info": probe,
        "flash_address": flash_address if include_flash else None,
        "flash_length": inferred_flash_length if include_flash else None,
        "ram_address": ram_address if include_ram else None,
        "ram_length": inferred_ram_length if include_ram else None,
        "files": files,
        "notes": notes,
    }
    metadata_path = output_root / "metadata.json"
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n")
    files["metadata"] = str(metadata_path)

    result = {
        "output_dir": str(output_root),
        "files": files,
        "chip_info": probe,
        "notes": notes,
    }
    if include_flash:
        result["flash_length"] = inferred_flash_length
    if include_ram:
        result["ram_length"] = inferred_ram_length
    return result


# ---------------------------------------------------------------------------
# st-flash
# ---------------------------------------------------------------------------

def flash_write(
    firmware_path: str,
    address: str = "0x8000000",
    serial: Optional[str] = None,
    format: str = "ihex",
    reset: bool = True,
) -> str:
    """
    Flash firmware to the target using st-flash.

    Args:
        firmware_path: Path to .bin or .hex file.
        address:       Flash start address (default 0x8000000). Ignored for ihex.
        serial:        ST-Link serial (optional).
        format:        'ihex' (auto-detect from extension) or 'binary'.
        reset:         Reset target after flash (default True).

    Returns: success message string.
    """
    _require("st-flash")
    if firmware_path.endswith(".hex"):
        fmt = "ihex"
    elif firmware_path.endswith(".bin"):
        fmt = "binary"
    else:
        fmt = format

    cmd = ["st-flash"]
    if serial:
        cmd += ["--serial", serial]
    if reset:
        cmd += ["--reset"]
    if fmt == "ihex":
        cmd += ["--format", "ihex", "write", firmware_path]
    else:
        cmd += ["write", firmware_path, address]

    out = _check(cmd, "st-flash write", timeout=120)
    log.info("st-flash write complete: %s", firmware_path)
    return f"Flash write OK: {firmware_path}\n{out[-200:].strip()}"


def flash_erase(serial: Optional[str] = None) -> str:
    """Mass-erase the target flash."""
    _require("st-flash")
    cmd = ["st-flash"]
    if serial:
        cmd += ["--serial", serial]
    cmd += ["erase"]
    out = _check(cmd, "st-flash erase", timeout=60)
    return f"Erase OK\n{out[-200:].strip()}"


def flash_read(
    output_path: str,
    address: str,
    length: int,
    serial: Optional[str] = None,
) -> str:
    """
    Read a region of flash to a binary file.

    Args:
        output_path: Destination file path.
        address:     Start address (e.g. '0x8000000').
        length:      Number of bytes to read.
        serial:      ST-Link serial (optional).
    """
    _require("st-flash")
    cmd = ["st-flash"]
    if serial:
        cmd += ["--serial", serial]
    cmd += ["read", output_path, address, str(length)]
    out = _check(cmd, "st-flash read", timeout=120)
    return f"Read OK → {output_path}\n{out[-200:].strip()}"


def flash_verify(firmware_path: str, serial: Optional[str] = None) -> str:
    """Verify flash contents against a firmware file (hex or bin).

    st-flash 1.8.0 requires the size argument for `verify` (same shape as
    `read`/`write`); omitting it produces 'invalid command line'. See issue #4.
    """
    _require("st-flash")
    fmt = "ihex" if firmware_path.endswith(".hex") else "binary"
    cmd = ["st-flash"]
    if serial:
        cmd += ["--serial", serial]
    if fmt == "ihex":
        cmd += ["--format", "ihex", "verify", firmware_path]
    else:
        size = os.path.getsize(firmware_path)
        cmd += ["verify", firmware_path, "0x8000000", hex(size)]
    out = _check(cmd, "st-flash verify", timeout=60)
    return f"Verify OK: {firmware_path}\n{out[-200:].strip()}"


def reset_target(serial: Optional[str] = None) -> str:
    """Reset the target MCU via ST-Link (st-flash reset)."""
    _require("st-flash")
    cmd = ["st-flash"]
    if serial:
        cmd += ["--serial", serial]
    cmd += ["reset"]
    out = _check(cmd, "st-flash reset", timeout=10)
    return f"Reset OK\n{out.strip()}"


# ---------------------------------------------------------------------------
# st-util — GDB server
# ---------------------------------------------------------------------------

def gdb_server_start(
    port: int = 4242,
    serial: Optional[str] = None,
    multi: bool = False,
) -> ProcessHandle:
    """
    Start an st-util GDB server.

    Args:
        port:   TCP port to listen on (default 4242).
        serial: ST-Link serial (optional).
        multi:  Multi-target mode (--multi).

    Returns: ProcessHandle. Use gdb_server_stop(handle) to stop.
    """
    _require("st-util")
    cmd = ["st-util", "--port", str(port)]
    if serial:
        cmd += ["--serial", serial]
    if multi:
        cmd += ["--multi"]
    tag = f"gdb-stutil-{port}"
    return get_manager().start(cmd, tag=tag, port=port, startup_wait_s=1.0)


def gdb_server_stop(handle_id: str) -> str:
    """Stop a running st-util GDB server by handle ID."""
    rc = get_manager().stop(handle_id)
    return f"st-util stopped (rc={rc})"


# ---------------------------------------------------------------------------
# st-trace — SWO trace
# ---------------------------------------------------------------------------

def trace_start(
    freq: int = 4000000,
    serial: Optional[str] = None,
) -> ProcessHandle:
    """
    Start st-trace SWO capture.

    Args:
        freq:   SWO clock frequency in Hz (default 4 MHz).
        serial: ST-Link serial (optional).
    """
    _require("st-trace")
    cmd = ["st-trace", "--clock", str(freq)]
    if serial:
        cmd += ["--serial", serial]
    return get_manager().start(cmd, tag="st-trace", startup_wait_s=1.0)


def trace_stop(handle_id: str) -> str:
    """Stop a running st-trace capture."""
    rc = get_manager().stop(handle_id)
    return f"st-trace stopped (rc={rc})"
