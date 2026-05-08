import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from awto_debug.registry import _load_registry, _save_probe_usb_ids, lookup_probe

STLINK_VID = 0x0483
STLINK_PIDS = (
    0x3744,
    0x3748,
    0x374B,
    0x374A,
    0x374E,
    0x3752,
    0x3753,
    0x3754,
)

STLINK_PID_TYPES = {
    0x3744: "ST-LINK/V2",
    0x3748: "ST-LINK/V2",
    0x374A: "ST-LINK/V2-1",
    0x374B: "ST-LINK/V2-1",
    0x374E: "ST-LINK/V3E",
    0x3752: "ST-LINK/V3-MNIE",
    0x3753: "ST-LINK/V3",
    0x3754: "ST-LINK/V3",
}


def _default_probe_model(vid: int, pid: int) -> str:
    kind = STLINK_PID_TYPES.get(pid)
    if kind:
        return f"{kind} ({vid:04x}:{pid:04x})"
    return f"USB {vid:04x}:{pid:04x}"


def _read_text_file(path: Path) -> Optional[str]:
    try:
        return path.read_text().strip()
    except Exception:
        return None


def _find_usb_sysfs_node(bus: int, address: int, serial: Optional[str]) -> Optional[Path]:
    for entry in Path("/sys/bus/usb/devices").glob("*"):
        busnum = _read_text_file(entry / "busnum")
        devnum = _read_text_file(entry / "devnum")
        if busnum is None or devnum is None:
            continue
        try:
            if int(busnum) != bus or int(devnum) != address:
                continue
        except ValueError:
            continue

        if serial:
            sysfs_serial = _read_text_file(entry / "serial")
            if sysfs_serial and sysfs_serial != serial:
                continue
        return entry
    return None


def _serial_by_id_map() -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    by_id = Path("/dev/serial/by-id")
    if not by_id.exists():
        return out

    for link in by_id.glob("*"):
        try:
            resolved = link.resolve(strict=True)
        except Exception:
            continue
        tty_name = resolved.name
        out.setdefault(tty_name, []).append(link.name)

    for vals in out.values():
        vals.sort()
    return out


def find_probe_vcps() -> List[Dict[str, Any]]:
    """List VCP tty devices and map them to detected ST-Link probes."""
    entries: List[Dict[str, Any]] = []
    by_id_map = _serial_by_id_map()

    for dev in _find_all_stlink_usb_devices():
        serial = dev.get("serial")
        if not serial:
            continue

        vid = int(dev["vid"])
        pid = int(dev["pid"])
        bus = int(dev["bus"])
        address = int(dev["address"])
        probe_type = _default_probe_model(vid, pid)

        usb_node = _find_usb_sysfs_node(bus, address, serial)
        if usb_node is None:
            continue

        for tty_node in usb_node.glob("**/tty*"):
            tty_name = tty_node.name
            if not re.match(r"^tty(ACM|USB)\d+$", tty_name):
                continue

            dev_path = Path("/dev") / tty_name
            if not dev_path.exists():
                continue

            iface_dir = tty_node.parent.parent
            iface_num = _read_text_file(iface_dir / "bInterfaceNumber")
            iface_name = _read_text_file(iface_dir / "interface")
            product = _read_text_file(usb_node / "product")
            manufacturer = _read_text_file(usb_node / "manufacturer")

            entries.append({
                "probe_serial": serial,
                "probe_nick": serial[-3:] if len(serial) >= 3 else serial,
                "probe_type": probe_type,
                "usb_vid": f"{vid:04x}",
                "usb_pid": f"{pid:04x}",
                "usb_bus": bus,
                "usb_address": address,
                "device": os.fspath(dev_path),
                "by_id": [f"/dev/serial/by-id/{n}" for n in by_id_map.get(tty_name, [])],
                "interface_number": iface_num,
                "interface_name": iface_name,
                "usb_product": product,
                "usb_manufacturer": manufacturer,
            })

    entries.sort(key=lambda x: (x["probe_serial"], x["device"]))
    return entries


def _require_usb_core():
    try:
        import usb.core  # type: ignore
        return usb.core
    except ImportError as exc:
        raise RuntimeError(
            "Missing required support package: pyusb. "
            "Install with Fedora dnf: sudo dnf install -y python3-pyusb "
            "or with pip: python3 -m pip install pyusb"
        ) from exc


class STLinkProbe:
    def __init__(self, serial: str, description: str = ""):
        self.serial = serial
        self.description = description
        self.last_3 = serial[-3:] if len(serial) >= 3 else serial

    def __str__(self) -> str:
        return f"SN: {self.serial} (...{self.last_3}) {self.description}"


def _find_stlink_by_serial(serial: Optional[str], preferred_pid: Optional[int] = None):
    usb_core = _require_usb_core()

    pids_to_try: List[int] = []
    if preferred_pid is not None:
        pids_to_try.append(preferred_pid)
    for pid in STLINK_PIDS:
        if pid not in pids_to_try:
            pids_to_try.append(pid)

    for pid in pids_to_try:
        for dev in usb_core.find(find_all=True, idVendor=STLINK_VID, idProduct=pid) or []:
            if serial is None:
                return dev
            try:
                if (dev.serial_number or "") == serial:
                    return dev
            except (ValueError, usb_core.USBError):
                continue
    return None


