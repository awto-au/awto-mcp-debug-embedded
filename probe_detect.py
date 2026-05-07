"""
probe_detect.py — USB probe enumeration, probes.json registry, and continuous monitor.

Registry flow (mirrors awto-mcp-riden approval pattern + stlink-toolkit probes.json):

  1. Background ProbeMonitor thread polls USB every POLL_INTERVAL_S seconds.
  2. Newly seen ST-Link probes are added to the registry with state="pending".
  3. ESP32 serial adapters (CP2102, CH340, etc.) are registered similarly.
  4. MCP tools probe_list() / probe_approve() / probe_ignore() let Copilot
     surface the approval flow to the user.
  5. Approved probes gain state="approved"; ignored probes gain state="ignored"
     and are not surfaced again unless the registry is cleared.
  6. probe_approve() persists the user's chosen nick and state to probes.json.

probes.json schema:
  {
    "probes": [
      {
        "serial":    "004D003...",          -- ST-Link serial or COM port ID
        "kind":      "stlink" | "esp",      -- probe family
        "model":     "ST-LINK/V3",          -- human-readable model
        "nick":      "myboard",             -- user-chosen nickname (optional)
        "usb_vid":   1155,
        "usb_pid":   14158,
        "state":     "pending"|"approved"|"ignored"
      }
    ]
  }
"""

from __future__ import annotations

import grp
import json
import logging
import os
import shutil
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("awto.probe")

# ---------------------------------------------------------------------------
# ST-Link USB VID/PID table  (mirrors stlink-toolkit/stlink_toolkit/usb.py)
# ---------------------------------------------------------------------------

STLINK_VID = 0x0483
STLINK_PID_TYPES: dict[int, str] = {
    0x3744: "ST-LINK/V2",
    0x3748: "ST-LINK/V2",
    0x374A: "ST-LINK/V2-1",
    0x374B: "ST-LINK/V2-1",
    0x374D: "ST-LINK/V3 (loader)",
    0x374E: "ST-LINK/V3E",
    0x374F: "ST-LINK/V3S",
    0x3752: "ST-LINK/V3-MNIE",
    0x3753: "ST-LINK/V3",
    0x3754: "ST-LINK/V3",
    0x3755: "ST-LINK/V3 (loader)",
    0x3757: "ST-LINK/V3MODS",
    0x3762: "ST-LINK/V3-HLADAPTER",
}

# Known udev rules files that grant access to ST-Link devices
STLINK_UDEV_RULES_PATHS = [
    "/etc/udev/rules.d/60-awto-stlink.rules",
    "/etc/udev/rules.d/49-stlinkv3.rules",
    "/etc/udev/rules.d/49-stlinkv2-1.rules",
    "/etc/udev/rules.d/49-stlinkv2.rules",
    "/lib/udev/rules.d/60-openocd.rules",
    "/usr/lib/udev/rules.d/60-openocd.rules",
    "/etc/udev/rules.d/70-st-link.rules",
]

# ESP32 serial adapter VID/PID table
ESP_ADAPTERS: dict[tuple[int, int], str] = {
    (0x10C4, 0xEA60): "CP2102/CP2104",
    (0x10C4, 0xEA70): "CP2105",
    (0x1A86, 0x7523): "CH340",
    (0x1A86, 0x55D4): "CH9102",
    (0x0403, 0x6001): "FT232R",
    (0x0403, 0x6010): "FT2232H",
    (0x0403, 0x6011): "FT4232H",
    (0x0403, 0x6014): "FT232H",
    (0x303A, 0x1001): "ESP32-S3 USB-OTG",
}

POLL_INTERVAL_S: float = 3.0


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ProbeInfo:
    serial: str
    kind: str          # "stlink" | "esp"
    model: str
    nick: str          # user-chosen, default ""
    usb_vid: int
    usb_pid: int
    state: str         # "pending" | "approved" | "ignored"
    port: str = ""     # serial port path for ESP adapters


# ---------------------------------------------------------------------------
# Registry — probes.json on disk
# ---------------------------------------------------------------------------

_REGISTRY_PATH: Path = Path("probes.json")
_registry_lock = threading.Lock()


def configure_registry(path: str | Path) -> None:
    """Override default registry file path. Call before starting ProbeMonitor."""
    global _REGISTRY_PATH
    _REGISTRY_PATH = Path(path)


def _load_registry() -> dict[str, Any]:
    with _registry_lock:
        if _REGISTRY_PATH.exists():
            try:
                return json.loads(_REGISTRY_PATH.read_text())
            except Exception as exc:
                log.warning("probes.json parse error: %s", exc)
        return {"probes": []}


