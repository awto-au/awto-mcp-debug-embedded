#!/usr/bin/env python3
"""
awto-mcp-debug-embedded — MCP server for embedded debuggers.

Exposes ST-Link (open-source + STM32CubeProgrammer) and Espressif ESP-IDF
tools as MCP tools for Copilot / AI agents.

Probe discovery and approval flow (mirrors awto-riden pattern):
  1. A background ProbeMonitor thread watches USB for ST-Link and ESP serial
     adapters continuously.
  2. Newly seen probes appear as state="pending" in probes.json.
  3. probe_list() returns all probes with their state (pending/approved/ignored).
  4. When pending probes are present, the server asks Copilot to show the list
     to the user and call probe_approve(serial, nick) or probe_ignore(serial).
  5. Approved probes are persisted to probes.json and surfaced for debugging.

.vscode/mcp.json:
    {
        "servers": {
            "awto-debug-embedded": {
                "type": "stdio",
                "command": "${workspaceFolder}/.venv/bin/python",
                "args": ["${workspaceFolder}/mcp_server.py"]
            }
        }
    }
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import re
import shutil
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

# Allow running from repo root without install
sys.path.insert(0, str(Path(__file__).parent))

import colorlog
from mcp.server.fastmcp import FastMCP

import debugger_cube as cube
import debugger_esp as esp
import debugger_stlink as stlink
import debug_workflow as wf
import probe_detect as pd
import cpu_registry as cr
from gdb_client import get_client as _gdb
from process_manager import get_manager as _mgr

# ---------------------------------------------------------------------------
# Logging  (colorlog stderr + platform-specific file/syslog handler)
# ---------------------------------------------------------------------------

def _setup_logging(verbose: bool = False) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)

    if sys.platform == "linux":
        # Linux: log to syslog via /dev/log
        try:
            syslog = logging.handlers.SysLogHandler(
                address="/dev/log",
                facility=logging.handlers.SysLogHandler.LOG_USER,
            )
            if getattr(syslog, "socket", None) is not None:
                syslog.ident = "awto-debug-embedded: "
                syslog.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
                root.addHandler(syslog)
            else:
                syslog.close()
        except OSError:
            pass
    else:
        # macOS / Windows: rotate log file in user data dir
        import tempfile
        log_dir = Path(tempfile.gettempdir()) / "awto-mcp-debug-embedded"
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(
                log_dir / "server.log",
                maxBytes=2 * 1024 * 1024,  # 2 MB
                backupCount=3,
                encoding="utf-8",
            )
            fh.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
            ))
            root.addHandler(fh)
        except OSError:
            pass

    handler = colorlog.StreamHandler(sys.stderr)
    handler.setFormatter(colorlog.ColoredFormatter(
        "%(log_color)s%(asctime)s %(levelname)-8s%(reset)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        log_colors={
            "DEBUG":    "cyan",
            "INFO":     "green",
            "WARNING":  "yellow",
            "ERROR":    "red",
            "CRITICAL": "bold_red",
        },
    ))
    root.addHandler(handler)


log = logging.getLogger("awto.mcp")

# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "awto-debug-embedded",
    instructions="""
Control embedded debug probes for STM32 (ST-Link) and Espressif ESP32/ESP32-S3 targets.

FIRST USE — Device approval flow:
1. Call probe_list() to see all detected probes (ST-Link and ESP serial adapters).
2. For any probe with state="pending", show the list to the user and ask:
   - Which probes to use (approve) — optionally assign a short nickname.
   - Which probes to ignore (will be hidden in future sessions).
3. Call probe_approve(serial=..., nick=...) for each approved probe.
4. Call probe_ignore(serial=...) for probes the user does not want.
5. After CPU discovery, call cpu_list() and have the user approve pending CPU
    entries with cpu_approve(cpu_id) before running debug/flash operations.

The monitor runs continuously — if a new probe is plugged in, probe_list()
will show it as "pending" and the approval flow should be repeated.

Workflow for STM32 targets:
  probe_list() → probe_info() → stlink_flash() or cube_flash()
  → stlink_gdb_start() → gdb_connect() → gdb_read_registers() → ...

Workflow for ESP32 targets:
  probe_list() → esp_chip_info() → idf_build() → idf_flash()
  → idf_openocd_start() → gdb_connect() → ...

GDB client notes:
- Always call a GDB server start tool before gdb_connect().
- gdb_halt() before reading memory or registers if the target is running.
- gdb_continue() to resume after inspection.

Managed workflow mode:
- Prefer debug_session_start() once per target/probe.
- Keep token usage low by default with compact session responses.
- Switch to deep diagnostics only when needed via debug_session_set_mode().
- Then use debug_session_memory_snapshot(), debug_session_safe_flash_cycle(),
    debug_parallel_flash_program(), and debug_session_report() for
    policy-enforced, server-owned workflows.
- These tools keep sequencing, safety gates, and artifact reporting on the
    server side instead of delegating low-level control flow to the model.
- If external/manual execution is needed, use debug_user_action_request() to
    produce a structured ask-user payload with origin attribution.
