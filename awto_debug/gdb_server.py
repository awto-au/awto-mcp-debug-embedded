"""ST-Link GDB server launcher."""

from __future__ import annotations

import shutil
import signal
import subprocess
import sys
import time
import os
from pathlib import Path
from typing import List, Optional

from awto_debug.programmer import MAX_SWD_FREQ, detect_attached_board
from awto_debug.registry import get_mode_probe_map, lookup_probe
from awto_debug.usb import find_probes


def _detect_cube_programmer_bin() -> Optional[str]:
    """Return STM32CubeProgrammer bin path for cube stlink-gdbserver -cp.

    Search order:
    1. Explicit env override (STM32CUBEPROGRAMMER_BIN)
    2. User-local cube bundle installs
    3. System installs under /opt/st
    """
    env_path = os.environ.get("STM32CUBEPROGRAMMER_BIN")
    if env_path and (Path(env_path) / "STM32_Programmer_CLI").exists():
        return env_path

    search_roots = [
        Path.home() / ".local/share/stm32cube/bundles/programmer",
        Path("/opt/st"),
    ]
    candidates: List[Path] = []
    for root in search_roots:
        if not root.exists():
            continue
        for cli in root.rglob("STM32_Programmer_CLI"):
            candidates.append(cli.parent)

    if not candidates:
        return None

    # Prefer the lexicographically latest path (typically highest version).
    candidates.sort(key=lambda p: str(p))
    return str(candidates[-1])


def _build_gdb_server_cmd(serial: str, port: int, freq: int) -> List[str]:
    if shutil.which("ST-LINK_gdbserver"):
        return [
            "ST-LINK_gdbserver",
            "-p", str(port),
            "-i", "swd",
            "-k",
            "-l", "31",
            "-s", serial,
        ]
    if shutil.which("cube"):
        cmd = [
            "cube", "stlink-gdbserver",
            "--swd",
            "--port-number", str(port),
            "--serial-number", serial,
            "--frequency", str(freq),
            "--shared",
            "--initialize-reset",
            "--verbose",
        ]
        cp = _detect_cube_programmer_bin()
        if cp:
            cmd += ["-cp", cp]
        return cmd
    raise FileNotFoundError("Neither ST-LINK_gdbserver nor cube is available on PATH")


def gdb_server_start(mode: Optional[str] = None, serial: Optional[str] = None, freq: Optional[int] = None) -> int:
    cur_freq = freq or MAX_SWD_FREQ
    t0 = time.monotonic()

    def log(msg: str) -> None:
        elapsed = time.monotonic() - t0
        print(f"[gdbsrv +{elapsed:.3f}s] {msg}")

    if not serial:
        if not mode:
            print("[gdbsrv] error: provide --mode MODE or --sn SERIAL", file=sys.stderr)
            return 1
        mode_upper = mode.upper()
        log(f"Looking for a live probe attached to a {mode_upper} board...")
        live_probes = find_probes()
        log(f"Found {len(live_probes)} probe(s) on USB")
        if not live_probes:
            print("[gdbsrv] error: no ST-LINK probes found", file=sys.stderr)
            return 1
        pinned_map = get_mode_probe_map()
        pinned_sn = pinned_map.get(mode_upper)
        if pinned_sn:
            pinned_probe = next((p for p in live_probes if p.serial == pinned_sn), None)
            if pinned_probe:
                serial = pinned_probe.serial
                log(f"pinned: mode {mode_upper} -> probe {pinned_probe.last_3} ({serial})")
        if not serial:
            matches = []
            for p in live_probes:
                board = detect_attached_board(p.serial)
                if board and board.get("build_mode") == mode_upper:
                    matches.append(p)
            if not matches:
                print(f"[gdbsrv] error: no live probe attached to a {mode_upper} board", file=sys.stderr)
                return 1
            if len(matches) > 1:
                print(f"[gdbsrv] error: {len(matches)} probes match mode {mode_upper} — use --sn", file=sys.stderr)
                return 1
            serial = matches[0].serial
            log(f"detected: mode {mode_upper} -> probe {matches[0].last_3} ({serial})")

    probe_entry = lookup_probe(serial)
    gdb_port = (probe_entry or {}).get("gdb_port") or 61235
    probe_label = (probe_entry or {}).get("label") or serial[-3:]

    log(f"Probe:   {probe_label} ({serial})")
    log(f"Port:    {gdb_port}")
    log(f"SWD:     {cur_freq} KHz")
    log("Press Ctrl+C to stop.\n")

    try:
        cmd = _build_gdb_server_cmd(serial, gdb_port, cur_freq)
    except FileNotFoundError as exc:
        print(f"[gdbsrv] error: {exc}", file=sys.stderr)
        return 1

    proc = subprocess.Popen(cmd)

    def _fwd(signum: int, _frame) -> None:
        if proc.poll() is None:
            proc.send_signal(signum)

    signal.signal(signal.SIGINT, _fwd)
    signal.signal(signal.SIGTERM, _fwd)
    return proc.wait()
