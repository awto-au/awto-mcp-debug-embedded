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
from typing import Any, Callable, Optional

from process_manager import ProcessHandle, get_manager

log = logging.getLogger("awto.cube")

# ---------------------------------------------------------------------------
# Backend detection (mirrors stlink-toolkit programmer.py pattern)
# ---------------------------------------------------------------------------

def find_cube_programmer() -> Optional[list[str]]:
    """Return the base command list for STM32CubeProgrammer, or None if not found.

    Resolution order:
      1. `cube programmer` wrapper on PATH (Cube IDE helper)
      2. `STM32_Programmer_CLI` on PATH
      3. `STM32_Programmer_CLI` under standard install roots
         (~/.local/share/stm32cube/bundles/programmer, /opt/st)
    """
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
    return None


def _detect_programmer() -> list[str]:
    """Return the base command list for STM32CubeProgrammer, or raise."""
    cmd = find_cube_programmer()
    if cmd is None:
        raise RuntimeError(
            "STM32CubeProgrammer not found. Install from "
            "https://www.st.com/en/development-tools/stm32cubeprog.html "
            "or install the Cube IDE bundle."
        )
    return cmd


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _strip_ansi(text: str) -> str:
    """Strip ANSI/VT100 escape sequences (cube CLI emits these unconditionally)."""
    return _ANSI_RE.sub("", text) if text else text


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
        return result.returncode, _strip_ansi(result.stdout), _strip_ansi(result.stderr)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Command timed out after {timeout}s: {' '.join(cmd)}")
    except FileNotFoundError:
        raise RuntimeError(f"Command not found: {cmd[0]}")


def _summarize_cube_error(text: str, max_len: int = 1500) -> str:
    """Extract the meaningful error tail from cube CLI output.

    Cube prints a long connect banner first, then the actual Error: lines
    at the end. Prefer Error/Warning lines; fall back to the tail.
    """
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    err_lines = [ln for ln in lines if ln.startswith(("Error", "Warning", "error:"))]
    if err_lines:
        summary = " | ".join(err_lines)
        if len(summary) <= max_len:
            return summary
    tail = "\n".join(lines[-12:])
    return tail[-max_len:]


def _check(cmd: list[str], op: str, timeout: int = 60) -> str:
    rc, stdout, stderr = _run(cmd, timeout=timeout)
    combined = (stdout + stderr).strip()
    if rc != 0:
        raise RuntimeError(f"{op} failed (rc={rc}): {_summarize_cube_error(combined)}")
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

def _probe_info_once(serial: Optional[str], under_reset: bool) -> dict[str, Any]:
    """Single cube ``-i`` attempt. Returns whatever fields we could parse."""
    prog = _detect_programmer()
    connect = _connect_under_reset_args(serial) if under_reset else _connect_args(serial)
    cmd = prog + connect + ["-i"]
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
    if m := re.search(r"Voltage\s*:\s*([0-9.]+)\s*V", combined, re.IGNORECASE):
        info["voltage"] = float(m.group(1))
    if m := re.search(r"Revision ID\s*:\s*(\S.*)", combined, re.IGNORECASE):
        info["revision_id"] = m.group(1).strip()
    if m := re.search(r"BL\s*Version\s*:\s*(\S+)", combined, re.IGNORECASE):
        info["bootloader_version"] = m.group(1).strip()
    if m := re.search(r"ST-LINK\s*FW\s*:\s*(\S+)", combined, re.IGNORECASE):
        info["stlink_fw"] = m.group(1).strip()
    if m := re.search(r"Connect mode\s*:\s*(\S+)", combined, re.IGNORECASE):
        info["connect_mode"] = m.group(1).strip()

    if not info:
        raise RuntimeError(
            f"Could not read target info — is a target connected and powered? "
            f"Output: {combined[:300]}"
        )
    return info


def _try_usb_reset(serial: Optional[str]) -> bool:
    """Best-effort USB reset of the ST-Link. Returns True if attempted."""
    if not serial:
        return False
    try:
        from awto_debug.usb import usb_reset_stlink
    except Exception:
        return False
    try:
        return bool(usb_reset_stlink(serial))
    except Exception:
        return False