""",
)

# ---------------------------------------------------------------------------
# Probe monitor callbacks — update MCP context on connect/disconnect
# ---------------------------------------------------------------------------

def _on_probe_connect(probe: pd.ProbeInfo) -> None:
    log.info(
        "** Probe connected: %s %s [%s] — call probe_list() to see it",
        probe.model, probe.serial[-8:], probe.state,
    )


def _on_probe_disconnect(probe: pd.ProbeInfo) -> None:
    log.info(
        "** Probe disconnected: %s %s",
        probe.model, probe.serial[-8:],
    )


def _require_probe_capability(probe: pd.ProbeInfo, capability: str) -> None:
    if probe.state != "approved":
        raise RuntimeError(
            f"Probe {probe.serial!r} is state={probe.state!r}. "
            "Approve it first via probe_approve(serial, nick) or set probe_state."
        )
    allowed = bool(getattr(probe, f"{capability}_allowed", False))
    if not allowed:
        raise RuntimeError(
            f"Probe {probe.serial!r} policy blocks capability {capability!r}. "
            "Use probe_set_permissions() or probe_set_state() to allow it."
        )


def _require_cpu_capability(row: dict[str, Any], capability: str) -> None:
    if row.get("state") != "approved":
        raise RuntimeError(
            "CPU requires approval before use. "
            f"Ask the user to approve CPU entry {row.get('id')!r} via cpu_approve()."
        )
    if not bool(row.get(f"{capability}_allowed", False)):
        raise RuntimeError(
            f"CPU policy blocks capability {capability!r} for {row.get('id')!r}. "
            "Use cpu_set_permissions() or cpu_set_state() to allow it."
        )


def _resolve_approved_stlink_serial(serial: Optional[str], capability: str = "scan") -> str:
    probes = [p for p in pd.get_all_probes() if p.kind == "stlink"]
    approved = [p for p in probes if p.state == "approved"]
    pending = [p for p in probes if p.state == "pending"]

    if serial:
        match = next((p for p in probes if p.serial == serial), None)
        if not match:
            raise RuntimeError(
                f"Probe {serial!r} is unknown. Call probe_list() and approve it first."
            )
        _require_probe_capability(match, capability)
        return serial

    if len(approved) == 1:
        _require_probe_capability(approved[0], capability)
        return approved[0].serial
    if len(approved) > 1:
        choices = [p.serial for p in approved]
        raise RuntimeError(f"Multiple approved ST-Link probes found; pass serial explicitly: {choices}")
    if pending:
        pending_serials = [p.serial for p in pending]
        raise RuntimeError(
            "No approved ST-Link probe available. "
            f"Pending probes require user approval first: {pending_serials}"
        )
    raise RuntimeError("No ST-Link probe available. Call probe_list() and approve a probe first.")


def _resolve_approved_esp_port(port: Optional[str], capability: str = "scan") -> str:
    probes = [p for p in pd.get_all_probes() if p.kind == "esp"]
    approved = [p for p in probes if p.state == "approved"]
    pending = [p for p in probes if p.state == "pending"]

    if port:
        match = next((p for p in probes if p.port == port), None)
        if not match:
            raise RuntimeError(
                f"ESP adapter at port {port!r} is unknown. Call probe_list() and approve it first."
            )
        _require_probe_capability(match, capability)
        return port

    approved_with_port = [p for p in approved if p.port]
    if len(approved_with_port) == 1:
        _require_probe_capability(approved_with_port[0], capability)
        return approved_with_port[0].port
    if len(approved_with_port) > 1:
        ports = [p.port for p in approved_with_port]
        raise RuntimeError(f"Multiple approved ESP adapters found; pass port explicitly: {ports}")
    if pending:
        pending_serials = [p.serial for p in pending]
        raise RuntimeError(
            "No approved ESP adapter available. "
            f"Pending adapters require user approval first: {pending_serials}"
        )
    raise RuntimeError("No ESP adapter available. Call probe_list() and approve an adapter first.")


def _register_and_require_stm32_cpu(
    info: dict[str, Any],
    probe_serial: Optional[str],
    capability: str = "read",
) -> dict[str, Any]:
    cpu_type = str(info.get("device_name") or info.get("chip_id") or "").strip()
    if not cpu_type:
        return {"state": "approved", "id": "stm32:unknown"}
    row = cr.register_stm32(cpu_type=cpu_type, probe_serial=probe_serial, details=info)
    _require_cpu_capability(row, capability)
    return row


def _register_and_require_esp_cpu(
    info: dict[str, Any],
    port: Optional[str],
    capability: str = "read",
) -> dict[str, Any]:
    mac = str(info.get("mac") or "").strip()
    chip_type = str(info.get("chip_type") or "unknown")
    if not mac:
        raise RuntimeError("Could not read ESP MAC; run esp_chip_info() after checking connection.")
    row = cr.register_esp(mac=mac, chip_type=chip_type, port=port, details=info)
    _require_cpu_capability(row, capability)
    return row


# ---------------------------------------------------------------------------
# ── Probe management tools ───────────────────────────────────────────────
# ---------------------------------------------------------------------------

@mcp.tool()
def probe_list(include_ignored: bool = False) -> dict[str, Any]:
    """
    List all detected debug probes and available debugger backends.

    Args:
        include_ignored: When False (default), suppress probes in state=ignored
                         from the result so the listing stays focused on the
                         probes you actually use. Set True to also see the
                         ignored entries (e.g. before calling probe_forget).

    Returns:
        probes:   list of {serial, kind, model, nick, state, port, connected}
        pending:  serials of probes awaiting user approval
        backends: dict of available CLI tools (st-flash, cube, esptool, etc.)

    If any probe has state="pending", show the list to the user and ask them
    to call probe_approve() or probe_ignore() for each pending probe.
    The ProbeMonitor runs continuously — newly plugged probes appear automatically.
    """
    monitor = pd.get_monitor()
    connected_serials = {p.serial for p in monitor.connected_probes()}

    all_probes = pd.get_all_probes()
    pending: list[str] = []
    probe_list_out: list[dict[str, Any]] = []
    ignored_hidden = 0

    for p in all_probes:
        if not include_ignored and p.state == "ignored":
            ignored_hidden += 1
            continue
        entry = asdict(p)
        entry["connected"] = p.serial in connected_serials
        probe_list_out.append(entry)
        if p.state == "pending":
            pending.append(p.serial)

    # Surface probes that are live but not in the registry (e.g. CH340 chips
    # with no stable serial — registry persistence is suppressed for them).
    # If the same serial is already in the listing, leave that entry alone.
    listed_serials = {p["serial"] for p in probe_list_out}
    for cp in monitor.connected_probes():
        if cp.serial in listed_serials:
            continue
        entry = asdict(cp)
        entry["connected"] = True
        probe_list_out.append(entry)
        if cp.state == "pending":
            pending.append(cp.serial)

    backends = asdict(pd.check_backends())
    result: dict[str, Any] = {
        "probes":   probe_list_out,
        "pending":  pending,
        "backends": backends,
    }
    if ignored_hidden:
        result["ignored_hidden"] = ignored_hidden
    if pending:
        result["message"] = (
            f"{len(pending)} probe(s) await approval. "
            "Show the list to the user and call probe_approve(serial, nick) "
            "or probe_ignore(serial) for each."
        )
    return result


@mcp.tool()
def probe_approve(serial: str, nick: str = "") -> dict[str, Any]:
    """
    Approve a pending probe for use and optionally assign a nickname.

    Args:
        serial: Full probe serial string (from probe_list()).
        nick:   Short friendly name (e.g. 'devboard', 'nucleo', 'esp-cam').
                Leave empty to use the serial tail as identifier.

    Returns updated probe info on success.
    """
    probe = pd.approve_probe(serial, nick)
    if probe is None:
        # Probe not in registry yet — might be a freshly connected one
        # enumerate and register it first
        for live in pd.enumerate_all_probes():
            if live.serial == serial:
                pd._registry_upsert_probe(live)
                probe = pd.approve_probe(serial, nick)
                break

    if probe is None:
        return {"ok": False, "error": f"Probe {serial!r} not found. Call probe_list() first."}

    return {
        "ok": True,
        "probe": asdict(probe),
        "message": f"Probe {probe.serial[-8:]!r} approved"
                   + (f" as '{nick}'" if nick else ""),
    }


@mcp.tool()
def probe_ignore(serial: str) -> dict[str, Any]:
    """
    Ignore a probe — it will not appear in future probe_list() results.

    Args:
        serial: Full probe serial string (from probe_list()).
    """
    ok = pd.ignore_probe(serial)
    if not ok:
        return {"ok": False, "error": f"Probe {serial!r} not found"}
    return {"ok": True, "message": f"Probe {serial[-8:]!r} ignored"}


@mcp.tool()
def probe_rename(serial: str, nick: str) -> dict[str, Any]:
    """Rename an approved probe (update its nickname)."""
    ok = pd.rename_probe(serial, nick)
    if not ok:
        return {"ok": False, "error": f"Probe {serial!r} not found"}
    return {"ok": True, "message": f"Probe {serial[-8:]!r} renamed to '{nick}'"}


@mcp.tool()
def probe_forget(serial: str) -> dict[str, Any]:
    """
    Remove a probe from the registry entirely.

    The probe will re-appear as 'pending' next time it is connected.
    Useful to reset a wrongly-ignored probe.
    """
    ok = pd.clear_probe(serial)
    if not ok:
        return {"ok": False, "error": f"Probe {serial!r} not found in registry"}
    return {"ok": True, "message": f"Probe {serial[-8:]!r} removed from registry"}


@mcp.tool()
def probe_set_state(serial: str, state: str) -> dict[str, Any]:
    """
    Set probe lifecycle state.

    Allowed states: pending, approved, ignored, blocked, revoked.
    """
    try:
        probe = pd.set_probe_state(serial, state)
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc)}
    if not probe:
        return {"ok": False, "error": f"Probe {serial!r} not found"}
    return {"ok": True, "probe": asdict(probe)}


@mcp.tool()
def probe_set_permissions(
    serial: str,
    scan_allowed: Optional[bool] = None,
    read_allowed: Optional[bool] = None,
    flash_allowed: Optional[bool] = None,
    stop_allowed: Optional[bool] = None,
) -> dict[str, Any]:
    """Set per-action permissions for a probe entry."""
    probe = pd.set_probe_permissions(
        serial,
        scan_allowed=scan_allowed,
        read_allowed=read_allowed,
        flash_allowed=flash_allowed,
        stop_allowed=stop_allowed,
    )
    if not probe:
        return {"ok": False, "error": f"Probe {serial!r} not found"}
    return {"ok": True, "probe": asdict(probe)}


@mcp.tool()
def probe_info(serial: Optional[str] = None) -> dict[str, Any]:
    """
    Read target chip info for the connected MCU.

    Tries st-info first (fast), falls back to CubeProgrammer if not available.

    Args:
        serial: ST-Link probe serial (optional — uses first found if omitted).
    """
    resolved_serial = _resolve_approved_stlink_serial(serial, capability="read")
    errors: list[str] = []
    # Try st-info
    try:
        info = stlink.chip_info(resolved_serial)
        info["backend"] = "st-info"
        _register_and_require_stm32_cpu(info, resolved_serial, capability="read")
        return info
    except RuntimeError as exc:
        errors.append(f"st-info: {exc}")

    # Try CubeProgrammer
    try:
        info = cube.probe_info(resolved_serial)
        info["backend"] = "cube"
        _register_and_require_stm32_cpu(info, resolved_serial, capability="read")
        return info
    except RuntimeError as exc:
        errors.append(f"cube: {exc}")

    raise RuntimeError(
        "Could not read target info from any available backend. "
        + " | ".join(errors)
    )


@mcp.tool()
def system_permissions_check() -> dict[str, Any]:
    """
    Diagnose USB access permissions for ST-Link debug probes.

    Checks udev rules, group membership (plugdev/dialout), and direct
    /dev/bus/usb read access. Returns actionable fix instructions if
    any issues are found.

    Run this if probe_list() returns no connected probes despite devices
    being physically attached, or if st-info / st-flash report libusb
    error -3 (ACCESS) or -4.

    The standard fix on Linux:
      1. Install udev rules (stlink project or awto installer)
      2. Add user to the plugdev group
      3. Replug devices

    Upstream rules:
      https://github.com/stlink-org/stlink/tree/master/config/udev/rules.d
    """
    from dataclasses import asdict as _asdict
    perms = pd.check_usb_permissions()
    return _asdict(perms)


@mcp.tool()
def probe_scan_all() -> dict[str, Any]:
    """
    Perform a full scan: enumerate all connected USB probes and cross-reference
    with the registry and CPU registry.

    Unlike probe_list() (which returns registry state), this does a live USB
    scan via sysfs (no device-open permission needed) so real serial numbers
    are always resolved correctly.

    Returns:
        connected:   list of currently attached probes with registry state
        registry:    all probes ever seen (including disconnected)
        cpus:        all registered CPUs
        backends:    available CLI tools
        permissions: USB permission health summary
    """
    from dataclasses import asdict as _asdict

    # Live USB scan — uses sysfs so no special perms needed for serial resolution
    live_probes = pd.enumerate_all_probes()
    live_by_serial = {p.serial: p for p in live_probes}

    # Registry (all ever-seen probes)
    registry_probes = pd.get_all_probes()
    registry_by_serial = {p.serial: p for p in registry_probes}

    # Merge: annotate live probes with registry state/nick
    connected_out: list[dict[str, Any]] = []
    for serial, live in live_by_serial.items():
        entry = _asdict(live)
        if serial in registry_by_serial:
            reg = registry_by_serial[serial]
            entry["state"] = reg.state
            entry["nick"] = reg.nick
        entry["connected"] = True
        connected_out.append(entry)

    # Annotate registry probes with connected status
    registry_out: list[dict[str, Any]] = []
    for p in registry_probes:
        entry = _asdict(p)
        entry["connected"] = p.serial in live_by_serial
        registry_out.append(entry)

    # CPU registry
    cpus = cr.get_all_cpus()

    # Quick permission health (no device open)
    perms = pd.check_usb_permissions()
    perm_summary = {
        "ok": perms.ok,
        "in_plugdev": perms.in_plugdev,
        "udev_rules_count": len(perms.udev_rules_files),
        "blocked_devices": perms.stlink_devices_blocked,
        "issues": perms.issues,
    }

    result: dict[str, Any] = {
        "connected": connected_out,
        "registry": registry_out,
        "cpus": cpus,
        "backends": _asdict(pd.check_backends()),
        "permissions": perm_summary,
    }

    pending_probes = [p["serial"] for p in registry_out if p.get("state") == "pending"]
    if pending_probes:
        result["message"] = (
            f"{len(pending_probes)} probe(s) await approval — "
            "call probe_approve(serial, nick) or probe_ignore(serial) for each."
        )
    if not perms.ok:
        result.setdefault("message", "")
        result["message"] += (
            " USB permission issues detected — call system_permissions_check() for details."
        )

    return result


# ---------------------------------------------------------------------------
# ── CPU registry tools ─────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

@mcp.tool()
def cpu_list(kind: Optional[str] = None) -> dict[str, Any]:
    """List registered CPUs and show which entries await approval."""
    cpus = cr.list_cpus(kind=kind)
    pending = [row.get("id") for row in cpus if row.get("state") == "pending"]
    result: dict[str, Any] = {
        "cpus": cpus,
        "pending": pending,
    }
    if pending:
        result["message"] = (
            f"{len(pending)} CPU entry(s) await approval. "
            "Show the list to the user and call cpu_approve(cpu_id) or cpu_ignore(cpu_id)."
        )
    return result


@mcp.tool()
def cpu_approve(cpu_id: str) -> dict[str, Any]:
    """Approve a CPU entry for use."""
    row = cr.approve_cpu(cpu_id)
    if not row:
        return {"ok": False, "error": f"CPU entry {cpu_id!r} not found"}
    return {"ok": True, "cpu": row}


@mcp.tool()
def cpu_ignore(cpu_id: str) -> dict[str, Any]:
    """Ignore a CPU entry."""
    ok = cr.ignore_cpu(cpu_id)
    if not ok:
        return {"ok": False, "error": f"CPU entry {cpu_id!r} not found"}
    return {"ok": True, "cpu_id": cpu_id}


@mcp.tool()
def cpu_forget(cpu_id: str) -> dict[str, Any]:
    """Remove a CPU entry so it can be re-discovered."""
    ok = cr.clear_cpu(cpu_id)
    if not ok:
        return {"ok": False, "error": f"CPU entry {cpu_id!r} not found"}
    return {"ok": True, "cpu_id": cpu_id}


@mcp.tool()
def cpu_set_state(cpu_id: str, state: str) -> dict[str, Any]:
    """
    Set CPU lifecycle state.

    Allowed states: pending, approved, ignored, blocked, revoked.
    """
    try:
        row = cr.set_cpu_state(cpu_id, state)
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc)}
    if not row:
        return {"ok": False, "error": f"CPU entry {cpu_id!r} not found"}
    return {"ok": True, "cpu": row}


@mcp.tool()
def cpu_set_permissions(
    cpu_id: str,
    scan_allowed: Optional[bool] = None,
    read_allowed: Optional[bool] = None,
    flash_allowed: Optional[bool] = None,
    stop_allowed: Optional[bool] = None,
) -> dict[str, Any]:
    """Set per-action permissions for a CPU entry."""
    row = cr.set_cpu_permissions(
        cpu_id,
        scan_allowed=scan_allowed,
        read_allowed=read_allowed,
        flash_allowed=flash_allowed,
        stop_allowed=stop_allowed,
    )
    if not row:
        return {"ok": False, "error": f"CPU entry {cpu_id!r} not found"}
    return {"ok": True, "cpu": row}


# ---------------------------------------------------------------------------
# ── Managed workflow tools (intent-level control plane) ───────────────────
# ---------------------------------------------------------------------------

@mcp.tool()
def debug_session_start(
    target_kind: str = "stlink",
    serial: Optional[str] = None,
    response_mode: str = "compact",
    deep_debug: bool = False,
) -> dict[str, Any]:
    """
    Start a managed debug session bound to a specific probe serial.

    This is the preferred entrypoint for model-driven flows where the server
    should own sequencing and safety policy.
    """
    resolved_serial = _resolve_approved_stlink_serial(serial, capability="read")
    target_info = stlink.chip_info(resolved_serial)
    _register_and_require_stm32_cpu(target_info, resolved_serial, capability="read")
    return wf.start_session(
        target_kind=target_kind,
        serial=resolved_serial,
        response_mode=response_mode,
        deep_debug=deep_debug,
    )


@mcp.tool()
def debug_session_set_mode(
    session_id: str,
    response_mode: str = "compact",
    deep_debug: bool = False,
) -> dict[str, Any]:
    """
    Configure token/detail behavior for a managed session.

    response_mode='compact' keeps payloads concise. Set deep_debug=true to
    force full-detail outputs for diagnostics.
    """
    return wf.set_session_mode(
        session_id=session_id,
        response_mode=response_mode,
        deep_debug=deep_debug,
    )


@mcp.tool()
def debug_session_status(session_id: str, detail_level: str = "compact") -> dict[str, Any]:
    """Return the current state and event history for a managed debug session."""
    return wf.session_status(session_id=session_id, detail_level=detail_level)


@mcp.tool()
def debug_session_memory_snapshot(
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
    """
    Capture a managed memory snapshot using the session's bound probe serial.

    The server decides low-level sequencing and records an event in session
    history for traceability.
    """
    return wf.session_memory_snapshot(
        session_id=session_id,
        output_dir=output_dir,
        flash_address=flash_address,
        flash_length=flash_length,
        ram_address=ram_address,
        ram_length=ram_length,
        include_flash=include_flash,
        include_ram=include_ram,
        detail_level=detail_level,
    )


@mcp.tool()
def debug_session_safe_flash_cycle(
    session_id: str,
    output_dir: str,
    confirm_destructive: bool,
    flash_address: str = "0x08000000",
    flash_length: Optional[int] = None,
    detail_level: Optional[str] = None,
) -> dict[str, Any]:
    """
    Run safe destructive flash workflow with explicit safety confirmation.

    Sequence: backup -> erase -> read erased -> restore -> verify -> reset.
    OTP / option-byte writes are out-of-scope for this workflow.
    """
    return wf.session_safe_flash_cycle(
        session_id=session_id,
        output_dir=output_dir,
        confirm_destructive=confirm_destructive,
        flash_address=flash_address,
        flash_length=flash_length,
        detail_level=detail_level,
    )


@mcp.tool()
def debug_session_report(session_id: str, output_path: str) -> dict[str, Any]:
    """Write a machine-readable handoff report for the managed debug session."""
    return wf.session_report(session_id=session_id, output_path=output_path)


@mcp.tool()
def debug_parallel_flash_program(
    serials: list[str],
    firmware_path: str,
    address: str = "0x8000000",
    reset: bool = True,
    max_workers: int = 4,
    continue_on_error: bool = True,
    detail_level: str = "compact",
) -> dict[str, Any]:
    """
    Program multiple ST-Link targets concurrently.

    Use this for multi-target bring-up to reduce iterative retries and tool
    round-trips.
    """
    return wf.parallel_flash_program(
        serials=serials,
        firmware_path=firmware_path,
        address=address,
        reset=reset,
        max_workers=max_workers,
        continue_on_error=continue_on_error,
        detail_level=detail_level,
    )


@mcp.tool()
def debug_user_action_request(
    command: str,
    reason: str,
    expected_output: str = "",
    request_origin: str = wf.REQUEST_ORIGIN_DEFAULT,
) -> dict[str, Any]:
    """
    Build a structured payload that asks the user to run a command manually.

    Intended for cases where local/manual interaction is cheaper or required.
    """
    return wf.build_user_action_request(
        command=command,
        reason=reason,
        expected_output=expected_output,
        request_origin=request_origin,
    )


# ---------------------------------------------------------------------------
# ── ST-Link open-source tools ────────────────────────────────────────────
# ---------------------------------------------------------------------------

@mcp.tool()
def stlink_probe_list() -> list[dict[str, Any]]:
    """
    List attached ST-Link probes using st-info --probe.

    Returns list of {serial, firmware, voltage, chip_id, target_description}.
    Returns empty list if no probes found (no error).
    """
    return stlink.probe_list()


@mcp.tool()
def stlink_flash(
    firmware: str,
    address: str = "0x8000000",
    serial: Optional[str] = None,
    reset: bool = True,
) -> str:
    """
    Flash firmware using st-flash (open-source stlink tools).

    Args:
        firmware: Path to .hex or .bin file.
        address:  Flash base address (default 0x8000000). Ignored for .hex files.
        serial:   ST-Link serial (optional — uses first found if omitted).
        reset:    Reset target after flashing (default True).

    Returns status string on success.
    """
    resolved_serial = _resolve_approved_stlink_serial(serial, capability="flash")
    _register_and_require_stm32_cpu(
        stlink.chip_info(resolved_serial),
        resolved_serial,
        capability="flash",
    )
    return stlink.flash_write(firmware, address=address, serial=resolved_serial, reset=reset)


@mcp.tool()
def stlink_erase(serial: Optional[str] = None) -> str:
    """Mass-erase the target flash using st-flash."""
    resolved_serial = _resolve_approved_stlink_serial(serial, capability="flash")
    _register_and_require_stm32_cpu(
        stlink.chip_info(resolved_serial),
        resolved_serial,
        capability="flash",
    )
    return stlink.flash_erase(resolved_serial)


@mcp.tool()
def stlink_read(
    output_path: str,
    address: str,
    length: int,
    serial: Optional[str] = None,
) -> str:
    """
    Read a flash region to a binary file using st-flash.

    Args:
        output_path: Destination .bin file path.
        address:     Start address (e.g. '0x8000000').
        length:      Number of bytes.
        serial:      ST-Link serial (optional).
    """
    resolved_serial = _resolve_approved_stlink_serial(serial, capability="read")
    _register_and_require_stm32_cpu(
        stlink.chip_info(resolved_serial),
        resolved_serial,
        capability="read",
    )
    return stlink.flash_read(output_path, address, length, resolved_serial)


@mcp.tool()
def stm32_program(
    elf_or_bin: str,
    serial: Optional[str] = None,
    force_full: bool = False,
    freq: Optional[int] = None,
    connect_under_reset: bool = True,
    fail_fast: bool = False,
    allow_fallback_freq: bool = False,
    no_mode_check: bool = False,
) -> dict[str, Any]:
    """
    Program a target with STM32CubeProgrammer (cube-primary, with full
    reliability stack: USB-spawn flock, fast-fail libusb error detection,
    optional SWD freq fallback, connect-under-reset, sector-0 erase recovery,
    and USB-reset retry).

    This is the canonical write path. Prefer this over cube_flash() for any
    flash-and-recover workflow; cube_flash() remains as a thin one-shot.

    Args:
        elf_or_bin:          Path to ELF (preferred) or BIN file.
        serial:              ST-Link serial (optional — uses sole approved probe).
        force_full:          Force full chip program instead of incremental.
        freq:                SWD frequency in kHz (default: max 24000).
        connect_under_reset: Use UR/HWrst connect (default true; safer).
        fail_fast:           Skip retries (full-flash + USB-reset) on first failure.
        allow_fallback_freq: Permit slow 8000 kHz recovery retry.
                             Off by default — fallback signals a serious link issue.
        no_mode_check:       Skip ELF/board build-mode safety check.

    Returns: {"ok": bool, "elapsed_s": float, "elf": ..., "serial": ...}.
    """
    import time
    from awto_debug import programmer as awto_programmer
    from awto_debug.usb import find_probes

    resolved_serial = _resolve_approved_stlink_serial(serial, capability="flash")
    _register_and_require_stm32_cpu(
        cube.probe_info(resolved_serial),
        resolved_serial,
        capability="flash",
    )
    probe = next((p for p in find_probes() if p.serial == resolved_serial), None)
    if probe is None:
        raise RuntimeError(f"Probe {resolved_serial!r} not found via USB enumeration")

    t0 = time.monotonic()
    success, elapsed = awto_programmer.program_with_recovery(
        elf_or_bin,
        probe,
        force_full=force_full,
        freq=freq,
        connect_under_reset=connect_under_reset,
        fail_fast=fail_fast,
        allow_fallback_freq=allow_fallback_freq,
        no_mode_check=no_mode_check,
        verbose=True,
        passthrough=False,
        timestamps=False,
        include_sn=True,
        shared=False,
        allow_server_kill=True,
    )
    return {
        "ok": bool(success),
        "elapsed_s": round(elapsed, 2),
        "total_s": round(time.monotonic() - t0, 2),
        "elf": elf_or_bin,
        "serial": resolved_serial,
        "backend": "STM32CubeProgrammer",
    }


@mcp.tool()
def stm32_erase(
    serial: Optional[str] = None,
    sector0_only: bool = False,
    freq: Optional[int] = None,
) -> dict[str, Any]:
    """
    Erase target flash via STM32CubeProgrammer (mass-erase by default).

    Set sector0_only=True for a fast targeted sector-0 erase under reset
    (the same recovery path used internally by stm32_program on retry).

    Returns {"ok": bool, "elapsed_s": float, "serial": ...}.
    """
    from awto_debug import programmer as awto_programmer
    from awto_debug.usb import find_probes

    resolved_serial = _resolve_approved_stlink_serial(serial, capability="flash")
    _register_and_require_stm32_cpu(
        cube.probe_info(resolved_serial),
        resolved_serial,
        capability="flash",
    )
    probe = next((p for p in find_probes() if p.serial == resolved_serial), None)
    if probe is None:
        raise RuntimeError(f"Probe {resolved_serial!r} not found via USB enumeration")

    if sector0_only:
        ok = awto_programmer.erase_sector0(probe, include_sn=True)
        return {"ok": bool(ok), "mode": "sector0", "serial": resolved_serial}

    success, elapsed = awto_programmer.erase_device(
        probe,
        include_sn=True,
        shared=False,
        freq=freq,
        connect_under_reset=True,
    )
    return {
        "ok": bool(success),
        "elapsed_s": round(elapsed, 2),
        "mode": "mass",
        "serial": resolved_serial,
        "backend": "STM32CubeProgrammer",
    }


@mcp.tool()
def stm32_verify(
    firmware: str,
    serial: Optional[str] = None,
    address: str = "0x08000000",
) -> dict[str, Any]:
    """
    Verify on-target flash content against a local file by reading the same
    region back via STM32CubeProgrammer and byte-comparing.

    There is no native verify-only mode in STM32_Programmer_CLI; this tool
    implements verify on top of cube_read_flash for reliability. For BIN files
    the size is determined from the file; for ELF files use stm32_program with
    a freshly-built ELF instead of verifying after the fact.

    Returns {"ok": bool, "matched_bytes": int, "mismatch_at": int|None}.
    """
    import os
    from pathlib import Path

    resolved_serial = _resolve_approved_stlink_serial(serial, capability="read")
    _register_and_require_stm32_cpu(
        cube.probe_info(resolved_serial),
        resolved_serial,
        capability="read",
    )

    fw_path = Path(firmware)
    if not fw_path.is_file():
        raise RuntimeError(f"firmware not found: {firmware}")
    if fw_path.suffix.lower() == ".elf":
        raise RuntimeError(
            "ELF verify not supported by this tool — use stm32_program with the "
            "fresh ELF (cube does an integral verify pass during programming)."
        )

    expected = fw_path.read_bytes()
    length = len(expected)

    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        cube.flash_read(tmp_path, address, length, resolved_serial)
        actual = Path(tmp_path).read_bytes()
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    if len(actual) < length:
        return {
            "ok": False,
            "error": f"short read: got {len(actual)} of {length} bytes",
            "matched_bytes": 0,
            "mismatch_at": 0,
        }

    actual = actual[:length]
    mismatch_at: Optional[int] = None
    for i, (a, b) in enumerate(zip(expected, actual)):
        if a != b:
            mismatch_at = i
            break

    return {
        "ok": mismatch_at is None,
        "matched_bytes": length if mismatch_at is None else mismatch_at,
        "mismatch_at": mismatch_at,
        "address": address,
        "length": length,
        "backend": "STM32CubeProgrammer",
    }


@mcp.tool()
def stm32_chip_info(serial: Optional[str] = None) -> dict[str, Any]:
    """
    Read enriched STM32 chip identity from the live target.

    Combines:
      • DBGMCU_IDCODE → device_id, name, revision, voltage, ST-Link FW
        (via STM32CubeProgrammer probe).
      • STM32CubeProgrammer Data_Base XML → series, CPU, F_SIZE address,
        flash variants, SRAM banks, OTP region, option-byte region,
        bootloader address, default flash size.
      • F_SIZE register → actual fitted flash in KB (authoritative).
      • Family-specific UID address → 96-bit unique device ID.
      • Package register (F4/F7/L4 only) → decoded package code.

    All on-chip memory reads go through ``STM32_Programmer_CLI -r32`` (cube),
    not st-flash, since st-flash UID readback is unreliable across families.
    """
    from awto_debug import stm32_db

    resolved_serial = _resolve_approved_stlink_serial(serial, capability="read")
    probe = cube.probe_info(resolved_serial)
    _register_and_require_stm32_cpu(probe, resolved_serial, capability="read")

    out: dict[str, Any] = {
        "serial":             resolved_serial,
        "device_id":          probe.get("device_id"),
        "device_name":        probe.get("device_name"),
        "revision_id":        probe.get("revision_id"),
        "board_name":         probe.get("board_name"),
        "voltage":            probe.get("voltage"),
        "cpu_freq_mhz":       probe.get("cpu_freq_mhz"),
        "bootloader_version": probe.get("bootloader_version"),
        "stlink_fw":          probe.get("stlink_fw"),
        "connect_mode":       probe.get("connect_mode"),
        "backend":            "STM32CubeProgrammer",
    }
    if probe.get("recovery_hint"):
        out["recovery_hint"] = probe["recovery_hint"]
    out["status"] = "ok" if probe.get("device_id") else "needs_power_cycle"

    db_entry = None
    devid_str = str(probe.get("device_id") or "").strip()
    if devid_str.lower().startswith("0x"):
        try:
            db_entry = stm32_db.lookup_device(int(devid_str, 16))
        except ValueError:
            db_entry = None
    if db_entry:
        out.update({
            "series":             db_entry["series"],
            "cpu":                db_entry["cpu"],
            "vendor":             db_entry["vendor"],
            "fsize_address":      f"0x{db_entry['fsize_address']:08X}" if db_entry["fsize_address"] else None,
            "fsize_default_kb":   (db_entry["fsize_default_bytes"] // 1024) if db_entry["fsize_default_bytes"] else None,
            "uid_address":        f"0x{db_entry['uid_address']:08X}" if db_entry["uid_address"] else None,
            "flash_base":         f"0x{db_entry['flash_base']:08X}" if db_entry["flash_base"] else None,
            "bootloader_address": f"0x{db_entry['bootloader_address']:08X}" if db_entry.get("bootloader_address") else None,
            "flash_variants":     db_entry["flash_variants"],
            "sram_regions":       db_entry.get("sram_regions") or [],
            "otp_regions":        db_entry.get("otp_regions") or [],
            "option_byte_regions": db_entry.get("option_byte_regions") or [],
            "package_register":   f"0x{db_entry['package_register']:08X}" if db_entry.get("package_register") else None,
            "db_path":            db_entry["db_path"],
        })

    fsize_addr = db_entry["fsize_address"] if db_entry else None
    uid_addr   = db_entry["uid_address"] if db_entry else None
    pkg_addr   = db_entry.get("package_register") if db_entry else None

    # F_SIZE: 16-bit value at fsize_addr giving fitted flash KB. Cube's -r32
    # reads at word granularity, so fetch one word and slice the right halfword
    # (F4 places F_SIZE at 0x1FFF7A22, an unaligned halfword within 0x1FFF7A20).
    if fsize_addr is not None:
        try:
            aligned = fsize_addr & ~0x3
            offset_bits = (fsize_addr - aligned) * 8
            word = cube.read_words(aligned, 1, resolved_serial)[0]
            out["flash_size_kb"] = (word >> offset_bits) & 0xFFFF
            out["flash_size_bytes"] = out["flash_size_kb"] * 1024
        except RuntimeError as exc:
            out["flash_size_error"] = str(exc)

    if uid_addr is not None:
        try:
            w0, w1, w2 = cube.read_words(uid_addr, 3, resolved_serial)
            out["uid_hex"] = f"{w2:08X}{w1:08X}{w0:08X}"
            out["uid_words"] = [f"0x{w2:08X}", f"0x{w1:08X}", f"0x{w0:08X}"]
            pd.set_probe_target_uid(resolved_serial, out["uid_hex"])
            siblings = pd.find_probes_by_target_uid(out["uid_hex"], exclude_serial=resolved_serial)
            if siblings:
                out["sibling_probes"] = siblings
        except RuntimeError as exc:
            out["uid_error"] = str(exc)

    if pkg_addr is not None:
        pkg_info = stm32_db.package_register(db_entry["series"]) or stm32_db.package_register(db_entry["name"])
        if pkg_info is not None:
            _addr, mask, table = pkg_info
            try:
                word = cube.read_words(pkg_addr, 1, resolved_serial)[0]
                code = word & mask
                out["package_raw"] = f"0x{word:08X}"
                out["package_code"] = f"0x{code:02X}"
                out["package"] = table.get(code, "unknown")
            except RuntimeError as exc:
                out["package_error"] = str(exc)

    return out


@mcp.tool()
def stlink_memory_snapshot(
    output_dir: str,
    serial: Optional[str] = None,
    flash_address: str = "0x08000000",
    flash_length: Optional[int] = None,
    ram_address: str = "0x20000000",
    ram_length: Optional[int] = None,
    include_flash: bool = True,
    include_ram: bool = True,
) -> dict[str, Any]:
    """
    Dump one contiguous internal flash range and one contiguous RAM range to files.

    If flash_length / ram_length are omitted, the tool infers the sizes from
    st-info --probe for the selected target. RAM capture defaults to one range
    starting at ram_address and does not automatically include extra SRAM banks
    or external SPI/QSPI RAM.
    """
    resolved_serial = _resolve_approved_stlink_serial(serial, capability="read")
    _register_and_require_stm32_cpu(
        stlink.chip_info(resolved_serial),
        resolved_serial,
        capability="read",
    )
    return stlink.dump_memory_snapshot(
        output_dir=output_dir,
        serial=resolved_serial,
        flash_address=flash_address,
        flash_length=flash_length,
        ram_address=ram_address,
        ram_length=ram_length,
        include_flash=include_flash,
        include_ram=include_ram,
    )


@mcp.tool()
def stlink_verify(firmware: str, serial: Optional[str] = None) -> str:
    """Verify flash contents against a firmware file using st-flash."""
    resolved_serial = _resolve_approved_stlink_serial(serial, capability="flash")
    _register_and_require_stm32_cpu(
        stlink.chip_info(resolved_serial),
        resolved_serial,
        capability="flash",
    )
    return stlink.flash_verify(firmware, resolved_serial)


@mcp.tool()
def stlink_reset(serial: Optional[str] = None) -> str:
    """Reset the target MCU via ST-Link."""
    resolved_serial = _resolve_approved_stlink_serial(serial, capability="stop")
    _register_and_require_stm32_cpu(
        stlink.chip_info(resolved_serial),
        resolved_serial,
        capability="stop",
    )
    return stlink.reset_target(resolved_serial)


@mcp.tool()
def stlink_gdb_start(port: int = 4242, serial: Optional[str] = None) -> dict[str, Any]:
    """
    Start an st-util GDB server.

    Args:
        port:   TCP port to listen on (default 4242).
        serial: ST-Link serial (optional).

    Returns: {handle_id, port, pid, tag} — pass handle_id to stlink_gdb_stop().
    After starting, call gdb_connect(port=PORT) to connect the GDB client.
    """
    resolved_serial = _resolve_approved_stlink_serial(serial, capability="read")
    _register_and_require_stm32_cpu(
        stlink.chip_info(resolved_serial),
        resolved_serial,
        capability="read",
    )
    handle = stlink.gdb_server_start(port=port, serial=resolved_serial)
    return handle.as_dict()


@mcp.tool()
def stlink_gdb_stop(handle_id: str) -> str:
    """Stop a running st-util GDB server. Args: handle_id from stlink_gdb_start()."""
    return stlink.gdb_server_stop(handle_id)


@mcp.tool()
def stlink_trace_start(freq: int = 4000000, serial: Optional[str] = None) -> dict[str, Any]:
    """
    Start st-trace SWO capture.

    Args:
        freq:   SWO clock frequency in Hz (default 4 MHz).
        serial: ST-Link serial (optional).
    """
    resolved_serial = _resolve_approved_stlink_serial(serial, capability="read")
    _register_and_require_stm32_cpu(
        stlink.chip_info(resolved_serial),
        resolved_serial,
        capability="read",
    )
    handle = stlink.trace_start(freq=freq, serial=resolved_serial)
    return handle.as_dict()


@mcp.tool()
def stlink_trace_stop(handle_id: str) -> str:
    """Stop a running st-trace capture."""
    return stlink.trace_stop(handle_id)


# ---------------------------------------------------------------------------
# ── STM32CubeProgrammer tools ────────────────────────────────────────────
# ---------------------------------------------------------------------------

@mcp.tool()
def cube_flash(
    firmware: str,
    serial: Optional[str] = None,
    verify: bool = True,
    reset: bool = True,
    address: Optional[str] = None,
) -> str:
    """
    Flash firmware using STM32CubeProgrammer.

    Supports .hex, .bin, .elf, .srec formats.

    Args:
        firmware: Path to firmware file.
        serial:   ST-Link serial (optional).
        verify:   Verify flash after write (default True).
        reset:    Hard-reset target after flash (default True).
        address:  Write address for .bin files (e.g. "0x08000000"). Required
                  by cube for raw binaries; auto-defaults to 0x08000000 if
                  omitted. Ignored for .hex/.elf/.srec.
    """
    resolved_serial = _resolve_approved_stlink_serial(serial, capability="flash")
    _register_and_require_stm32_cpu(
        cube.probe_info(resolved_serial),
        resolved_serial,
        capability="flash",
    )
    return cube.flash_write(
        firmware,
        serial=resolved_serial,
        verify=verify,
        reset=reset,
        address=address,
    )


@mcp.tool()
def cube_erase(serial: Optional[str] = None) -> str:
    """Mass-erase target flash via STM32CubeProgrammer."""
    resolved_serial = _resolve_approved_stlink_serial(serial, capability="flash")
    _register_and_require_stm32_cpu(
        cube.probe_info(resolved_serial),
        resolved_serial,
        capability="flash",
    )
    return cube.flash_erase(resolved_serial)


@mcp.tool()
def cube_info(serial: Optional[str] = None) -> dict[str, Any]:
    """Read target device info via STM32CubeProgrammer (device_id, flash size, UID, etc.)."""
    resolved_serial = _resolve_approved_stlink_serial(serial, capability="read")
    info = cube.probe_info(resolved_serial)
    _register_and_require_stm32_cpu(info, resolved_serial, capability="read")
    return info


@mcp.tool()
def cube_read_uid(serial: Optional[str] = None) -> str:
    """Read the 96-bit STM32 unique device ID via CubeProgrammer."""
    resolved_serial = _resolve_approved_stlink_serial(serial, capability="read")
    _register_and_require_stm32_cpu(
        cube.probe_info(resolved_serial),
        resolved_serial,
        capability="read",
    )
    return cube.read_uid(resolved_serial)


@mcp.tool()
def cube_otp_read(serial: Optional[str] = None) -> str:
    """Read option bytes / OTP area via CubeProgrammer."""
    resolved_serial = _resolve_approved_stlink_serial(serial, capability="read")
    _register_and_require_stm32_cpu(
        cube.probe_info(resolved_serial),
        resolved_serial,
        capability="read",
    )
    return cube.read_otp(resolved_serial)


@mcp.tool()
def cube_otp_dump(output_path: str, serial: Optional[str] = None) -> dict[str, Any]:
    """Read option bytes / OTP text via CubeProgrammer and save it to a file."""
    resolved_serial = _resolve_approved_stlink_serial(serial, capability="read")
    _register_and_require_stm32_cpu(
        cube.probe_info(resolved_serial),
        resolved_serial,
        capability="read",
    )
    return cube.dump_otp(output_path, resolved_serial)


@mcp.tool()
def cube_connection_properties(
    serial: Optional[str] = None,
    freq: int = 8000,
    under_reset: bool = False,
) -> dict[str, Any]:
    """
    Show the CubeProgrammer connection arguments for this target.

    Use under_reset=True to inspect the connect-under-reset profile.
    """
    resolved_serial = _resolve_approved_stlink_serial(serial, capability="read")
    _register_and_require_stm32_cpu(
        cube.probe_info(resolved_serial),
        resolved_serial,
        capability="read",
    )
    return cube.connection_properties(serial=resolved_serial, freq=freq, under_reset=under_reset)


@mcp.tool()
def cube_recover(serial: Optional[str] = None) -> str:
    """
    Attempt full-chip recovery using connect-under-reset + mass erase.

    Use when the target is locked or unresponsive to normal connection.
    """
    resolved_serial = _resolve_approved_stlink_serial(serial, capability="flash")
    _register_and_require_stm32_cpu(
        cube.probe_info(resolved_serial),
        resolved_serial,
        capability="flash",
    )
    return cube.recover(resolved_serial)


@mcp.tool()
def cube_read_flash(
    output_path: str,
    address: str,
    length: int,
    serial: Optional[str] = None,
) -> str:
    """
    Read a flash region to a binary file via STM32CubeProgrammer.

    Args:
        output_path: Destination .bin file.
        address:     Start address (e.g. '0x08000000').
        length:      Number of bytes.
        serial:      ST-Link serial (optional).
    """
    resolved_serial = _resolve_approved_stlink_serial(serial, capability="read")
    _register_and_require_stm32_cpu(
        cube.probe_info(resolved_serial),
        resolved_serial,
        capability="read",
    )
    return cube.flash_read(output_path, address, length, resolved_serial)


@mcp.tool()
def cube_gdb_start(
    port: int = 61234,
    serial: Optional[str] = None,
    freq: int = 8000,
) -> dict[str, Any]:
    """
    Start a GDB server via STM32CubeProgrammer (ST-LINK_gdbserver or cube stlink-gdbserver).

    Args:
        port:   TCP port (default 61234).
        serial: ST-Link serial (optional).
        freq:   SWD frequency in kHz (default 8000).

    Returns: {handle_id, port, pid, tag} — pass handle_id to cube_gdb_stop().
    After starting, call gdb_connect(port=PORT) to connect the GDB client.
    """
    resolved_serial = _resolve_approved_stlink_serial(serial, capability="read")
    _register_and_require_stm32_cpu(
        cube.probe_info(resolved_serial),
        resolved_serial,
        capability="read",
    )
    handle = cube.gdb_server_start(port=port, serial=resolved_serial, freq=freq)
    return handle.as_dict()


@mcp.tool()
def cube_gdb_stop(handle_id: str) -> str:
    """Stop a running CubeProgrammer GDB server."""
    return cube.gdb_server_stop(handle_id)


# ---------------------------------------------------------------------------
# ── GDB client tools ─────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

@mcp.tool()
def gdb_connect(port: int = 4242, host: str = "localhost") -> str:
    """
    Connect the GDB/MI client to a running GDB server.

    Call stlink_gdb_start() or cube_gdb_start() first, then gdb_connect().

    Args:
        port: TCP port the GDB server is listening on (default 4242).
        host: Hostname (default localhost).
    """
    return _gdb().connect(port=port, host=host)


@mcp.tool()
def gdb_disconnect() -> str:
    """Disconnect the GDB/MI client from the GDB server."""
    return _gdb().disconnect()


@mcp.tool()
def gdb_status() -> dict[str, Any]:
    """Return current GDB client connection status."""
    return _gdb().status()


@mcp.tool()
def gdb_halt() -> str:
    """Halt (interrupt) target execution. Required before reading memory or registers."""
    return _gdb().halt()


@mcp.tool()
def gdb_continue() -> str:
    """Resume target execution."""
    return _gdb().continue_()


@mcp.tool()
def gdb_step() -> str:
    """Single-step the target (step into one instruction)."""
    return _gdb().step()


@mcp.tool()
def gdb_step_over() -> str:
    """Step over one source line (next)."""
    return _gdb().step_over()


@mcp.tool()
def gdb_read_memory(address: str, length: int) -> str:
    """
    Read memory from the target as a hex dump.

    Args:
        address: Memory address (e.g. '0x20000000').
        length:  Number of bytes to read.

    Call gdb_halt() first if the target is running.
    """
    return _gdb().read_memory(address, length)


@mcp.tool()
def gdb_write_memory(address: str, hex_data: str) -> str:
    """
    Write bytes to target memory.

    Args:
        address:  Target address (e.g. '0x20000100').
        hex_data: Hex string without spaces (e.g. 'deadbeef01020304').
    """
    return _gdb().write_memory(address, hex_data)


@mcp.tool()
def gdb_read_registers() -> dict[str, str]:
    """
    Read all CPU registers from the target.

    Returns dict of register_name → hex value string.
    Call gdb_halt() first if the target is running.
    """
    return _gdb().read_registers()


@mcp.tool()
def gdb_set_breakpoint(location: str) -> dict[str, Any]:
    """
    Set a breakpoint at a function name or address.

    Args:
        location: Symbol name (e.g. 'main', 'HAL_Init') or address ('0x08001234').

    Returns: {number, address, function, file, line}
    """
    return _gdb().set_breakpoint(location)


@mcp.tool()
def gdb_delete_breakpoint(number: int) -> str:
    """Delete a breakpoint by number (from gdb_set_breakpoint result)."""
    return _gdb().delete_breakpoint(number)


@mcp.tool()
def gdb_list_breakpoints() -> list[dict[str, Any]]:
    """List all current breakpoints."""
    return _gdb().list_breakpoints()


# ---------------------------------------------------------------------------
# ── Process management tools ─────────────────────────────────────────────
# ---------------------------------------------------------------------------

@mcp.tool()
def processes_list() -> list[dict[str, Any]]:
    """List all managed background processes (GDB servers, OpenOCD, monitors)."""
    return _mgr().list_all()


@mcp.tool()
def process_stop(handle_id: str) -> str:
    """
    Stop any managed background process by handle ID.

    Args:
        handle_id: UUID from stlink_gdb_start(), cube_gdb_start(),
                   idf_monitor_start(), idf_openocd_start(), etc.
    """
    rc = _mgr().stop(handle_id)
    if rc is None:
        return f"Handle {handle_id!r} not found"
    return f"Process stopped (rc={rc})"


# ---------------------------------------------------------------------------
# ── Espressif / esptool tools ────────────────────────────────────────────
# ---------------------------------------------------------------------------

@mcp.tool()
def esp_chip_info(port: Optional[str] = None, baud: int = 460800) -> dict[str, Any]:
    """
    Read ESP chip info (chip type, features, MAC address).

    Args:
        port: Serial port (e.g. '/dev/ttyUSB0'). Auto-detect if omitted.
        baud: Baud rate for esptool connection (default 460800).
    """
    resolved_port = _resolve_approved_esp_port(port, capability="read")
    info = esp.chip_info(port=resolved_port, baud=baud)
    _register_and_require_esp_cpu(info, resolved_port, capability="read")
    return info


@mcp.tool()
def esp_flash_id(port: Optional[str] = None) -> dict[str, Any]:
    """Read ESP flash manufacturer and size information."""
    resolved_port = _resolve_approved_esp_port(port, capability="read")
    info = esp.chip_info(port=resolved_port, baud=460800)
    _register_and_require_esp_cpu(info, resolved_port, capability="read")
    return esp.flash_id(port=resolved_port)


@mcp.tool()
def esp_flash_write(
    firmware: str,
    port: Optional[str] = None,
    baud: int = 460800,
    offset: str = "0x0",
    chip: Optional[str] = None,
) -> str:
    """
    Write a binary firmware file to ESP flash.

    Args:
        firmware: Path to .bin file.
        port:     Serial port (auto-detect if omitted).
        baud:     Baud rate (default 460800).
        offset:   Flash offset (default '0x0').
        chip:     Chip override (e.g. 'esp32', 'esp32s3'). Auto-detect if omitted.
    """
    resolved_port = _resolve_approved_esp_port(port, capability="flash")
    info = esp.chip_info(port=resolved_port, baud=baud)
    _register_and_require_esp_cpu(info, resolved_port, capability="flash")
    return esp.flash_write(firmware, port=resolved_port, baud=baud, offset=offset, chip=chip)


@mcp.tool()
def esp_flash_erase(port: Optional[str] = None, chip: Optional[str] = None) -> str:
    """Erase entire ESP flash chip."""
    resolved_port = _resolve_approved_esp_port(port, capability="flash")
    info = esp.chip_info(port=resolved_port, baud=460800)
    _register_and_require_esp_cpu(info, resolved_port, capability="flash")
    return esp.flash_erase(port=resolved_port, chip=chip)


@mcp.tool()
def esp_flash_read(
    output_path: str,
    offset: str = "0x0",
    length: int = 0x400000,
    port: Optional[str] = None,
) -> str:
    """
    Read ESP flash region to a binary file.

    Args:
        output_path: Destination .bin file.
        offset:      Start offset (default '0x0').
        length:      Bytes to read (default 4 MB).
        port:        Serial port (auto-detect if omitted).
    """
    resolved_port = _resolve_approved_esp_port(port, capability="read")
    info = esp.chip_info(port=resolved_port, baud=460800)
    _register_and_require_esp_cpu(info, resolved_port, capability="read")
    return esp.flash_read(output_path, offset=offset, length=length, port=resolved_port)


@mcp.tool()
def esp_reset(port: Optional[str] = None, mode: str = "normal") -> str:
    """
    Reset the ESP chip.

    Args:
        port: Serial port (auto-detect if omitted).
        mode: 'normal' (run firmware) or 'bootloader' (enter download mode).
    """
    resolved_port = _resolve_approved_esp_port(port, capability="stop")
    info = esp.chip_info(port=resolved_port, baud=460800)
    _register_and_require_esp_cpu(info, resolved_port, capability="stop")
    return esp.reset_chip(port=resolved_port, mode=mode)


# ---------------------------------------------------------------------------
# ── Espressif / idf.py tools ─────────────────────────────────────────────
# ---------------------------------------------------------------------------

@mcp.tool()
def idf_build(project_path: Optional[str] = None) -> str:
    """
    Build an ESP-IDF project with idf.py build.

    Args:
        project_path: Project root directory (defaults to current directory).
    """
    return esp.idf_build(project_path)


@mcp.tool()
def idf_flash(
    project_path: Optional[str] = None,
    port: Optional[str] = None,
    baud: int = 460800,
) -> str:
    """
    Build and flash an ESP-IDF project with idf.py flash.

    Args:
        project_path: Project root directory.
        port:         Serial port (auto-detect if omitted).
        baud:         Flash baud rate (default 460800).
    """
    resolved_port = _resolve_approved_esp_port(port, capability="flash")
    info = esp.chip_info(port=resolved_port, baud=baud)
    _register_and_require_esp_cpu(info, resolved_port, capability="flash")
    return esp.idf_flash(project_path=project_path, port=resolved_port, baud=baud)


@mcp.tool()
def idf_size(project_path: Optional[str] = None) -> str:
    """Report binary sizes for an ESP-IDF project (idf.py size)."""
    return esp.idf_size(project_path)


@mcp.tool()
def idf_monitor_start(
    project_path: Optional[str] = None,
    port: Optional[str] = None,
    baud: int = 115200,
) -> dict[str, Any]:
    """
    Start idf.py monitor as a background process.

    Args:
        project_path: Project root (optional).
        port:         Serial port (auto-detect if omitted).
        baud:         Monitor baud rate (default 115200).

    Returns: {handle_id, ...} — pass handle_id to idf_monitor_stop().
    """
    resolved_port = _resolve_approved_esp_port(port, capability="read")
    info = esp.chip_info(port=resolved_port, baud=baud)
    _register_and_require_esp_cpu(info, resolved_port, capability="read")
    handle = esp.idf_monitor_start(project_path=project_path, port=resolved_port, baud=baud)
    return handle.as_dict()


@mcp.tool()
def idf_monitor_stop(handle_id: str) -> str:
    """Stop a running idf.py monitor process."""
    return esp.idf_monitor_stop(handle_id)


@mcp.tool()
def idf_openocd_start(
    config: Optional[str] = None,
    gdb_port: int = 3333,
    project_path: Optional[str] = None,
) -> dict[str, Any]:
    """
    Start OpenOCD for ESP32 debugging (idf.py openocd or standalone openocd).

    Args:
        config:       OpenOCD config (e.g. 'board/esp32s3-builtin.cfg').
                      Uses ESP-IDF defaults if omitted.
        gdb_port:     GDB server port (default 3333).
        project_path: IDF project root (optional).

    Returns: {handle_id, port, ...} — call gdb_connect(port=3333) after.
    """
    handle = esp.openocd_start(config=config, gdb_port=gdb_port, project_path=project_path)
    return handle.as_dict()


@mcp.tool()
def idf_openocd_stop(handle_id: str) -> str:
    """Stop a running OpenOCD process."""
    return esp.openocd_stop(handle_id)


# ---------------------------------------------------------------------------
# ── Inventory / forensics / ELF tools ─────────────────────────────────────
# ---------------------------------------------------------------------------

@mcp.tool()
def target_scan_all() -> dict[str, Any]:
    """
    Probe all currently-connected targets and register CPU identities.

    This performs read-only target identification:
    - ST-Link targets: st-info based chip scan per probe serial
    - ESP targets: esptool chip_info per serial port (if port is known)

    Returns discovered CPU rows and per-probe errors/skips.
    """
    live = pd.enumerate_all_probes()
    reg_by_serial = {p.serial: p for p in pd.get_all_probes()}

    discovered: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    stlink_scan: list[dict[str, Any]] = []
    try:
        stlink_scan = stlink.probe_list()
    except RuntimeError:
        stlink_scan = []
    stlink_by_serial = {
        str(row.get("serial") or ""): row
        for row in stlink_scan
        if row.get("serial")
    }

    for probe in live:
        policy_probe = reg_by_serial.get(probe.serial, probe)
        if not bool(getattr(policy_probe, "scan_allowed", True)):
            skipped.append({
                "serial": probe.serial,
                "kind": probe.kind,
                "reason": "scan blocked by probe policy",
            })
            continue

        if probe.kind == "stlink":
            try:
                info = stlink_by_serial.get(probe.serial)
                if not info:
                    info = stlink.chip_info(probe.serial)
                row = cr.register_stm32(
                    cpu_type=str(info.get("device_name") or info.get("chip_id") or "unknown"),
                    probe_serial=probe.serial,
                    details=info,
                )
                discovered.append({
                    "probe_serial": probe.serial,
                    "probe_model": probe.model,
                    "cpu": row,
                    "chip_info": info,
                })
            except RuntimeError as exc:
                errors.append({
                    "serial": probe.serial,
                    "kind": "stlink",
                    "error": str(exc),
                })
            continue

        if probe.kind == "esp":
            port = probe.port or getattr(policy_probe, "port", "")
            if not port:
                skipped.append({
                    "serial": probe.serial,
                    "kind": "esp",
                    "reason": "no serial port mapped",
                })
                continue
            try:
                try:
                    info = esp.chip_info(port=port, baud=460800)
                except RuntimeError as exc:
                    # Compatibility fallback for esptool versions that don't support --json.
                    if "usage: esptool" not in str(exc).lower():
                        raise
                    tool = shutil.which("esptool.py") or shutil.which("esptool")
                    if not tool:
                        raise
                    legacy = subprocess.run(
                        [tool, "--port", port, "--baud", "460800", "chip_id"],
                        capture_output=True,
                        text=True,
                    )
                    combined = (legacy.stdout + legacy.stderr).strip()
                    info = {}
                    if m := re.search(r"Chip is\s+(.+?)[\r\n]", combined):
                        info["chip_type"] = m.group(1).strip()
                    if m := re.search(r"MAC:\s+([0-9a-f:]+)", combined, re.IGNORECASE):
                        info["mac"] = m.group(1).strip()
                    if not info:
                        raise RuntimeError(f"legacy esptool chip_id parse failed: {combined[:300]}")
                row = cr.register_esp(
                    mac=str(info.get("mac") or "").strip(),
                    chip_type=str(info.get("chip_type") or "unknown"),
                    port=port,
                    details=info,
                )
                discovered.append({
                    "probe_serial": probe.serial,
                    "probe_model": probe.model,
                    "port": port,
                    "cpu": row,
                    "chip_info": info,
                })
            except RuntimeError as exc:
                errors.append({
                    "serial": probe.serial,
                    "kind": "esp",
                    "port": port,
                    "error": str(exc),
                })

    return {
        "connected_count": len(live),
        "discovered": discovered,
        "skipped": skipped,
        "errors": errors,
        "cpus": cr.list_cpus(),
    }


@mcp.tool()
def stlink_capture_artifacts(
    output_dir: str,
    serial: Optional[str] = None,
    include_flash: bool = True,
    include_ram: bool = False,
    include_otp: bool = True,
    flash_address: str = "0x08000000",
    flash_length: Optional[int] = None,
    ram_address: str = "0x20000000",
    ram_length: Optional[int] = None,
) -> dict[str, Any]:
    """
    Capture ST-Link target artifacts to a local directory.

    Writes per-target artifacts under:
      <output_dir>/<serial_tail>/

    Includes:
    - flash/ram snapshot files via st-flash (optional)
    - option-bytes/OTP text via CubeProgrammer (optional)
    """
    resolved_serial = _resolve_approved_stlink_serial(serial, capability="read")
    _register_and_require_stm32_cpu(stlink.chip_info(resolved_serial), resolved_serial, capability="read")

    dest = Path(output_dir) / resolved_serial[-8:]
    dest.mkdir(parents=True, exist_ok=True)

    result: dict[str, Any] = {
        "serial": resolved_serial,
        "output_dir": str(dest),
        "flash_ram": None,
        "otp": None,
        "warnings": [],
    }

    if include_flash or include_ram:
        result["flash_ram"] = stlink.dump_memory_snapshot(
            output_dir=str(dest),
            serial=resolved_serial,
            flash_address=flash_address,
            flash_length=flash_length,
            ram_address=ram_address,
            ram_length=ram_length,
            include_flash=include_flash,
            include_ram=include_ram,
        )

    if include_otp:
        otp_path = dest / "otp.txt"
        try:
            result["otp"] = cube.dump_otp(str(otp_path), resolved_serial)
        except RuntimeError as exc:
            result["warnings"].append(
                f"OTP capture failed: {exc}. Install cube or STM32_Programmer_CLI to enable OTP dump."
            )

    return result


@mcp.tool()
def elf_disassemble(
    elf_path: str,
    output_path: Optional[str] = None,
    include_source: bool = False,
) -> dict[str, Any]:
    """
    Disassemble an ELF file and extract quick product/board hints.

    Uses objdump/readelf/strings when available and writes the disassembly text
    to a local file for offline review.
    """
    elf = Path(elf_path)
    if not elf.exists():
        raise RuntimeError(f"ELF file not found: {elf_path}")

    objdump = shutil.which("arm-none-eabi-objdump") or shutil.which("objdump")
    if not objdump:
        raise RuntimeError("No objdump found. Install binutils or gcc-arm-none-eabi toolchain.")

    disasm_out = Path(output_path) if output_path else elf.with_suffix(elf.suffix + ".disasm.txt")
    disasm_out.parent.mkdir(parents=True, exist_ok=True)

    cmd = [objdump, "-d", "-C", str(elf)]
    if include_source:
        cmd.insert(1, "-S")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"objdump disassembly failed (rc={proc.returncode}): {(proc.stderr or proc.stdout)[:500]}")
    disasm_out.write_text(proc.stdout)

    readelf = shutil.which("arm-none-eabi-readelf") or shutil.which("readelf")
    elf_header = ""
    machine = ""
    entry = ""
    if readelf:
        hdr = subprocess.run([readelf, "-h", str(elf)], capture_output=True, text=True)
        elf_header = (hdr.stdout or hdr.stderr).strip()
        if m := re.search(r"Machine:\s+(.+)", elf_header):
            machine = m.group(1).strip()
        if m := re.search(r"Entry point address:\s+(.+)", elf_header):
            entry = m.group(1).strip()

    strings_cmd = shutil.which("strings")
    hints: list[str] = []
    if strings_cmd:
        sres = subprocess.run([strings_cmd, str(elf)], capture_output=True, text=True)
        lines = (sres.stdout or "").splitlines()
        pattern = re.compile(
            r"(stm32|esp32|nucleo|disco|discovery|deno|board|product|idf|zephyr|arduino)",
            re.IGNORECASE,
        )
        seen: set[str] = set()
        for line in lines:
            if pattern.search(line) and line not in seen:
                seen.add(line)
                hints.append(line)
            if len(hints) >= 40:
                break

    return {
        "elf_path": str(elf),
        "disassembly_path": str(disasm_out),
        "objdump": objdump,
        "machine": machine,
        "entry_point": entry,
        "elf_header": elf_header,
        "product_hints": hints,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="awto-debug-embedded MCP server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--registry",
        default="probes.json",
        help="Path to probe registry file (default: probes.json in cwd)",
    )
    p.add_argument(
        "--cpu-registry",
        default="cpus.json",
        help="Path to CPU registry file (default: cpus.json in cwd)",
    )
    p.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable DEBUG-level logging",
    )
    p.add_argument(
        "--no-monitor",
        action="store_true",
        help="Disable the background probe monitor (useful for testing)",
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    _setup_logging(verbose=args.verbose)

    # Configure probe registry path
    pd.configure_registry(args.registry)
    cr.configure_registry(args.cpu_registry)

    # Start probe monitor unless disabled
    if not args.no_monitor:
        monitor = pd.get_monitor()
        monitor.on_connect(_on_probe_connect)
        monitor.on_disconnect(_on_probe_disconnect)
        log.info(
            "Probe monitor started — probe registry: %s, cpu registry: %s",
            args.registry,
            args.cpu_registry,
        )

    log.info("awto-debug-embedded MCP server starting")

    # Startup permission check — warn but don't abort
    perms = pd.check_usb_permissions()
    if not perms.ok:
        for issue in perms.issues:
            log.warning("USB permissions: %s", issue)
        log.warning(
            "ST-Link devices may not be accessible. "
            "Call system_permissions_check() for a full diagnosis and fix instructions."
        )

    try:
        mcp.run()
    finally:
        pd.stop_monitor()
        _mgr().stop_all()
        log.info("awto-debug-embedded MCP server stopped")


if __name__ == "__main__":
    main()
