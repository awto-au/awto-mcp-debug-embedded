"""
cpu_registry.py — persistent CPU identity registry (cpus.json).

This registry is separate from probes.json:
- probes.json tracks debug adapters/probes and approval state
- cpus.json tracks discovered target CPU identities

Identity policy:
- ESP targets are keyed by MAC address
- STM32 targets are keyed by CPU type string (device_name/chip_id family)
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_REGISTRY_PATH: Path = Path("cpus.json")
_registry_lock = threading.Lock()
CPU_ALLOWED_STATES = {"pending", "approved", "ignored", "blocked", "revoked"}


def configure_registry(path: str | Path) -> None:
    """Override default CPU registry file path."""
    global _REGISTRY_PATH
    _REGISTRY_PATH = Path(path)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_registry() -> dict[str, Any]:
    with _registry_lock:
        if _REGISTRY_PATH.exists():
            try:
                return json.loads(_REGISTRY_PATH.read_text())
            except Exception:
                return {"cpus": []}
        return {"cpus": []}


def _save_registry(reg: dict[str, Any]) -> None:
    with _registry_lock:
        _REGISTRY_PATH.write_text(json.dumps(reg, indent=2) + "\n")


def _normalize_cpu_row(row: dict[str, Any]) -> dict[str, Any]:
    state = str(row.get("state") or "pending").strip().lower()
    if state not in CPU_ALLOWED_STATES:
        state = "pending"

    is_approved = state == "approved"
    scan_default = state not in {"ignored", "revoked"}

    normalized = dict(row)
    normalized["state"] = state
    normalized["scan_allowed"] = bool(row.get("scan_allowed", scan_default))
    normalized["read_allowed"] = bool(row.get("read_allowed", is_approved))
    normalized["flash_allowed"] = bool(row.get("flash_allowed", is_approved))
    normalized["stop_allowed"] = bool(row.get("stop_allowed", is_approved))
    return normalized


def _upsert(entry_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    reg = _load_registry()
    cpus: list[dict[str, Any]] = reg.setdefault("cpus", [])
    now = _now_iso()

    for row in cpus:
        if row.get("id") == entry_id:
            row.update(_normalize_cpu_row(row))
            state = row.get("state", "pending")
            row.update(payload)
            row["state"] = state
            row["last_seen"] = now
            row.update(_normalize_cpu_row(row))
            _save_registry(reg)
            return row

    row = {
        "id": entry_id,
        "state": "pending",
        "first_seen": now,
        "last_seen": now,
        **payload,
    }
    row.update(_normalize_cpu_row(row))
    cpus.append(row)
    _save_registry(reg)
    return row


def register_esp(
    mac: str,
    chip_type: str,
    port: Optional[str] = None,
    details: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Register/update an ESP CPU entry keyed by MAC."""
    normalized_mac = (mac or "").strip().lower()
    if not normalized_mac:
        raise RuntimeError("ESP registration requires a MAC address")

    entry_id = f"esp:{normalized_mac}"
    payload: dict[str, Any] = {
        "kind": "esp",
        "mac": normalized_mac,
        "cpu_type": (chip_type or "unknown").strip() or "unknown",
    }
    if port:
        payload["last_port"] = port
    if details:
        payload["details"] = details
    return _upsert(entry_id, payload)