def _save_registry(reg: dict[str, Any]) -> None:
    with _registry_lock:
        try:
            _REGISTRY_PATH.write_text(json.dumps(reg, indent=2) + "\n")
        except OSError as exc:
            log.error("Could not write probes.json: %s", exc)


def _registry_get_probe(serial: str) -> Optional[dict[str, Any]]:
    reg = _load_registry()
    for p in reg.get("probes", []):
        if p.get("serial") == serial:
            return p
    return None


def _registry_upsert_probe(info: ProbeInfo) -> bool:
    """Insert or update probe. Returns True if registry was changed."""
    reg = _load_registry()
    probes: list[dict] = reg.setdefault("probes", [])
    for p in probes:
        if p.get("serial") == info.serial:
            changed = False
            # Update mutable fields but never overwrite user choices (nick, state)
            for key in ("kind", "model", "usb_vid", "usb_pid"):
                if p.get(key) != getattr(info, key):
                    p[key] = getattr(info, key)
                    changed = True
            if info.port and p.get("port") != info.port:
                p["port"] = info.port
                changed = True
            if changed:
                _save_registry(reg)
            return changed
    # New probe — add as pending
    probes.append(asdict(info))
    _save_registry(reg)
    log.info("New probe registered as pending: %s (%s)", info.serial[-8:], info.model)
    return True


def get_all_probes() -> list[ProbeInfo]:
    """Return all probes from the registry."""
    reg = _load_registry()
    result = []
    for p in reg.get("probes", []):
        try:
            result.append(ProbeInfo(**{k: p.get(k, "") for k in ProbeInfo.__dataclass_fields__}))
        except Exception as exc:
            log.warning("Skipping malformed probe entry: %s", exc)
    return result


def approve_probe(serial: str, nick: str = "") -> Optional[ProbeInfo]:
    """Mark a probe approved (and optionally set a nickname). Returns updated ProbeInfo."""
    reg = _load_registry()
    for p in reg.get("probes", []):
        if p.get("serial") == serial:
            p["state"] = "approved"
            if nick:
                p["nick"] = nick
            _save_registry(reg)
            log.info("Probe approved: %s nick=%r", serial[-8:], p.get("nick", ""))
            return ProbeInfo(**{k: p.get(k, "") for k in ProbeInfo.__dataclass_fields__})
    return None


def ignore_probe(serial: str) -> bool:
    """Mark a probe ignored. Returns True if found."""
    reg = _load_registry()
    for p in reg.get("probes", []):
        if p.get("serial") == serial:
            p["state"] = "ignored"
            _save_registry(reg)
            log.info("Probe ignored: %s", serial[-8:])
            return True
    return False


def rename_probe(serial: str, nick: str) -> bool:
    """Rename an existing probe. Returns True if found."""
    reg = _load_registry()
    for p in reg.get("probes", []):
        if p.get("serial") == serial:
            p["nick"] = nick
            _save_registry(reg)
            return True
    return False


def clear_probe(serial: str) -> bool:
    """Remove a probe from the registry entirely (allows re-discovery)."""
    reg = _load_registry()
    before = len(reg.get("probes", []))
    reg["probes"] = [p for p in reg.get("probes", []) if p.get("serial") != serial]
    if len(reg["probes"]) < before:
        _save_registry(reg)
        return True
    return False


# ---------------------------------------------------------------------------
# Live USB enumeration
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Sysfs serial resolution — reads serial without opening the USB device
# ---------------------------------------------------------------------------

_sysfs_serial_cache: dict[tuple[int, int], str] = {}
_sysfs_cache_lock = threading.Lock()


def _build_sysfs_serial_map() -> dict[tuple[int, int], str]:
    """
    Build a (bus, devnum) → serial mapping by reading /sys/bus/usb/devices.
    No special permissions required — serial is a sysfs attribute.
    """
    result: dict[tuple[int, int], str] = {}
    try:
        sysfs_root = Path("/sys/bus/usb/devices")
        for entry in sysfs_root.iterdir():
            try:
                serial_path = entry / "serial"
                if not serial_path.exists():
                    continue
                bus = int((entry / "busnum").read_text().strip())
                dev = int((entry / "devnum").read_text().strip())
                serial = serial_path.read_text().strip()
                if serial:
                    result[(bus, dev)] = serial
            except (OSError, ValueError):
                continue
    except OSError:
        pass
    return result


def _serial_from_sysfs(bus: int, address: int) -> str:
    """Return USB serial number for bus:address from sysfs (no device open needed)."""
    with _sysfs_cache_lock:
        if not _sysfs_serial_cache:
            _sysfs_serial_cache.update(_build_sysfs_serial_map())
        return _sysfs_serial_cache.get((bus, address), "")


