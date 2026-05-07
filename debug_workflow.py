"""
debug_workflow.py — high-level, policy-driven debug workflows.

This module provides a server-owned control plane so MCP clients can request
intent-level operations (start session, capture snapshot, run safe flash cycle)
without directly sequencing low-level debugger commands.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

import debugger_stlink as stlink


@dataclass
class WorkflowSession:
    id: str
    target_kind: str
    serial: str
    created_at: str
    target_info: dict[str, Any]
    events: list[dict[str, Any]] = field(default_factory=list)


_SESSIONS: dict[str, WorkflowSession] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _pick_stlink_serial(serial: Optional[str]) -> str:
    if serial:
        return serial
    probes = stlink.probe_list()
    if not probes:
        raise RuntimeError("No ST-Link probes detected.")
    if len(probes) > 1:
        serials = [str(p.get("serial", "")) for p in probes]
        raise RuntimeError(
            "Multiple ST-Link probes detected; specify serial explicitly. "
            f"Candidates: {serials}"
        )
    resolved = probes[0].get("serial")
    if not resolved:
        raise RuntimeError("Probe found but serial missing in probe metadata.")
    return str(resolved)


def start_session(target_kind: str = "stlink", serial: Optional[str] = None) -> dict[str, Any]:
    """Create a managed debug session bound to a concrete probe serial."""
    if target_kind != "stlink":
        raise RuntimeError("Only target_kind='stlink' is currently supported.")

    resolved_serial = _pick_stlink_serial(serial)
    info = stlink.chip_info(resolved_serial)

    session = WorkflowSession(
        id=str(uuid4()),
        target_kind=target_kind,
        serial=resolved_serial,
        created_at=_now_iso(),
        target_info=info,
    )
    session.events.append({
        "time": _now_iso(),
        "type": "session_started",
        "details": {
            "target_kind": target_kind,
            "serial": resolved_serial,
            "target_info": info,
        },
    })
    _SESSIONS[session.id] = session
    return asdict(session)


def session_status(session_id: str) -> dict[str, Any]:
    session = _SESSIONS.get(session_id)
    if not session:
        raise RuntimeError(f"Session {session_id!r} not found.")
    return asdict(session)


def session_memory_snapshot(
    session_id: str,
    output_dir: str,
    flash_address: str = "0x08000000",
    flash_length: Optional[int] = None,
    ram_address: str = "0x20000000",
    ram_length: Optional[int] = None,
    include_flash: bool = True,
    include_ram: bool = True,
) -> dict[str, Any]:
    """Run a managed memory snapshot using the session's bound probe serial."""
    session = _SESSIONS.get(session_id)
    if not session:
        raise RuntimeError(f"Session {session_id!r} not found.")

    result = stlink.dump_memory_snapshot(
        output_dir=output_dir,
        serial=session.serial,
        flash_address=flash_address,
        flash_length=flash_length,
        ram_address=ram_address,
        ram_length=ram_length,
        include_flash=include_flash,
        include_ram=include_ram,
    )

    session.events.append({
        "time": _now_iso(),
        "type": "memory_snapshot",
        "details": {
            "output_dir": output_dir,
            "include_flash": include_flash,
            "include_ram": include_ram,
            "flash_length": result.get("flash_length"),
            "ram_length": result.get("ram_length"),
        },
    })

    return {
        "session_id": session_id,
        "serial": session.serial,
        "result": result,
    }


def session_safe_flash_cycle(
    session_id: str,
    output_dir: str,
    confirm_destructive: bool,
    flash_address: str = "0x08000000",
    flash_length: Optional[int] = None,
) -> dict[str, Any]:
    """
    Execute safe destructive workflow: backup -> erase -> read erased -> restore -> verify -> reset.

    Requires confirm_destructive=True to prevent accidental execution.
    """
    if not confirm_destructive:
        raise RuntimeError("Destructive workflow blocked: set confirm_destructive=true to proceed.")

    session = _SESSIONS.get(session_id)
    if not session:
        raise RuntimeError(f"Session {session_id!r} not found.")

    target_info = stlink.chip_info(session.serial)
    inferred_length = flash_length
    if inferred_length is None:
        flash_kb = target_info.get("flash_size_kb")
        if not flash_kb:
            raise RuntimeError("Could not infer flash size; pass flash_length explicitly.")
        inferred_length = int(flash_kb) * 1024

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    pre_path = out / "flash_backup_pre.bin"
    erased_path = out / "flash_after_erase.bin"
    post_path = out / "flash_after_restore.bin"

    stlink.flash_read(str(pre_path), flash_address, inferred_length, session.serial)
    stlink.flash_erase(session.serial)
    stlink.flash_read(str(erased_path), flash_address, inferred_length, session.serial)
    stlink.flash_write(str(pre_path), address=flash_address, serial=session.serial, format="binary", reset=False)
    stlink.flash_read(str(post_path), flash_address, inferred_length, session.serial)
    stlink.reset_target(session.serial)

    pre_hash = _sha256(pre_path)
    erased_hash = _sha256(erased_path)
    post_hash = _sha256(post_path)
    restore_match = pre_hash == post_hash

    summary = {
        "session_id": session_id,
        "serial": session.serial,
        "flash_address": flash_address,
        "flash_length": inferred_length,
        "files": {
            "pre_backup": str(pre_path),
            "after_erase": str(erased_path),
            "after_restore": str(post_path),
        },
        "hashes": {
            "pre_sha256": pre_hash,
            "erased_sha256": erased_hash,
            "post_sha256": post_hash,
        },
        "restore_match": restore_match,
        "target_info": target_info,
        "notes": [
            "OTP / option-byte writes are not part of this workflow.",
            "This workflow only covers the requested internal flash range.",
        ],
    }

    summary_path = out / "safe_flash_cycle_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n")
    summary["summary_file"] = str(summary_path)

    session.events.append({
        "time": _now_iso(),
        "type": "safe_flash_cycle",
        "details": {
            "output_dir": output_dir,
            "flash_address": flash_address,
            "flash_length": inferred_length,
            "restore_match": restore_match,
            "summary_file": str(summary_path),
        },
    })

    return summary


def session_report(session_id: str, output_path: str) -> dict[str, Any]:
    """Write a complete session handoff report to disk."""
    session = _SESSIONS.get(session_id)
    if not session:
        raise RuntimeError(f"Session {session_id!r} not found.")

    payload = asdict(session)
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")

    return {
        "session_id": session_id,
        "output_path": str(path),
        "bytes_written": path.stat().st_size,
    }
