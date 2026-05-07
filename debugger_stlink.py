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

import logging
import re
import subprocess
import shutil
from typing import Any, Optional

from process_manager import ProcessHandle, get_manager

log = logging.getLogger("awto.stlink")

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

    Each dict: {serial, hla_serial, firmware, voltage, target_id, target_description}
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

    if current:
        probes.append(current)

    return probes


def chip_info(serial: Optional[str] = None) -> dict[str, Any]:
    """
    Return chip information for the connected target.

    Args:
        serial: ST-Link serial (optional if only one probe is attached).

    Returns dict with: chip_id, flash_size_kb, ram_size_kb, device_name, description
    """
    _require("st-info")
    cmd = ["st-info", "--chipid"]
    if serial:
        cmd += ["--serial", serial]
    _, stdout, stderr = _run(cmd)
    combined = stdout + stderr

    info: dict[str, Any] = {}

    if m := re.search(r"chipid:\s+(0x[0-9a-fA-F]+)", combined):
        info["chip_id"] = m.group(1)
    if m := re.search(r"flash_size:\s+(\d+)", combined):
        info["flash_size_kb"] = int(m.group(1))
    if m := re.search(r"sram_size:\s+(\d+)", combined):
        info["ram_size_kb"] = int(m.group(1))
    if m := re.search(r"description:\s+(.+)", combined):
        info["description"] = m.group(1).strip()

    # Also try --descr
    cmd2 = ["st-info", "--descr"]
    if serial:
        cmd2 += ["--serial", serial]
    _, stdout2, _ = _run(cmd2)
    if stdout2.strip():
        info["device_name"] = stdout2.strip()

    if not info:
        raise RuntimeError(f"Could not read chip info — is a target connected? Output: {combined[:200]}")
    return info


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
    """Verify flash contents against a firmware file (hex or bin)."""
    _require("st-flash")
    fmt = "ihex" if firmware_path.endswith(".hex") else "binary"
    cmd = ["st-flash"]
    if serial:
        cmd += ["--serial", serial]
    if fmt == "ihex":
        cmd += ["--format", "ihex", "verify", firmware_path]
    else:
        cmd += ["verify", firmware_path, "0x8000000"]
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