def _refresh_sysfs_cache() -> None:
    """Refresh the sysfs serial cache (call after USB hotplug events)."""
    with _sysfs_cache_lock:
        _sysfs_serial_cache.clear()
        _sysfs_serial_cache.update(_build_sysfs_serial_map())


def _enumerate_stlink_probes() -> list[ProbeInfo]:
    """Return list of currently-attached ST-Link probes via pyusb."""
    try:
        import usb.core
        import usb.util
    except ImportError:
        log.warning("pyusb not available — ST-Link enumeration disabled")
        return []

    results: list[ProbeInfo] = []
    devices = usb.core.find(idVendor=STLINK_VID, find_all=True) or []
    for dev in devices:
        pid = dev.idProduct
        if pid not in STLINK_PID_TYPES:
            continue
        model = STLINK_PID_TYPES[pid]
        serial = ""
        try:
            serial = usb.util.get_string(dev, dev.iSerialNumber) or ""
        except Exception:
            pass
        if not serial:
            # Fall back to sysfs (no device-open permission needed)
            serial = _serial_from_sysfs(dev.bus, dev.address)
        if not serial:
            serial = f"{dev.bus:03d}:{dev.address:03d}"
        results.append(ProbeInfo(
            serial=serial,
            kind="stlink",
            model=model,
            nick="",
            usb_vid=STLINK_VID,
            usb_pid=pid,
            state="pending",
            port="",
        ))
    return results


def _enumerate_esp_adapters() -> list[ProbeInfo]:
    """Return list of attached ESP serial adapters via pyusb + pyserial."""
    results: list[ProbeInfo] = []

    # pyusb pass — identify VID/PID
    try:
        import usb.core
        import usb.util
        for (vid, pid), model_name in ESP_ADAPTERS.items():
            devices = usb.core.find(idVendor=vid, idProduct=pid, find_all=True) or []
            for dev in devices:
                serial = ""
                try:
                    serial = usb.util.get_string(dev, dev.iSerialNumber) or ""
                except Exception:
                    pass
                if not serial:
                    serial = _serial_from_sysfs(dev.bus, dev.address)
                if not serial:
                    serial = f"{dev.bus:03d}:{dev.address:03d}"
                results.append(ProbeInfo(
                    serial=serial,
                    kind="esp",
                    model=model_name,
                    nick="",
                    usb_vid=vid,
                    usb_pid=pid,
                    state="pending",
                    port="",
                ))
    except ImportError:
        pass

    # pyserial pass — resolve /dev/tty* paths and associate
    try:
        from serial.tools.list_ports import comports
        for port_info in comports():
            vid = getattr(port_info, "vid", None)
            pid = getattr(port_info, "pid", None)
            if (vid, pid) not in ESP_ADAPTERS:
                continue
            device = port_info.device
            hwid = port_info.hwid or ""
            # match by VID:PID in hwid to any already-added entry
            matched = False
            for p in results:
                if p.usb_vid == vid and p.usb_pid == pid and not p.port:
                    p.port = device
                    matched = True
                    break
            if not matched:
                model_name = ESP_ADAPTERS.get((vid, pid), f"USB {vid:04x}:{pid:04x}")
                serial = port_info.serial_number or hwid or device
                results.append(ProbeInfo(
                    serial=serial,
                    kind="esp",
                    model=model_name,
                    nick="",
                    usb_vid=vid or 0,
                    usb_pid=pid or 0,
                    state="pending",
                    port=device,
                ))
    except ImportError:
        pass

    return results


def enumerate_all_probes() -> list[ProbeInfo]:
    """Enumerate all currently-attached probes (ST-Link + ESP adapters)."""
    _refresh_sysfs_cache()  # Rebuild serial map before each full scan
    return _enumerate_stlink_probes() + _enumerate_esp_adapters()


# ---------------------------------------------------------------------------
# Backend availability
# ---------------------------------------------------------------------------

@dataclass
class BackendStatus:
    st_flash: bool = False
    st_info: bool = False
    st_util: bool = False
    st_trace: bool = False
    cube: bool = False          # cube CLI (Cube IDE helper)
    stm32_programmer: bool = False  # standalone STM32_Programmer_CLI
    esptool: bool = False
    idf: bool = False
    openocd: bool = False
    arm_gdb: bool = False       # arm-none-eabi-gdb
    gdb_multiarch: bool = False