def _sysfs_serial_map() -> dict:
    """Return {(bus, address): serial} from /sys/bus/usb/devices (no USB protocol needed)."""
    result: dict = {}
    for entry in Path("/sys/bus/usb/devices").glob("*"):
        try:
            vid = int((entry / "idVendor").read_text().strip(), 16)
            pid = int((entry / "idProduct").read_text().strip(), 16)
            if vid != STLINK_VID or pid not in STLINK_PIDS:
                continue
            bus = int((entry / "busnum").read_text().strip())
            address = int((entry / "devnum").read_text().strip())
            serial_path = entry / "serial"
            serial = serial_path.read_text().strip() if serial_path.exists() else None
            if serial:
                result[(bus, address)] = serial
        except (OSError, ValueError):
            continue
    return result


def _find_all_stlink_usb_devices() -> List[dict]:
    devices: List[dict] = []
    seen: set = set()

    try:
        usb_core = _require_usb_core()

        for pid in STLINK_PIDS:
            for dev in usb_core.find(find_all=True, idVendor=STLINK_VID, idProduct=pid) or []:
                key = (int(dev.bus), int(dev.address))
                if key in seen:
                    continue
                seen.add(key)
                serial: Optional[str] = None
                try:
                    serial = dev.serial_number or None
                except (ValueError, usb_core.USBError):
                    serial = None
                devices.append({
                    "dev": dev,
                    "serial": serial,
                    "vid": int(dev.idVendor),
                    "pid": int(dev.idProduct),
                    "bus": int(dev.bus),
                    "address": int(dev.address),
                })
        if devices:
            # pyusb may not be able to read serial strings when running in a
            # restricted sandbox (e.g. VS Code agent) — enrich with sysfs serials.
            if any(d["serial"] is None for d in devices):
                sysfs = _sysfs_serial_map()
                for d in devices:
                    if d["serial"] is None:
                        d["serial"] = sysfs.get((d["bus"], d["address"]))
            return devices
    except RuntimeError:
        raise
    except Exception:
        pass

    for entry in Path("/sys/bus/usb/devices").glob("*"):
        try:
            vid = int((entry / "idVendor").read_text().strip(), 16)
            pid = int((entry / "idProduct").read_text().strip(), 16)
            if vid != STLINK_VID or pid not in STLINK_PIDS:
                continue
            bus = int((entry / "busnum").read_text().strip())
            address = int((entry / "devnum").read_text().strip())
            key = (bus, address)
            if key in seen:
                continue
            seen.add(key)
            serial_path = entry / "serial"
            serial = serial_path.read_text().strip() if serial_path.exists() else None
            devices.append({
                "dev": None,
                "serial": serial or None,
                "vid": vid,
                "pid": pid,
                "bus": bus,
                "address": address,
            })
        except (OSError, ValueError):
            continue

    return devices


def find_probes(reset_usb: bool = False, list_command: Optional[List[str]] = None) -> List[STLinkProbe]:
    reg = _load_registry()
    reg_probes = {p.get("serial"): p for p in reg.get("probes", []) if p.get("serial")}

    try:
        if reset_usb:
            reset_detected_stlink_usb_devices()

        probes: List[STLinkProbe] = []
        for entry in _find_all_stlink_usb_devices():
            serial = entry.get("serial")
            if not serial:
                continue
            reg_entry = reg_probes.get(serial, {})
            default_model = _default_probe_model(int(entry["vid"]), int(entry["pid"]))
            model = reg_entry.get("model")
            if isinstance(model, str) and model and not model.startswith("USB "):
                description = model
            else:
                description = default_model
            probes.append(STLinkProbe(serial, description))
            try:
                _save_probe_usb_ids(serial, int(entry["vid"]), int(entry["pid"]))
            except Exception:
                pass

        if probes:
            probes.sort(key=lambda p: p.serial)
            return probes

        if list_command:
            result = subprocess.run([*list_command, "-l", "stlink"], capture_output=True, text=True)
            fallback: List[STLinkProbe] = []
            for line in result.stdout.splitlines():
                if "ST-LINK SN" in line:
                    match = re.search(r"([0-9A-F]{24})", line)
                    if match:
                        sn = match.group(1)
                        desc_start = line.find(sn) + len(sn)
                        fallback.append(STLinkProbe(sn, line[desc_start:].strip()))
            return fallback

        return []

    except FileNotFoundError:
        print("Error: programmer binary not found on PATH.", file=sys.stderr)
        sys.exit(2)
    except Exception as exc:
        print(f"Error detecting probes: {exc}", file=sys.stderr)
        sys.exit(2)


