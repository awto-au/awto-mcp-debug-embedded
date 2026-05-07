"""
debug_workflow.py — high-level, policy-driven debug workflows.

This module provides a server-owned control plane so MCP clients can request
intent-level operations (start session, capture snapshot, run safe flash cycle)
without directly sequencing low-level debugger commands.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

import debugger_stlink as stlink


VALID_RESPONSE_MODES = {"compact", "full"}
REQUEST_ORIGIN_DEFAULT = "Dan (embedded developer, original request source)"


def _normalize_response_mode(response_mode: str) -> str:
    mode = (response_mode or "compact").strip().lower()
    if mode not in VALID_RESPONSE_MODES:
        raise RuntimeError(
            f"Unsupported response_mode={response_mode!r}. "
            "Use 'compact' or 'full'."
        )
    return mode


def _resolve_detail_level(detail_level: Optional[str], session: "WorkflowSession") -> str:
    if detail_level:
        return _normalize_response_mode(detail_level)
    if session.deep_debug:
        return "full"
    return session.response_mode


def _last_event_type(session: "WorkflowSession") -> Optional[str]:
    if not session.events:
        return None
    return str(session.events[-1].get("type"))


@dataclass
class WorkflowSession:
    id: str
    target_kind: str
    serial: str
    created_at: str
    target_info: dict[str, Any]
    response_mode: str = "compact"
    deep_debug: bool = False
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


def start_session(
    target_kind: str = "stlink",
    serial: Optional[str] = None,
    response_mode: str = "compact",
    deep_debug: bool = False,
) -> dict[str, Any]:
    """Create a managed debug session bound to a concrete probe serial."""
    if target_kind != "stlink":
        raise RuntimeError("Only target_kind='stlink' is currently supported.")

    mode = _normalize_response_mode(response_mode)
    resolved_serial = _pick_stlink_serial(serial)
    info = stlink.chip_info(resolved_serial)

    session = WorkflowSession(
        id=str(uuid4()),
        target_kind=target_kind,
        serial=resolved_serial,
        created_at=_now_iso(),
        target_info=info,
        response_mode=mode,
        deep_debug=bool(deep_debug),
    )
    session.events.append({
        "time": _now_iso(),
        "type": "session_started",
        "details": {
            "target_kind": target_kind,
            "serial": resolved_serial,
            "target_info": info,
            "response_mode": session.response_mode,
            "deep_debug": session.deep_debug,
        },
    })
    _SESSIONS[session.id] = session
    return asdict(session)


def set_session_mode(
    session_id: str,
    response_mode: str = "compact",
    deep_debug: bool = False,
) -> dict[str, Any]:
    """Set token/detail behavior for a managed session."""
    session = _SESSIONS.get(session_id)
    if not session:
        raise RuntimeError(f"Session {session_id!r} not found.")

    mode = _normalize_response_mode(response_mode)
    session.response_mode = mode
    session.deep_debug = bool(deep_debug)
    session.events.append({
        "time": _now_iso(),
        "type": "mode_updated",
        "details": {
            "response_mode": session.response_mode,
            "deep_debug": session.deep_debug,
        },
    })
    return {
        "session_id": session.id,
        "response_mode": session.response_mode,
        "deep_debug": session.deep_debug,
        "event_count": len(session.events),
        "last_event": _last_event_type(session),
    }


def session_status(session_id: str, detail_level: str = "compact") -> dict[str, Any]:
    session = _SESSIONS.get(session_id)
    if not session:
        raise RuntimeError(f"Session {session_id!r} not found.")

    mode = _normalize_response_mode(detail_level)
    if mode == "full":
        return asdict(session)

    return {
        "id": session.id,
        "target_kind": session.target_kind,
        "serial": session.serial,
        "created_at": session.created_at,
        "response_mode": session.response_mode,
        "deep_debug": session.deep_debug,
        "event_count": len(session.events),
        "last_event": _last_event_type(session),
        "target": {
            "chip_id": session.target_info.get("chip_id"),
            "device_name": session.target_info.get("device_name"),
        },
    }


def session_memory_snapshot(
    session_id: str,
    output_dir: str,
    flash_address: str = "0x08000000",
    flash_length: Optional[int] = None,
    ram_address: str = "0x20000000",
    ram_length: Optional[int] = None,
    include_flash: bool = True,
    include_ram: bool = True,
    detail_level: Optional[str] = None,
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

    mode = _resolve_detail_level(detail_level, session)
    payload: dict[str, Any] = {
        "session_id": session_id,
        "serial": session.serial,
        "result": result if mode == "full" else {
            "output_dir": result.get("output_dir"),
            "files": result.get("files", {}),
            "flash_length": result.get("flash_length"),
            "ram_length": result.get("ram_length"),
        },
        "detail_level": mode,
    }
    return payload


def session_safe_flash_cycle(
    session_id: str,
    output_dir: str,
    confirm_destructive: bool,
    flash_address: str = "0x08000000",
    flash_length: Optional[int] = None,
    detail_level: Optional[str] = None,
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

    mode = _resolve_detail_level(detail_level, session)
    if mode == "full":
        return summary

    return {
        "session_id": summary["session_id"],
        "serial": summary["serial"],
        "flash_address": summary["flash_address"],
        "flash_length": summary["flash_length"],
        "restore_match": summary["restore_match"],
        "summary_file": summary["summary_file"],
        "detail_level": mode,
    }


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


def parallel_flash_program(
    serials: list[str],
    firmware_path: str,
    address: str = "0x8000000",
    reset: bool = True,
    max_workers: int = 4,
    continue_on_error: bool = True,
    detail_level: str = "compact",
) -> dict[str, Any]:
    """Program multiple ST-Link targets concurrently to reduce iterative retries."""
    if not serials:
        raise RuntimeError("No serials provided.")
    mode = _normalize_response_mode(detail_level)
    workers = max(1, min(int(max_workers), len(serials)))

    results: list[dict[str, Any]] = []

    def _program_one(serial: str) -> dict[str, Any]:
        try:
            out = stlink.flash_write(
                firmware_path,
                address=address,
                serial=serial,
                reset=reset,
            )
            return {"serial": serial, "ok": True, "output": out}
        except RuntimeError as exc:
            return {"serial": serial, "ok": False, "error": str(exc)}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_by_serial = {pool.submit(_program_one, serial): serial for serial in serials}
        for future in as_completed(future_by_serial):
            res = future.result()
            results.append(res)
            if not continue_on_error and not res.get("ok"):
                for pending in future_by_serial:
                    if not pending.done():
                        pending.cancel()
                break

    ok_count = sum(1 for r in results if r.get("ok"))
    fail_count = len(results) - ok_count
    summary: dict[str, Any] = {
        "requested": len(serials),
        "attempted": len(results),
        "ok_count": ok_count,
        "fail_count": fail_count,
        "failed_serials": [r["serial"] for r in results if not r.get("ok")],
        "detail_level": mode,
    }
    if mode == "full":
        summary["results"] = results
    return summary


def build_user_action_request(
    command: str,
    reason: str,
    expected_output: str = "",
    request_origin: str = REQUEST_ORIGIN_DEFAULT,
) -> dict[str, Any]:
    """Return a structured escalation payload when human execution is needed."""
    cmd = command.strip()
    why = reason.strip()
    if not cmd:
        raise RuntimeError("command must not be empty")
    if not why:
        raise RuntimeError("reason must not be empty")

    payload = {
        "action": "ask_user_to_run_command",
        "command": cmd,
        "reason": why,
        "request_origin": request_origin,
        "message": (
            "Please ask the user to run the command below and return output. "
            f"Request origin: {request_origin}."
        ),
    }
    if expected_output.strip():
        payload["expected_output"] = expected_output.strip()
    return payload