def _try_power_cycle(serial: Optional[str]) -> bool:
    """Best-effort uhubctl power-cycle of the ST-Link's USB port. Returns True on success."""
    if not serial:
        return False
    try:
        from awto_debug.usb import power_cycle_stlink
    except Exception:
        return False
    try:
        return bool(power_cycle_stlink(serial))
    except Exception:
        return False


def _check_with_escalation(
    op: str,
    serial: Optional[str],
    build_cmd: Callable[[bool], list[str]],
    timeout: int = 60,
) -> str:
    """Run a cube command with the standard recovery ladder.

    ``build_cmd(under_reset)`` must return the full argv for that mode. Ladder:
      1. normal
      2. UR (connect-under-reset)
      3. usb_reset_stlink + UR
      4. uhubctl VBUS power-cycle + UR  (only on PPPS-capable hubs)

    Raises RuntimeError with a clear power-cycle message if all steps fail.
    """
    try:
        return _check(build_cmd(False), op, timeout=timeout)
    except RuntimeError as exc1:
        try:
            return _check(build_cmd(True), f"{op}(UR)", timeout=timeout)
        except RuntimeError as exc2:
            if _try_usb_reset(serial):
                try:
                    return _check(build_cmd(True), f"{op}(UR+USBReset)", timeout=timeout)
                except RuntimeError:
                    pass
            if _try_power_cycle(serial):
                try:
                    return _check(build_cmd(True), f"{op}(UR+PowerCycle)", timeout=timeout)
                except RuntimeError as exc4:
                    raise RuntimeError(
                        f"{op} failed after normal, UR, USB-reset, and uhubctl "
                        f"power-cycle. Target SWD AP unresponsive — check NRST "
                        f"wiring, RDP level, and target firmware. Last error: {exc4}"
                    ) from exc4
            raise RuntimeError(
                f"{op} failed (normal: {exc1}; UR: {exc2}). Probe USB reset / "
                f"uhubctl power-cycle did not recover the target. Manually "
                f"power-cycle VTarget (remove and reapply) and retry."
            ) from exc2


def probe_info(serial: Optional[str] = None, under_reset: bool = False) -> dict[str, Any]:
    """
    Return target info from CubeProgrammer with auto-escalation.

    Escalation ladder when no ``Device ID`` is read (probe is healthy but
    target SWD AP isn't responding — typical when target firmware reconfigures
    PA13/PA14 or sleeps the core):

      1. normal connect
      2. connect-under-reset (``mode=UR``, holds NRST low)
      3. USB-reset of the ST-Link, then connect-under-reset again

    If all three fail, the result includes ``recovery_hint`` asking the user
    to power-cycle the target. Caller can force step 2 only with
    ``under_reset=True``.
    """
    # Step 1: normal (or caller-forced UR) connect.
    info = _probe_info_once(serial, under_reset=under_reset)
    if "device_id" in info or under_reset:
        return info

    # Step 2: under-reset retry.
    try:
        ur_info = _probe_info_once(serial, under_reset=True)
        if "device_id" in ur_info:
            ur_info.setdefault("connect_mode", "UnderReset")
            return ur_info
    except RuntimeError:
        ur_info = None  # noqa: F841

    # Step 3: USB-reset the probe, then UR connect.
    if _try_usb_reset(serial):
        try:
            ur2 = _probe_info_once(serial, under_reset=True)
            if "device_id" in ur2:
                ur2.setdefault("connect_mode", "UnderReset+USBReset")
                return ur2
        except RuntimeError:
            pass

    # Step 4: uhubctl VBUS power-cycle, then UR connect (PPPS hubs only).
    if _try_power_cycle(serial):
        try:
            ur3 = _probe_info_once(serial, under_reset=True)
            if "device_id" in ur3:
                ur3.setdefault("connect_mode", "UnderReset+PowerCycle")
                return ur3
        except RuntimeError:
            pass

    # Give up — return banner info plus a hint.
    info["recovery_hint"] = (
        "SWD AP did not respond after normal, under-reset, USB-reset, and "
        "uhubctl power-cycle retries. Power-cycle the target manually "
        "(remove and reapply VTarget, or hold NRST low while replugging)."
    )
    return info


