import json
import sys
from pathlib import Path
from typing import Dict, Optional

_REGISTRY_PATH = Path("probes.json")


def configure(path) -> None:
    """Set registry file path (str or Path)."""
    global _REGISTRY_PATH
    _REGISTRY_PATH = Path(path)


def _load_registry() -> dict:
    if _REGISTRY_PATH.exists():
        try:
            return json.loads(_REGISTRY_PATH.read_text())
        except Exception:
            pass
    return {"probes": [], "boards": []}


def _save_registry(reg: dict) -> None:
    _REGISTRY_PATH.write_text(json.dumps(reg, indent=2) + "\n")


def lookup_probe(serial: str) -> Optional[dict]:
    reg = _load_registry()
    for p in reg.get("probes", []):
        if p.get("serial") == serial:
            return p
    return None


def lookup_board(cpu_serial: str) -> Optional[dict]:
    reg = _load_registry()
    for b in reg.get("boards", []):
        if b.get("cpu_serial") == cpu_serial:
            return b
    return None


def _save_probe_usb_ids(serial: str, vid: int, pid: int) -> None:
    reg = _load_registry()
    changed = False
    for p in reg.get("probes", []):
        if p.get("serial") == serial:
            if p.get("usb_vid") != vid or p.get("usb_pid") != pid:
                p["usb_vid"] = vid
                p["usb_pid"] = pid
                changed = True
            break
    if changed:
        try:
            _save_registry(reg)
            print(f"[registry] Cached USB id {vid:04x}:{pid:04x} for probe ...{serial[-3:]}")
        except Exception as exc:
            print(f"[registry] Could not persist USB id: {exc}", file=sys.stderr)


def get_mode_probe_map() -> Dict[str, str]:
    reg = _load_registry()
    raw = reg.get("mode_probe_map", {})
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, str] = {}
    for mode, serial in raw.items():
        if isinstance(mode, str) and isinstance(serial, str):
            out[mode.upper()] = serial
    return out


def mode_probe_auto_update_enabled() -> bool:
    reg = _load_registry()
    return bool(reg.get("mode_probe_map_auto_update", False))


def update_mode_probe_map(mode: str, probe_serial: str) -> None:
    reg = _load_registry()
    mode_map = reg.setdefault("mode_probe_map", {})
    mode_key = mode.upper()
    old = mode_map.get(mode_key)
    if old == probe_serial:
        return
    mode_map[mode_key] = probe_serial
    _save_registry(reg)
    print(f"[registry] mode_probe_map[{mode_key}] = {probe_serial[-3:]} ({probe_serial})")
