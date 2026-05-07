"""
debugger_esp.py — subprocess wrappers for esptool.py and idf.py.

Covers:
  esptool.py: chip_info, flash_id, write_flash, erase_flash, read_flash, hard_reset
  idf.py:     build, flash, monitor (long-running), menuconfig, size, openocd

esptool is called with --json where available for structured output.
idf.py long-running tasks (monitor, openocd) are managed via process_manager.

All functions raise RuntimeError on failure with a clean message.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

from process_manager import ProcessHandle, get_manager

log = logging.getLogger("awto.esp")

# ---------------------------------------------------------------------------
# Backend detection
# ---------------------------------------------------------------------------

def _esptool_cmd() -> list[str]:
    """Return base esptool command."""
    for candidate in ("esptool.py", "esptool"):
        if shutil.which(candidate):
            return [candidate]
    raise RuntimeError(
        "esptool not found. Install with: pip install esptool"
    )


def _idf_cmd() -> str:
    """Return idf.py path."""
    path = shutil.which("idf.py")
    if path:
        return path
    # Check common IDF install locations
    for candidate in [
        Path.home() / "esp/esp-idf/tools/idf.py",
        Path("/opt/esp-idf/tools/idf.py"),
        Path("/usr/local/bin/idf.py"),
    ]:
        if candidate.exists():
            return str(candidate)
    raise RuntimeError(
        "idf.py not found. Source the ESP-IDF environment: "
        ". $IDF_PATH/export.sh"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(cmd: list[str], timeout: int = 60, cwd: Optional[str] = None) -> tuple[int, str, str]:
    log.debug("run: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Command timed out after {timeout}s: {' '.join(cmd)}")
    except FileNotFoundError:
        raise RuntimeError(f"Command not found: {cmd[0]}")


def _check(cmd: list[str], op: str, timeout: int = 60, cwd: Optional[str] = None) -> str:
    rc, stdout, stderr = _run(cmd, timeout=timeout, cwd=cwd)
    combined = (stdout + stderr).strip()
    if rc != 0:
        raise RuntimeError(f"{op} failed (rc={rc}): {combined[:500]}")
    return combined


def _port_args(port: Optional[str], baud: Optional[int] = None) -> list[str]:
    args = []
    if port:
        args += ["--port", port]
    if baud:
        args += ["--baud", str(baud)]
    return args


# ---------------------------------------------------------------------------
# esptool — chip and flash info
# ---------------------------------------------------------------------------

def chip_info(port: Optional[str] = None, baud: int = 460800) -> dict[str, Any]:
    """
    Read chip info from ESP device using esptool.py --json.

    Returns dict with: chip_type, chip_features, mac, crystal_is_26mhz,
    flash_size, flash_manufacturer_id, flash_device_id.
    """
    tool = _esptool_cmd()
    cmd = tool + _port_args(port, baud) + ["--json", "chip_id"]
    rc, stdout, stderr = _run(cmd, timeout=20)

    # Try JSON output first
    try:
        data = json.loads(stdout)
        return {
            "chip_type": data.get("chip_type", ""),
            "chip_features": data.get("chip_features", []),
            "mac": data.get("mac", ""),
            "crystal_is_26mhz": data.get("crystal_is_26mhz", False),
        }
    except (json.JSONDecodeError, ValueError):
        pass

    # Fall back to text parse
    combined = stdout + stderr
    info: dict[str, Any] = {}
    if m := re.search(r"Chip is\s+(.+?)[\r\n]", combined):
        info["chip_type"] = m.group(1).strip()
    if m := re.search(r"Features:\s+(.+?)[\r\n]", combined):
        info["chip_features"] = [f.strip() for f in m.group(1).split(",")]
    if m := re.search(r"MAC:\s+([0-9a-f:]+)", combined, re.IGNORECASE):
        info["mac"] = m.group(1)
    if rc != 0 and not info:
        raise RuntimeError(f"chip_info failed (rc={rc}): {combined[:300]}")
    return info


def flash_id(port: Optional[str] = None, baud: int = 460800) -> dict[str, Any]:
    """Read flash manufacturer ID and size from esptool."""
    tool = _esptool_cmd()
    cmd = tool + _port_args(port, baud) + ["flash_id"]
    rc, stdout, stderr = _run(cmd, timeout=15)
    combined = stdout + stderr
    info: dict[str, Any] = {}
    if m := re.search(r"Manufacturer:\s+(\S+)", combined, re.IGNORECASE):
        info["manufacturer_id"] = m.group(1)
    if m := re.search(r"Device:\s+(\S+)", combined, re.IGNORECASE):
        info["device_id"] = m.group(1)
    if m := re.search(r"Detected flash size:\s+(\S+)", combined, re.IGNORECASE):
        info["flash_size"] = m.group(1)
    if rc != 0 and not info:
        raise RuntimeError(f"flash_id failed (rc={rc}): {combined[:300]}")
    return info


# ---------------------------------------------------------------------------
# esptool — flash write / erase / read
# ---------------------------------------------------------------------------

def flash_write(
    firmware_path: str,
    port: Optional[str] = None,
    baud: int = 460800,
    offset: str = "0x0",
    chip: Optional[str] = None,
    compress: bool = True,
) -> str:
    """
    Write firmware to ESP flash.

    Args:
        firmware_path: Path to .bin file.
        port:          Serial port (auto-detect if omitted).
        baud:          Baud rate (default 460800).
        offset:        Flash offset (default 0x0).
        chip:          Target chip family override (e.g. 'esp32', 'esp32s3').
        compress:      Use compression (-z flag, default True).
    """
    tool = _esptool_cmd()
    cmd = tool + _port_args(port, baud)
    if chip:
        cmd += ["--chip", chip]
    cmd += ["write_flash"]
    if compress:
        cmd += ["-z"]
    cmd += [offset, firmware_path]
    out = _check(cmd, "esptool write_flash", timeout=120)
    log.info("esptool write_flash complete: %s", firmware_path)
    return f"Flash write OK: {firmware_path}\n{out[-200:].strip()}"


def flash_erase(port: Optional[str] = None, baud: int = 460800, chip: Optional[str] = None) -> str:
    """Erase entire ESP flash chip."""
    tool = _esptool_cmd()
    cmd = tool + _port_args(port, baud)
    if chip:
        cmd += ["--chip", chip]
    cmd += ["erase_flash"]
    out = _check(cmd, "esptool erase_flash", timeout=60)
    return f"Erase OK\n{out[-200:].strip()}"


def flash_read(
    output_path: str,
    offset: str = "0x0",
    length: int = 0x400000,
    port: Optional[str] = None,
    baud: int = 460800,
) -> str:
    """
    Read a region of ESP flash to a binary file.

    Args:
        output_path: Destination .bin file.
        offset:      Start offset (default 0x0).
        length:      Number of bytes to read (default 4 MB).
    """
    tool = _esptool_cmd()
    cmd = tool + _port_args(port, baud) + [
        "read_flash", offset, hex(length), output_path,
    ]
    out = _check(cmd, "esptool read_flash", timeout=180)
    return f"Read OK → {output_path}\n{out[-200:].strip()}"


def reset_chip(
    port: Optional[str] = None,
    baud: int = 115200,
    mode: str = "normal",
) -> str:
    """
    Reset the ESP chip.

    Args:
        mode: 'normal' (run firmware), 'bootloader' (enter download mode), 'hard'
    """
    tool = _esptool_cmd()
    cmd = tool + _port_args(port, baud)
    if mode == "bootloader":
        cmd += ["load_ram", "/dev/null"]  # forces bootloader entry stub
    elif mode == "hard":
        # Use run command to trigger hard reset
        cmd += ["run"]
    else:
        cmd += ["run"]
    rc, stdout, stderr = _run(cmd, timeout=10)
    combined = (stdout + stderr).strip()
    # run often exits non-zero but succeeds — be lenient
    log.info("esp reset [%s]: rc=%d", mode, rc)
    return f"Reset ({mode}) OK\n{combined[-200:].strip()}"


# ---------------------------------------------------------------------------
# idf.py — build / flash / monitor / size / menuconfig
# ---------------------------------------------------------------------------

def idf_build(project_path: Optional[str] = None) -> str:
    """
    Run 'idf.py build' in the project directory.

    Args:
        project_path: Path to the IDF project root (defaults to cwd).
    """
    idf = _idf_cmd()
    out = _check([idf, "build"], "idf.py build", timeout=600, cwd=project_path)
    return f"Build OK\n{out[-400:].strip()}"


def idf_flash(
    project_path: Optional[str] = None,
    port: Optional[str] = None,
    baud: int = 460800,
) -> str:
    """Run 'idf.py flash' to build (if needed) and flash."""
    idf = _idf_cmd()
    cmd = [idf, "flash"]
    if port:
        cmd += ["-p", port]
    cmd += ["-b", str(baud)]
    out = _check(cmd, "idf.py flash", timeout=300, cwd=project_path)
    return f"Flash OK\n{out[-400:].strip()}"


def idf_size(project_path: Optional[str] = None) -> str:
    """Run 'idf.py size' and return binary size summary."""
    idf = _idf_cmd()
    out = _check([idf, "size"], "idf.py size", timeout=120, cwd=project_path)
    return out


def idf_monitor_start(
    project_path: Optional[str] = None,
    port: Optional[str] = None,
    baud: int = 115200,
) -> ProcessHandle:
    """
    Start 'idf.py monitor' as a long-running background process.

    Returns ProcessHandle. Call idf_monitor_stop(handle.id) to stop.
    """
    idf = _idf_cmd()
    cmd = [idf, "monitor"]
    if port:
        cmd += ["-p", port]
    cmd += ["-b", str(baud)]
    return get_manager().start(
        cmd, tag="idf-monitor", startup_wait_s=1.0,
        cwd=project_path,
    )


def idf_monitor_stop(handle_id: str) -> str:
    """Stop a running idf.py monitor process."""
    rc = get_manager().stop(handle_id)
    return f"idf.py monitor stopped (rc={rc})"


def idf_menuconfig(project_path: Optional[str] = None) -> str:
    """
    Run 'idf.py menuconfig'.

    Note: menuconfig is interactive — this runs it in a subprocess and returns
    when the user saves and exits. Not suitable for headless/MCP use; provided
    for completeness.
    """
    idf = _idf_cmd()
    try:
        subprocess.run([idf, "menuconfig"], cwd=project_path, check=True)
        return "menuconfig completed"
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"idf.py menuconfig failed (rc={exc.returncode})")


# ---------------------------------------------------------------------------
# OpenOCD for ESP32 (via idf.py openocd or standalone)
# ---------------------------------------------------------------------------

def openocd_start(
    config: Optional[str] = None,
    gdb_port: int = 3333,
    tcl_port: int = 6666,
    telnet_port: int = 4444,
    project_path: Optional[str] = None,
) -> ProcessHandle:
    """
    Start OpenOCD for ESP32 debugging.

    Tries 'idf.py openocd' first (uses ESP-IDF bundled openocd-esp32),
    then falls back to standalone 'openocd'.

    Args:
        config:      OpenOCD config file or board name (e.g. 'board/esp32s3-builtin.cfg').
                     If omitted, uses ESP-IDF defaults.
        gdb_port:    GDB server port (default 3333).
        tcl_port:    TCL server port (default 6666).
        telnet_port: Telnet port (default 4444).
    """
    idf = shutil.which("idf.py")
    ocd = shutil.which("openocd")

    if idf:
        cmd = [idf, "openocd"]
        if config:
            cmd += ["-f", config]
        return get_manager().start(
            cmd, tag="openocd-esp", port=gdb_port, startup_wait_s=2.0,
            cwd=project_path,
        )
    elif ocd:
        cmd = [ocd]
        if config:
            cmd += ["-f", config]
        else:
            cmd += ["-f", "board/esp32s3-builtin.cfg"]
        cmd += [
            "-c", f"gdb_port {gdb_port}",
            "-c", f"tcl_port {tcl_port}",
            "-c", f"telnet_port {telnet_port}",
        ]
        return get_manager().start(cmd, tag="openocd-esp", port=gdb_port, startup_wait_s=2.0)
    else:
        raise RuntimeError(
            "Neither idf.py nor openocd found. "
            "Source the ESP-IDF environment or install openocd."
        )


def openocd_stop(handle_id: str) -> str:
    """Stop a running OpenOCD process."""
    rc = get_manager().stop(handle_id)
    return f"OpenOCD stopped (rc={rc})"
