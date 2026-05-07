"""
debugger_cube.py — subprocess wrappers for STM32CubeProgrammer.

Detects 'cube' (Cube IDE helper) or standalone 'STM32_Programmer_CLI'.
Wraps: probe info, flash write/erase/read, UID read, OTP, recovery,
       GDB server (cube stlink-gdbserver).

All functions raise RuntimeError on failure with a clean error message.
Pattern borrowed from ~/git/stlink-toolkit/stlink_toolkit/programmer.py.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

from process_manager import ProcessHandle, get_manager

log = logging.getLogger("awto.cube")

# ---------------------------------------------------------------------------
# Backend detection (mirrors stlink-toolkit programmer.py pattern)
# ---------------------------------------------------------------------------

def _detect_programmer() -> list[str]:
    """Return the base command list for STM32CubeProgrammer."""
    if shutil.which("cube"):
        return ["cube", "programmer"]
    cli = shutil.which("STM32_Programmer_CLI")
    if not cli:
        search_roots = [
            Path.home() / ".local/share/stm32cube/bundles/programmer",
            Path("/opt/st"),
        ]
        candidates: list[Path] = []
        for root in search_roots:
            if root.exists():
                candidates.extend(root.rglob("STM32_Programmer_CLI"))
        if candidates:
            candidates.sort(key=str)
            cli = str(candidates[-1])
    if cli:
        log.debug("Using STM32_Programmer_CLI: %s", cli)
        return [cli]
    raise RuntimeError(
        "STM32CubeProgrammer not found. Install from "
        "https://www.st.com/en/development-tools/stm32cubeprog.html "
        "or install the Cube IDE bundle."
    )


def _detect_gdbserver() -> Optional[str]:
    """Return path to ST-LINK_gdbserver or None."""
    return shutil.which("ST-LINK_gdbserver")


def _detect_cube_programmer_bin() -> Optional[str]:
    """Return STM32CubeProgrammer bin directory for -cp argument."""
    env_path = os.environ.get("STM32CUBEPROGRAMMER_BIN")
    if env_path and (Path(env_path) / "STM32_Programmer_CLI").exists():
        return env_path
    search_roots = [
        Path.home() / ".local/share/stm32cube/bundles/programmer",
        Path("/opt/st"),
    ]
    candidates: list[Path] = []
    for root in search_roots:
        if not root.exists():
            continue
        for cli in root.rglob("STM32_Programmer_CLI"):
            candidates.append(cli.parent)
    if not candidates:
        return None
    candidates.sort(key=str)
    return str(candidates[-1])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(cmd: list[str], timeout: int = 60) -> tuple[int, str, str]:
    log.debug("run: %s", " ".join(cmd))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Command timed out after {timeout}s: {' '.join(cmd)}")
    except FileNotFoundError:
        raise RuntimeError(f"Command not found: {cmd[0]}")


def _check(cmd: list[str], op: str, timeout: int = 60) -> str:
    rc, stdout, stderr = _run(cmd, timeout=timeout)
    combined = (stdout + stderr).strip()
    if rc != 0:
        raise RuntimeError(f"{op} failed (rc={rc}): {combined[:500]}")
    return combined


def _connect_args(serial: Optional[str], freq: int = 8000, reset: bool = False) -> list[str]:
    args = ["-c", "port=SWD"]
    if serial:
        args += [f"sn={serial}"]
    args += [f"freq={freq}"]
    if reset:
        args += ["reset=HWrst"]
    return args


def _connect_under_reset_args(serial: Optional[str], freq: int = 4000) -> list[str]:
    args = ["-c", "port=SWD"]
    if serial:
        args += [f"sn={serial}"]
    args += [f"freq={freq}", "mode=UR"]
    return args


# ---------------------------------------------------------------------------
# Probe / target info
# ---------------------------------------------------------------------------

def probe_info(serial: Optional[str] = None) -> dict[str, Any]:
    """
    Return target info from CubeProgrammer.

    Returns dict with: device_id, device_name, flash_size_kb, ram_size_kb,
    uid_str, board_name, cpu_freq_mhz.
    """
    prog = _detect_programmer()
    cmd = prog + _connect_args(serial) + ["-i"]
    rc, stdout, stderr = _run(cmd, timeout=20)
    combined = stdout + stderr
    info: dict[str, Any] = {}

    if m := re.search(r"Device ID\s*:\s*(0x[0-9a-fA-F]+)", combined, re.IGNORECASE):
        info["device_id"] = m.group(1)
    if m := re.search(r"Device name\s*:\s*(.+)", combined, re.IGNORECASE):
        info["device_name"] = m.group(1).strip()
    if m := re.search(r"Flash\s*size\s*:\s*(\d+)\s*K?Bytes", combined, re.IGNORECASE):
        info["flash_size_kb"] = int(m.group(1))
    if m := re.search(r"RAM\s*size\s*:\s*(\d+)\s*K?Bytes", combined, re.IGNORECASE):
        info["ram_size_kb"] = int(m.group(1))
    if m := re.search(r"Board\s*:\s*(.+)", combined, re.IGNORECASE):
        info["board_name"] = m.group(1).strip()
    if m := re.search(r"CPU\s*freq\s*:\s*([0-9.]+)\s*MHz", combined, re.IGNORECASE):
        info["cpu_freq_mhz"] = float(m.group(1))

    if not info:
        raise RuntimeError(
            f"Could not read target info — is a target connected and powered? "
            f"Output: {combined[:300]}"
        )
    return info


def read_uid(serial: Optional[str] = None) -> str:
    """Read the 96-bit STM32 unique device ID (UID)."""
    prog = _detect_programmer()
    # Standard UID base address for most STM32 families; auto-detect via --uid flag
    cmd = prog + _connect_args(serial) + ["-uid"]
    rc, stdout, stderr = _run(cmd, timeout=15)
    combined = stdout + stderr
    if m := re.search(r"UID\s*:\s*([0-9A-Fa-f\s\-]+)", combined, re.IGNORECASE):
        return m.group(1).strip().replace(" ", "").replace("-", "")
    if rc != 0:
        raise RuntimeError(f"read_uid failed (rc={rc}): {combined[:300]}")
    return combined.strip()


# ---------------------------------------------------------------------------
# Flash operations
# ---------------------------------------------------------------------------

def flash_write(
    firmware_path: str,
    serial: Optional[str] = None,
    verify: bool = True,
    reset: bool = True,
) -> str:
    """
    Flash firmware using CubeProgrammer (-d / download).

    Supports .hex, .bin, .elf, .srec.
    """
    prog = _detect_programmer()
    cmd = prog + _connect_args(serial) + ["-d", firmware_path]
    if verify:
        cmd += ["-v"]
    if reset:
        cmd += ["--hardRst"]
    out = _check(cmd, "cube flash", timeout=180)
    log.info("cube flash complete: %s", firmware_path)
    return f"Flash OK: {firmware_path}\n{out[-300:].strip()}"


def flash_erase(serial: Optional[str] = None) -> str:
    """Mass-erase target flash via CubeProgrammer."""
    prog = _detect_programmer()
    cmd = prog + _connect_args(serial) + ["-e", "all"]
    out = _check(cmd, "cube erase", timeout=60)
    return f"Erase OK\n{out[-200:].strip()}"


def flash_read(
    output_path: str,
    address: str,
    length: int,
    serial: Optional[str] = None,
) -> str:
    """
    Read a flash region to a binary file.

    Args:
        output_path: Destination .bin file path.
        address:     Start address (e.g. '0x08000000').
        length:      Number of bytes.
    """
    prog = _detect_programmer()
    cmd = prog + _connect_args(serial) + [
        "-r64", output_path, address, hex(length),
    ]
    out = _check(cmd, "cube read", timeout=120)
    return f"Read OK → {output_path}\n{out[-200:].strip()}"


def read_otp(serial: Optional[str] = None) -> str:
    """Read OTP (one-time programmable) area."""
    prog = _detect_programmer()
    # OTP address varies by family — use option bytes interface
    cmd = prog + _connect_args(serial) + ["-ob", "displ"]
    rc, stdout, stderr = _run(cmd, timeout=20)
    combined = (stdout + stderr).strip()
    if rc != 0:
        raise RuntimeError(f"OTP read failed (rc={rc}): {combined[:300]}")
    return combined


def dump_otp(output_path: str, serial: Optional[str] = None) -> dict[str, Any]:
    """Read option bytes / OTP text and save it to a file."""
    text = read_otp(serial)
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + ("" if text.endswith("\n") else "\n"))
    return {
        "output_path": str(path),
        "bytes_written": path.stat().st_size,
    }


def connection_properties(
    serial: Optional[str] = None,
    freq: int = 8000,
    under_reset: bool = False,
) -> dict[str, Any]:
    """Return the CubeProgrammer connection arguments that would be used."""
    connect_args = _connect_under_reset_args(serial, freq=freq) if under_reset else _connect_args(
        serial,
        freq=freq,
    )
    return {
        "backend": shutil.which("cube") and "cube" or shutil.which("STM32_Programmer_CLI") and "STM32_Programmer_CLI" or None,
        "serial": serial,
        "freq_khz": freq,
        "under_reset": under_reset,
        "connect_args": connect_args,
        "notes": [
            "under_reset=true uses mode=UR for a connect-under-reset session.",
            "Normal connections use port=SWD and the requested SWD clock frequency.",
        ],
    }


def recover(serial: Optional[str] = None) -> str:
    """
    Attempt full-chip recovery: connect-under-reset then mass erase.

    Use when the target is locked or unresponsive to normal connection.
    """
    prog = _detect_programmer()
    log.warning("Starting recovery sequence (connect-under-reset + mass erase)")
    cmd = prog + _connect_under_reset_args(serial) + ["-e", "all"]
    out = _check(cmd, "cube recover", timeout=120)
    return f"Recovery OK\n{out[-300:].strip()}"


# ---------------------------------------------------------------------------
# GDB server
# ---------------------------------------------------------------------------

def gdb_server_start(
    port: int = 61234,
    serial: Optional[str] = None,
    freq: int = 8000,
) -> ProcessHandle:
    """
    Start a GDB server using either ST-LINK_gdbserver or cube stlink-gdbserver.

    Args:
        port:   TCP port (default 61234 — CubeProgrammer default).
        serial: ST-Link serial (optional).
        freq:   SWD frequency in kHz (default 8000).

    Returns: ProcessHandle.
    """
    standalone = _detect_gdbserver()
    if standalone:
        cmd = [
            standalone,
            "-p", str(port),
            "-i", "swd",
            "-k",
            "-l", "31",
        ]
        if serial:
            cmd += ["-s", serial]
    elif shutil.which("cube"):
        cmd = [
            "cube", "stlink-gdbserver",
            "--swd",
            "--port-number", str(port),
            "--frequency", str(freq),
            "--shared",
            "--initialize-reset",
            "--verbose",
        ]
        if serial:
            cmd += ["--serial-number", serial]
        cp = _detect_cube_programmer_bin()
        if cp:
            cmd += ["-cp", cp]
    else:
        raise RuntimeError(
            "No GDB server found. Install ST-LINK_gdbserver "
            "(part of STM32CubeProgrammer) or the cube CLI."
        )

    tag = f"gdb-cube-{port}"
    return get_manager().start(cmd, tag=tag, port=port, startup_wait_s=1.5)


def gdb_server_stop(handle_id: str) -> str:
    """Stop a running cube/ST-LINK_gdbserver by handle ID."""
    rc = get_manager().stop(handle_id)
    return f"GDB server stopped (rc={rc})"