def read_words(
    address: int,
    num_words: int,
    serial: Optional[str] = None,
    under_reset: bool = False,
) -> list[int]:
    """Read *num_words* 32-bit little-endian words from *address* via cube ``-r32``.

    Cube's stdout looks like::

        0x1FFF7A10 : 31343637 31335118 00440028

    Returns the words in memory order (low address first).
    """
    if num_words <= 0:
        return []
    prog = _detect_programmer()

    def _attempt(use_ur: bool) -> str:
        connect = _connect_under_reset_args(serial) if use_ur else _connect_args(serial)
        cmd = prog + connect + ["-r32", f"0x{address:08X}", f"0x{num_words * 4:X}"]
        return _check(cmd, "read_words" + ("(UR)" if use_ur else ""), timeout=15)

    try:
        out = _attempt(use_ur=under_reset)
    except RuntimeError as exc1:
        if under_reset:
            raise
        # Step 2: under-reset.
        try:
            out = _attempt(use_ur=True)
        except RuntimeError as exc2:
            # Step 3: USB-reset, then under-reset again.
            if _try_usb_reset(serial):
                try:
                    out = _attempt(use_ur=True)
                except RuntimeError:
                    out = None  # try power-cycle
            else:
                out = None
            if out is None:
                # Step 4: uhubctl VBUS power-cycle, then UR (PPPS hubs only).
                if _try_power_cycle(serial):
                    out = _attempt(use_ur=True)
                else:
                    raise RuntimeError(
                        f"read_words failed (normal: {exc1}; UR: {exc2}); "
                        "USB reset and uhubctl power-cycle did not recover. "
                        "Power-cycle the target manually and retry."
                    ) from exc2
    words: list[int] = []
    for line in out.splitlines():
        if ":" not in line:
            continue
        rhs = line.split(":", 1)[1]
        for tok in rhs.split():
            if len(tok) == 8 and all(c in "0123456789abcdefABCDEF" for c in tok):
                try:
                    words.append(int(tok, 16))
                except ValueError:
                    pass
        if len(words) >= num_words:
            break
    if len(words) < num_words:
        raise RuntimeError(
            f"read_words: expected {num_words} words at 0x{address:08X}, "
            f"got {len(words)}. Output: {out[:300]}"
        )
    return words[:num_words]


def read_uid(serial: Optional[str] = None) -> str:
    """Read the 96-bit STM32 unique device ID (UID).

    The cube CLI ``-uid`` flag is unreliable across families (returns empty on
    H7 etc.), so resolve the UID base address from the cube DB by family and
    read 12 bytes via cube's ``-r32`` memory read instead. Returns the canonical
    ST display form ``w2 w1 w0`` concatenated as 24 hex chars.
    """
    import struct
    from awto_debug import stm32_db

    info = probe_info(serial)
    devid_str = str(info.get("device_id") or "")
    if not devid_str.lower().startswith("0x"):
        raise RuntimeError(f"could not determine device_id for UID lookup: {info!r}")
    db = stm32_db.lookup_device(int(devid_str, 16))
    if db is None or not db.get("uid_address"):
        raise RuntimeError(
            f"no UID address known for device_id {devid_str} "
            f"(series={db.get('series') if db else 'unknown'}); "
            "extend awto_debug.stm32_db._UID_ADDRS"
        )
    w0, w1, w2 = read_words(db["uid_address"], 3, serial)
    return f"{w2:08X}{w1:08X}{w0:08X}"


# ---------------------------------------------------------------------------
# Flash operations
# ---------------------------------------------------------------------------

def flash_write(
    firmware_path: str,
    serial: Optional[str] = None,
    verify: bool = True,
    reset: bool = True,
    address: Optional[str] = None,
    external_loader: Optional[str] = None,
) -> str:
    """
    Flash firmware using CubeProgrammer (-d / download).

    Supports .hex, .bin, .elf, .srec. Uses the standard escalation ladder
    (normal → UR → USB-reset+UR) before failing.

    For .bin files cube requires an explicit write address; pass `address`
    (e.g. "0x08000000"). Auto-defaults to 0x08000000 for .bin if omitted.
    For .hex/.elf/.srec the address is embedded in the file and `address`
    is ignored.

    For external memories (QSPI/OSPI/eMMC), pass `external_loader` with
    the .stldr filename or absolute path (e.g.
    "MT25TL01G_STM32H750B-DISCO.stldr" for the H750B-DK QSPI at 0x90000000).
    """
    prog = _detect_programmer()
    loader = _resolve_external_loader(external_loader)
    is_bin = firmware_path.lower().endswith(".bin")
    write_addr = address if (is_bin and address) else ("0x08000000" if is_bin else None)

    def build(ur: bool) -> list[str]:
        connect = _connect_under_reset_args(serial) if ur else _connect_args(serial)
        cmd = prog + connect
        if loader:
            cmd += ["-el", loader]
        cmd += ["-d", firmware_path]
        if write_addr:
            cmd += [write_addr]
        if verify:
            cmd += ["-v"]
        if reset:
            cmd += ["--hardRst"]
        return cmd

    out = _check_with_escalation("cube flash", serial, build, timeout=180)
    log.info("cube flash complete: %s", firmware_path)
    return f"Flash OK: {firmware_path}\n{out[-300:].strip()}"


