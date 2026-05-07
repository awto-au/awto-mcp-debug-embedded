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
import probe_detect as pd
from gdb_client import get_client as _gdb
from process_manager import get_manager as _mgr

# ---------------------------------------------------------------------------
# Logging  (colorlog stderr + syslog)
# ---------------------------------------------------------------------------

def _setup_logging(verbose: bool = False) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)

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

FIRST USE — Probe approval flow:
1. Call probe_list() to see all detected probes (ST-Link and ESP serial adapters).
2. For any probe with state="pending", show the list to the user and ask:
   - Which probes to use (approve) — optionally assign a short nickname.
   - Which probes to ignore (will be hidden in future sessions).
3. Call probe_approve(serial=..., nick=...) for each approved probe.
4. Call probe_ignore(serial=...) for probes the user does not want.

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


# ---------------------------------------------------------------------------
# ── Probe management tools ───────────────────────────────────────────────
# ---------------------------------------------------------------------------

@mcp.tool()
def probe_list() -> dict[str, Any]:
    """
    List all detected debug probes and available debugger backends.

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

    for p in all_probes:
        entry = asdict(p)
        entry["connected"] = p.serial in connected_serials
        probe_list_out.append(entry)
        if p.state == "pending":
            pending.append(p.serial)

    # Also include probes currently connected but not yet in registry
    # (shouldn't happen in normal flow, but guard against it)
    for cp in monitor.connected_probes():
        if not any(p["serial"] == cp.serial for p in probe_list_out):
            entry = asdict(cp)
            entry["connected"] = True
            probe_list_out.append(entry)
            pending.append(cp.serial)

    backends = asdict(pd.check_backends())
    result: dict[str, Any] = {
        "probes":   probe_list_out,
        "pending":  pending,
        "backends": backends,
    }
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
def probe_info(serial: Optional[str] = None) -> dict[str, Any]:
    """
    Read target chip info for the connected MCU.

    Tries st-info first (fast), falls back to CubeProgrammer if not available.

    Args:
        serial: ST-Link probe serial (optional — uses first found if omitted).
    """
    errors: list[str] = []
    # Try st-info
    try:
        info = stlink.chip_info(serial)
        info["backend"] = "st-info"
        return info
    except RuntimeError as exc:
        errors.append(f"st-info: {exc}")

    # Try CubeProgrammer
    try:
        info = cube.probe_info(serial)
        info["backend"] = "cube"
        return info
    except RuntimeError as exc:
        errors.append(f"cube: {exc}")

    raise RuntimeError(
        "Could not read target info from any available backend. "
        + " | ".join(errors)
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
    return stlink.flash_write(firmware, address=address, serial=serial, reset=reset)


@mcp.tool()
def stlink_erase(serial: Optional[str] = None) -> str:
    """Mass-erase the target flash using st-flash."""
    return stlink.flash_erase(serial)


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
    return stlink.flash_read(output_path, address, length, serial)


@mcp.tool()
def stlink_verify(firmware: str, serial: Optional[str] = None) -> str:
    """Verify flash contents against a firmware file using st-flash."""
    return stlink.flash_verify(firmware, serial)


@mcp.tool()
def stlink_reset(serial: Optional[str] = None) -> str:
    """Reset the target MCU via ST-Link."""
    return stlink.reset_target(serial)


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
    handle = stlink.gdb_server_start(port=port, serial=serial)
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
    handle = stlink.trace_start(freq=freq, serial=serial)
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
) -> str:
    """
    Flash firmware using STM32CubeProgrammer.

    Supports .hex, .bin, .elf, .srec formats.

    Args:
        firmware: Path to firmware file.
        serial:   ST-Link serial (optional).
        verify:   Verify flash after write (default True).
        reset:    Hard-reset target after flash (default True).
    """
    return cube.flash_write(firmware, serial=serial, verify=verify, reset=reset)


@mcp.tool()
def cube_erase(serial: Optional[str] = None) -> str:
    """Mass-erase target flash via STM32CubeProgrammer."""
    return cube.flash_erase(serial)


@mcp.tool()
def cube_info(serial: Optional[str] = None) -> dict[str, Any]:
    """Read target device info via STM32CubeProgrammer (device_id, flash size, UID, etc.)."""
    return cube.probe_info(serial)


@mcp.tool()
def cube_read_uid(serial: Optional[str] = None) -> str:
    """Read the 96-bit STM32 unique device ID via CubeProgrammer."""
    return cube.read_uid(serial)


@mcp.tool()
def cube_otp_read(serial: Optional[str] = None) -> str:
    """Read option bytes / OTP area via CubeProgrammer."""
    return cube.read_otp(serial)


@mcp.tool()
def cube_recover(serial: Optional[str] = None) -> str:
    """
    Attempt full-chip recovery using connect-under-reset + mass erase.

    Use when the target is locked or unresponsive to normal connection.
    """
    return cube.recover(serial)


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
    return cube.flash_read(output_path, address, length, serial)


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
    handle = cube.gdb_server_start(port=port, serial=serial, freq=freq)
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
    return esp.chip_info(port=port, baud=baud)


@mcp.tool()
def esp_flash_id(port: Optional[str] = None) -> dict[str, Any]:
    """Read ESP flash manufacturer and size information."""
    return esp.flash_id(port=port)


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
    return esp.flash_write(firmware, port=port, baud=baud, offset=offset, chip=chip)


@mcp.tool()
def esp_flash_erase(port: Optional[str] = None, chip: Optional[str] = None) -> str:
    """Erase entire ESP flash chip."""
    return esp.flash_erase(port=port, chip=chip)


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
    return esp.flash_read(output_path, offset=offset, length=length, port=port)


@mcp.tool()
def esp_reset(port: Optional[str] = None, mode: str = "normal") -> str:
    """
    Reset the ESP chip.

    Args:
        port: Serial port (auto-detect if omitted).
        mode: 'normal' (run firmware) or 'bootloader' (enter download mode).
    """
    return esp.reset_chip(port=port, mode=mode)


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
    return esp.idf_flash(project_path=project_path, port=port, baud=baud)


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
    handle = esp.idf_monitor_start(project_path=project_path, port=port, baud=baud)
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

    # Start probe monitor unless disabled
    if not args.no_monitor:
        monitor = pd.get_monitor()
        monitor.on_connect(_on_probe_connect)
        monitor.on_disconnect(_on_probe_disconnect)
        log.info("Probe monitor started — registry: %s", args.registry)

    log.info("awto-debug-embedded MCP server starting")

    try:
        mcp.run()
    finally:
        pd.stop_monitor()
        _mgr().stop_all()
        log.info("awto-debug-embedded MCP server stopped")


if __name__ == "__main__":
    main()