def check_backends() -> BackendStatus:
    """Return which debugger CLIs are installed on this machine."""
    def has(cmd: str) -> bool:
        return shutil.which(cmd) is not None

    return BackendStatus(
        st_flash=has("st-flash"),
        st_info=has("st-info"),
        st_util=has("st-util"),
        st_trace=has("st-trace"),
        cube=has("cube"),
        stm32_programmer=has("STM32_Programmer_CLI"),
        esptool=has("esptool.py") or has("esptool"),
        idf=has("idf.py"),
        openocd=has("openocd"),
        arm_gdb=has("arm-none-eabi-gdb"),
        gdb_multiarch=has("gdb-multiarch"),
    )


# ---------------------------------------------------------------------------
# USB permission diagnostics
# ---------------------------------------------------------------------------

@dataclass
class UsbPermissions:
    udev_rules_files: list[str]        # rule files found that cover 0483 VID
    user_groups: list[str]             # current user's supplementary groups
    in_plugdev: bool
    in_dialout: bool
    stlink_devices_accessible: list[str]  # /dev/bus/usb paths that are readable
    stlink_devices_blocked: list[str]     # paths that are NOT readable
    ok: bool
    issues: list[str]
    fix_hint: str


def check_usb_permissions() -> UsbPermissions:
    """
    Diagnose whether the current user has permission to open ST-Link USB devices.

    Checks:
    - udev rules files covering VID 0483 (ST-Link)
    - group membership (plugdev, dialout)
    - direct /dev/bus/usb read access for known ST-Link devices

    The standard fix on Linux is to install udev rules from:
    https://github.com/stlink-org/stlink/tree/master/config/udev/rules.d
    or use the awto-stlink installer script.
    """
    issues: list[str] = []

    # --- udev rules scan ---
    rules_found: list[str] = []
    for rules_path in STLINK_UDEV_RULES_PATHS:
        p = Path(rules_path)
        if p.exists():
            try:
                content = p.read_text(errors="replace")
                if "0483" in content:
                    rules_found.append(str(p))
            except OSError:
                pass
    # Also scan rules.d directories for any file mentioning 0483
    for rules_dir in ("/etc/udev/rules.d", "/lib/udev/rules.d", "/usr/lib/udev/rules.d"):
        try:
            for rf in sorted(Path(rules_dir).iterdir()):
                if rf.suffix not in (".rules",) or str(rf) in rules_found:
                    continue
                try:
                    if "0483" in rf.read_text(errors="replace"):
                        rules_found.append(str(rf))
                except OSError:
                    pass
        except OSError:
            pass

    if not rules_found:
        issues.append(
            "No udev rules found for VID 0483 (ST-Link). "
            "Install rules from https://github.com/stlink-org/stlink/tree/master/config/udev/rules.d "
            "then run: sudo udevadm control --reload-rules && sudo udevadm trigger"
        )

    # --- group membership ---
    try:
        user_gids = os.getgroups()
        user_groups = [grp.getgrgid(g).gr_name for g in user_gids]
    except Exception:
        user_groups = []

    in_plugdev = "plugdev" in user_groups
    in_dialout = "dialout" in user_groups

    if not in_plugdev:
        issues.append(
            "User is not in the 'plugdev' group. "
            "Run: sudo usermod -aG plugdev $USER  then log out and back in."
        )

    # --- direct device access ---
    accessible: list[str] = []
    blocked: list[str] = []
    sysfs_map = _build_sysfs_serial_map()  # (bus, dev) → serial
    # Invert to get bus/dev for known ST-Link serials
    try:
        import usb.core
        stlink_devs = usb.core.find(idVendor=STLINK_VID, find_all=True) or []
        for dev in stlink_devs:
            dev_path = f"/dev/bus/usb/{dev.bus:03d}/{dev.address:03d}"
            if os.access(dev_path, os.R_OK):
                accessible.append(dev_path)
            else:
                blocked.append(dev_path)
                issues.append(f"Cannot read {dev_path} — udev rules may not have applied (replug the device?)")
    except Exception:
        pass

    fix_hint = (
        "Standard fix: \n"
        "  1. Install udev rules:\n"
        "       sudo cp stlink-udev-rules/*.rules /etc/udev/rules.d/\n"
        "       sudo udevadm control --reload-rules && sudo udevadm trigger\n"
        "  2. Add user to plugdev group:\n"
        "       sudo usermod -aG plugdev $USER\n"
        "  3. Log out and back in (or run: newgrp plugdev)\n"
        "  4. Replug all ST-Link devices\n"
        "\n"
        "Upstream rules: https://github.com/stlink-org/stlink/tree/master/config/udev/rules.d\n"
        "awto installer:  bash scripts/install-udev-rules.sh"
    ) if issues else "Permissions look correct."

    return UsbPermissions(
        udev_rules_files=rules_found,
        user_groups=user_groups,
        in_plugdev=in_plugdev,
        in_dialout=in_dialout,
        stlink_devices_accessible=accessible,
        stlink_devices_blocked=blocked,
        ok=not issues,
        issues=issues,
        fix_hint=fix_hint,
    )

    return BackendStatus(
        st_flash=has("st-flash"),
        st_info=has("st-info"),
        st_util=has("st-util"),
        st_trace=has("st-trace"),
        cube=has("cube"),
        stm32_programmer=has("STM32_Programmer_CLI"),
        esptool=has("esptool.py") or has("esptool"),
        idf=has("idf.py"),
        openocd=has("openocd"),
        arm_gdb=has("arm-none-eabi-gdb"),
        gdb_multiarch=has("gdb-multiarch"),
    )