def flash_erase(serial: Optional[str] = None) -> str:
    """Mass-erase target flash via CubeProgrammer (with escalation)."""
    prog = _detect_programmer()

    def build(ur: bool) -> list[str]:
        connect = _connect_under_reset_args(serial) if ur else _connect_args(serial)
        return prog + connect + ["-e", "all"]

    out = _check_with_escalation("cube erase", serial, build, timeout=60)
    return f"Erase OK\n{out[-200:].strip()}"


def _resolve_external_loader(name_or_path: Optional[str]) -> Optional[str]:
    """Resolve an external-loader spec to an absolute .stldr path.

    Accepts an absolute path, a bare filename (e.g. 'MT25TL01G_STM32H750B-DISCO.stldr'),
    or a stem (e.g. 'MT25TL01G_STM32H750B-DISCO'). Searches the cube
    install's ExternalLoader/ directory next to the programmer binary,
    plus standard install roots so it works whether _detect_programmer
    returned an absolute CLI path or the `cube programmer` wrapper.
    """
    if not name_or_path:
        return None
    if os.path.isabs(name_or_path) and os.path.isfile(name_or_path):
        return name_or_path
    candidate_names = [name_or_path]
    if not name_or_path.endswith(".stldr"):
        candidate_names.append(name_or_path + ".stldr")

    search_dirs: list[Path] = []
    prog = _detect_programmer()
    if os.path.isabs(prog[0]):
        search_dirs.append(Path(prog[0]).parent / "ExternalLoader")
    # Fallback: scan known install roots for any ExternalLoader directory.
    for root in (
        Path.home() / ".local/share/stm32cube/bundles/programmer",
        Path("/opt/st"),
    ):
        if root.exists():
            search_dirs.extend(p for p in root.rglob("ExternalLoader") if p.is_dir())

    seen: set[str] = set()
    for d in search_dirs:
        key = str(d)
        if key in seen:
            continue
        seen.add(key)
        for cand in candidate_names:
            full = d / cand
            if full.is_file():
                return str(full)
    raise RuntimeError(
        f"External loader not found: {name_or_path}. Searched: "
        + ", ".join(sorted(seen)) if seen else f"External loader not found: {name_or_path}."
    )


def flash_read(
    output_path: str,
    address: str,
    length: int,
    serial: Optional[str] = None,
    external_loader: Optional[str] = None,
) -> str:
    """
    Read a flash region to a binary file.

    Args:
        output_path:     Destination .bin file path.
        address:         Start address (e.g. '0x08000000', '0x90000000').
        length:          Number of bytes.
        external_loader: Optional .stldr name/path for external memory
                         (e.g. QSPI/OSPI). Required for non-internal
                         addresses like 0x90000000 on H750B-DK.
    """
    prog = _detect_programmer()
    loader = _resolve_external_loader(external_loader)

    # Use `-r` (auto access width) rather than `-r64`: forcing 64-bit reads
    # on H7 internal flash @0x08000000 trips a CubeProgrammer failure mid-stream.
    # See issue #4.
    def build(ur: bool) -> list[str]:
        connect = _connect_under_reset_args(serial) if ur else _connect_args(serial)
        cmd = prog + connect
        if loader:
            cmd += ["-el", loader]
        # Cube CLI syntax: -r <address> <size> <file_path>
        return cmd + ["-r", address, hex(length), output_path]

    out = _check_with_escalation("cube read", serial, build, timeout=300)
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
    cmd = find_cube_programmer()
    backend = cmd[0] if cmd else None
    return {
        "backend": backend,
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