def _poll_stlink_reenumerated(timeout_s: float = 5.0, serial: Optional[str] = None, preferred_pid: Optional[int] = None) -> bool:
    deadline = time.monotonic() + timeout_s
    t_start = deadline - timeout_s
    while time.monotonic() < deadline:
        time.sleep(0.05)
        if _find_stlink_by_serial(serial, preferred_pid) is not None:
            elapsed_ms = int((time.monotonic() - t_start) * 1000)
            print(f"ST-Link re-enumerated after {elapsed_ms}ms")
            return True
    return False


def _sysfs_port_rebind(bus: int, devaddr: int) -> bool:
    import glob

    for entry in glob.glob("/sys/bus/usb/devices/*"):
        busnum_path = os.path.join(entry, "busnum")
        devnum_path = os.path.join(entry, "devnum")
        auth_path = os.path.join(entry, "authorized")
        try:
            if (int(open(busnum_path).read().strip()) == bus and
                    int(open(devnum_path).read().strip()) == devaddr and
                    os.path.exists(auth_path)):
                open(auth_path, "w").write("0")
                time.sleep(0.3)
                open(auth_path, "w").write("1")
                return True
        except (OSError, ValueError):
            continue
    return False


def usb_reset_stlink(serial: Optional[str] = None) -> bool:
    return _usb_reset_stlink_impl(serial)


def _usb_reset_stlink_impl(serial: Optional[str] = None) -> bool:
    try:
        preferred_pid: Optional[int] = None
        if serial:
            entry = lookup_probe(serial) or {}
            preferred_pid = entry.get("usb_pid")

        dev = _find_stlink_by_serial(serial, preferred_pid)
        if dev is None:
            return False

        if serial:
            try:
                _save_probe_usb_ids(serial, int(dev.idVendor), int(dev.idProduct))
            except Exception:
                pass

        bus, devaddr = dev.bus, dev.address
        found_pid = int(dev.idProduct)

        try:
            dev.reset()
        except Exception:
            pass
        if _poll_stlink_reenumerated(5.0, serial, found_pid):
            return True

        if _sysfs_port_rebind(bus, devaddr):
            if _poll_stlink_reenumerated(5.0, serial, found_pid):
                return True

        devnode = f"/dev/bus/usb/{bus:03d}/{devaddr:03d}"
        usbreset_bin = shutil.which("usbreset")
        if usbreset_bin and os.path.exists(devnode):
            subprocess.run([usbreset_bin, devnode], timeout=10, check=False)
            if _poll_stlink_reenumerated(5.0, serial, found_pid):
                return True

        return False

    except Exception:
        return False


def _hub_location_and_port(sysfs_name: str) -> Optional[tuple[str, str]]:
    """Split a sysfs USB path like '1-1.4.2' or '1-5' into (hub_location, port).

    uhubctl wants ``-l <hub_location> -p <port>``; for '1-1.4.2' that's
    ('1-1.4', '2'); for '1-5' that's ('1', '5').
    Returns None if the path doesn't match either form.
    """
    if "." in sysfs_name:
        hub, _, port = sysfs_name.rpartition(".")
        return (hub, port) if hub and port else None
    if "-" in sysfs_name:
        bus, _, port = sysfs_name.partition("-")
        return (bus, port) if bus and port else None
    return None


def power_cycle_stlink(serial: Optional[str], delay_s: float = 2.0) -> bool:
    """Hard VBUS power-cycle of the ST-Link's USB port via uhubctl.

    Only works on hubs that support per-port power switching (PPPS). Returns
    True on success, False otherwise (including when uhubctl is missing, the
    hub lacks PPPS, or the probe cannot be located). Re-enumeration is polled
    for up to 5s after the cycle.
    """
    if sys.platform != "linux":
        return False
    uhubctl = shutil.which("uhubctl")
    if not uhubctl:
        return False

    try:
        preferred_pid: Optional[int] = None
        if serial:
            entry = lookup_probe(serial) or {}
            preferred_pid = entry.get("usb_pid")

        dev = _find_stlink_by_serial(serial, preferred_pid)
        if dev is None:
            return False

        bus, devaddr = int(dev.bus), int(dev.address)
        found_pid = int(dev.idProduct)
        node = _find_usb_sysfs_node(bus, devaddr, serial)
        if node is None:
            return False

        loc_port = _hub_location_and_port(node.name)
        if loc_port is None:
            return False
        hub_location, port = loc_port

        result = subprocess.run(
            [uhubctl, "-l", hub_location, "-p", port, "-a", "cycle", "-d", str(int(delay_s))],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            return False

        return _poll_stlink_reenumerated(8.0, serial, found_pid)

    except Exception:
        return False


def reset_detected_stlink_usb_devices() -> None:
    _reset_detected_stlink_usb_devices_impl()


def _reset_detected_stlink_usb_devices_impl() -> None:
    devices = _find_all_stlink_usb_devices()
    for entry in devices:
        dev = entry["dev"]
        serial = entry["serial"]
        vid, pid = entry["vid"], entry["pid"]
        bus, address = entry["bus"], entry["address"]
        if serial:
            try:
                _save_probe_usb_ids(serial, vid, pid)
            except Exception:
                pass
        try:
            if dev is not None:
                dev.reset()
            else:
                _sysfs_port_rebind(bus, address)
        except Exception:
            continue