# ---------------------------------------------------------------------------
# Continuous monitor — background thread
# ---------------------------------------------------------------------------

class ProbeMonitor:
    """
    Background thread that polls USB every POLL_INTERVAL_S seconds.

    - Newly seen probes are added to probes.json with state="pending".
    - Callbacks are fired for connect/disconnect events.
    - The MCP server registers on_connect / on_disconnect to update its
      in-memory state and surface the approval flow to the user.
    """

    def __init__(self, poll_interval: float = POLL_INTERVAL_S) -> None:
        self._poll_interval = poll_interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="probe-monitor")
        self._lock = threading.Lock()
        self._connected: dict[str, ProbeInfo] = {}  # serial → ProbeInfo
        self._on_connect: list = []     # callbacks(ProbeInfo)
        self._on_disconnect: list = []  # callbacks(ProbeInfo)
        self.ready = threading.Event()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def on_connect(self, cb) -> None:
        """Register callback fired when a new probe is detected."""
        self._on_connect.append(cb)

    def on_disconnect(self, cb) -> None:
        """Register callback fired when a probe is removed."""
        self._on_disconnect.append(cb)

    def connected_probes(self) -> list[ProbeInfo]:
        """Snapshot of currently-connected probes (live USB view)."""
        with self._lock:
            return list(self._connected.values())

    def _run(self) -> None:
        log.info("ProbeMonitor started (poll interval %.1fs)", self._poll_interval)
        first = True
        while not self._stop.is_set():
            try:
                self._poll(first)
            except Exception as exc:
                log.warning("ProbeMonitor poll error: %s", exc)
            if first:
                self.ready.set()
                first = False
            self._stop.wait(timeout=self._poll_interval)
        log.info("ProbeMonitor stopped")

    def _poll(self, initial: bool) -> None:
        current = {p.serial: p for p in enumerate_all_probes()}

        with self._lock:
            prev_serials = set(self._connected.keys())

        new_serials = set(current.keys())

        # Appeared
        for serial in new_serials - prev_serials:
            probe = current[serial]
            # Register in probes.json (inserts as pending if not seen before)
            existing = _registry_get_probe(serial)
            if existing is None:
                _registry_upsert_probe(probe)
                log.info("Probe connected (pending approval): %s %s", probe.model, serial[-8:])
            else:
                # Update live port info, keep existing state/nick
                probe.state = existing.get("state", "pending")
                probe.nick = existing.get("nick", "")
                _registry_upsert_probe(probe)
                log.info("Probe connected: %s %s [%s]", probe.model, serial[-8:], probe.state)

            with self._lock:
                self._connected[serial] = probe

            if not initial:
                for cb in self._on_connect:
                    try:
                        cb(probe)
                    except Exception as exc:
                        log.warning("on_connect callback error: %s", exc)

        # Disappeared
        for serial in prev_serials - new_serials:
            with self._lock:
                probe = self._connected.pop(serial, None)
            if probe:
                log.info("Probe disconnected: %s %s", probe.model, serial[-8:])
                for cb in self._on_disconnect:
                    try:
                        cb(probe)
                    except Exception as exc:
                        log.warning("on_disconnect callback error: %s", exc)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_monitor: Optional[ProbeMonitor] = None
_monitor_lock = threading.Lock()


def get_monitor() -> ProbeMonitor:
    """Return the global ProbeMonitor singleton, starting it if needed."""
    global _monitor
    with _monitor_lock:
        if _monitor is None:
            _monitor = ProbeMonitor()
            _monitor.start()
            _monitor.ready.wait(timeout=10)
    return _monitor


def stop_monitor() -> None:
    """Stop the global ProbeMonitor (call on server shutdown)."""
    global _monitor
    with _monitor_lock:
        if _monitor is not None:
            _monitor.stop()
            _monitor = None