def register_stm32(
    cpu_type: str,
    probe_serial: Optional[str] = None,
    details: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Register/update an STM32 CPU entry keyed by CPU type."""
    normalized_type = (cpu_type or "").strip()
    if not normalized_type:
        raise RuntimeError("STM32 registration requires cpu_type")

    entry_id = f"stm32:{normalized_type.lower()}"
    payload: dict[str, Any] = {
        "kind": "stm32",
        "cpu_type": normalized_type,
    }
    if probe_serial:
        payload["last_probe_serial"] = probe_serial
    if details:
        payload["details"] = details
    return _upsert(entry_id, payload)


def list_cpus(kind: Optional[str] = None) -> list[dict[str, Any]]:
    """Return all CPUs, optionally filtered by kind ('esp' or 'stm32')."""
    rows = [_normalize_cpu_row(row) for row in _load_registry().get("cpus", [])]
    if kind:
        kind_norm = kind.strip().lower()
        rows = [row for row in rows if str(row.get("kind", "")).lower() == kind_norm]
    return rows


def get_all_cpus() -> list[dict[str, Any]]:
    """Backwards-compatible alias for list_cpus()."""
    return list_cpus()


def approve_cpu(entry_id: str) -> Optional[dict[str, Any]]:
    """Mark a CPU entry approved. Returns updated entry, or None if missing."""
    reg = _load_registry()
    for row in reg.get("cpus", []):
        if row.get("id") == entry_id:
            row.update(_normalize_cpu_row(row))
            row["state"] = "approved"
            row["scan_allowed"] = True
            row["read_allowed"] = True
            row["flash_allowed"] = True
            row["stop_allowed"] = True
            row["last_seen"] = _now_iso()
            _save_registry(reg)
            return row
    return None


def ignore_cpu(entry_id: str) -> bool:
    """Mark a CPU entry ignored. Returns True if found."""
    reg = _load_registry()
    for row in reg.get("cpus", []):
        if row.get("id") == entry_id:
            row.update(_normalize_cpu_row(row))
            row["state"] = "ignored"
            row["scan_allowed"] = False
            row["read_allowed"] = False
            row["flash_allowed"] = False
            row["stop_allowed"] = False
            row["last_seen"] = _now_iso()
            _save_registry(reg)
            return True
    return False


def set_cpu_state(entry_id: str, state: str) -> Optional[dict[str, Any]]:
    """Set CPU lifecycle state. Returns updated row or None."""
    normalized_state = (state or "").strip().lower()
    if normalized_state not in CPU_ALLOWED_STATES:
        raise RuntimeError(
            f"Invalid CPU state {state!r}; allowed: {sorted(CPU_ALLOWED_STATES)}"
        )

    reg = _load_registry()
    for row in reg.get("cpus", []):
        if row.get("id") == entry_id:
            row.update(_normalize_cpu_row(row))
            row["state"] = normalized_state
            if normalized_state in {"ignored", "blocked", "revoked"}:
                row["read_allowed"] = False
                row["flash_allowed"] = False
                row["stop_allowed"] = False
                row["scan_allowed"] = normalized_state not in {"ignored", "revoked"}
            elif normalized_state == "approved":
                row["scan_allowed"] = True
            row["last_seen"] = _now_iso()
            row.update(_normalize_cpu_row(row))
            _save_registry(reg)
            return row
    return None


def set_cpu_permissions(
    entry_id: str,
    *,
    scan_allowed: Optional[bool] = None,
    read_allowed: Optional[bool] = None,
    flash_allowed: Optional[bool] = None,
    stop_allowed: Optional[bool] = None,
) -> Optional[dict[str, Any]]:
    """Set per-action permissions for a CPU entry. Returns updated row or None."""
    reg = _load_registry()
    for row in reg.get("cpus", []):
        if row.get("id") == entry_id:
            row.update(_normalize_cpu_row(row))
            if scan_allowed is not None:
                row["scan_allowed"] = bool(scan_allowed)
            if read_allowed is not None:
                row["read_allowed"] = bool(read_allowed)
            if flash_allowed is not None:
                row["flash_allowed"] = bool(flash_allowed)
            if stop_allowed is not None:
                row["stop_allowed"] = bool(stop_allowed)
            row["last_seen"] = _now_iso()
            row.update(_normalize_cpu_row(row))
            _save_registry(reg)
            return row
    return None


def clear_cpu(entry_id: str) -> bool:
    """Remove a CPU entry from registry. Returns True if removed."""
    reg = _load_registry()
    before = len(reg.get("cpus", []))
    reg["cpus"] = [row for row in reg.get("cpus", []) if row.get("id") != entry_id]
    if len(reg["cpus"]) < before:
        _save_registry(reg)
        return True
    return False


def get_cpu(entry_id: str) -> Optional[dict[str, Any]]:
    """Lookup a CPU registry entry by ID."""
    for row in _load_registry().get("cpus", []):
        if row.get("id") == entry_id:
            return _normalize_cpu_row(row)
    return None
