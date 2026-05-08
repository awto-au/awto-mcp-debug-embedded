"""STM32CubeProgrammer wrapper: probe info, programming, erase, recovery.

Generic helpers that drive `cube programmer` (preferred) or the standalone
`STM32_Programmer_CLI`. MCU- and project-specific values (expected device id,
default ELF, memory regions for size logging) are injected via `configure()`
so the toolkit stays project-agnostic.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import List, Optional, Tuple

from awto_debug import log
from awto_debug.registry import lookup_board, lookup_probe
from awto_debug.servers import (
    print_watcher_summary,
    run_server_cleanup_step,
    stlink_server_watcher,
)
from awto_debug.sizes import log_build_size
from awto_debug.usb import STLinkProbe, find_probes, usb_reset_stlink

# ── Configurable constants (override via configure()) ──────────────────────
MAX_SWD_FREQ = 24000          # KHz - ST-LINK v3 maximum (expected normal path)
FALLBACK_SWD_FREQ = 8000      # KHz - diagnostic/recovery only; indicates link-quality issue
EXPECTED_DEVICE_ID: Optional[str] = None     # e.g. "0x419"
EXPECTED_DEVICE_NAME: Optional[str] = None   # e.g. "STM32F42x/F43x"
# 96-bit STM32 UID base addresses; override per MCU family if needed.
UID_ADDRESSES: Tuple[str, str, str] = ("0x1FFF7A10", "0x1FFF7A14", "0x1FFF7A18")

# ── Subprocess timeouts (seconds) ──────────────────────────────────────────
TIMEOUT_DEVICE_ID_S = 5
TIMEOUT_CPU_SERIAL_S = 10
TIMEOUT_CUBE_PROGRAM_S = 60
TIMEOUT_CUBE_ERASE_S = 30
TIMEOUT_SECTOR_ERASE_S = 20
TIMEOUT_OPENOCD_PROGRAM_S = 120
TIMEOUT_OPENOCD_ERASE_S = 90


def configure(
    *,
    expected_device_id: Optional[str] = None,
    expected_device_name: Optional[str] = None,
    max_swd_freq: Optional[int] = None,
    fallback_swd_freq: Optional[int] = None,
    uid_addresses: Optional[Tuple[str, str, str]] = None,
) -> None:
    global EXPECTED_DEVICE_ID, EXPECTED_DEVICE_NAME
    global MAX_SWD_FREQ, FALLBACK_SWD_FREQ, UID_ADDRESSES
    if expected_device_id is not None:
        EXPECTED_DEVICE_ID = expected_device_id
    if expected_device_name is not None:
        EXPECTED_DEVICE_NAME = expected_device_name
    if max_swd_freq is not None:
        MAX_SWD_FREQ = max_swd_freq
    if fallback_swd_freq is not None:
        FALLBACK_SWD_FREQ = fallback_swd_freq
    if uid_addresses is not None:
        UID_ADDRESSES = uid_addresses


# ── Programmer detection ───────────────────────────────────────────────────
def _detect_programmer() -> List[str]:
    if shutil.which("cube"):
        return ["cube", "programmer"]
    cli = shutil.which("STM32_Programmer_CLI")
    if not cli:
        search_roots = [
            Path.home() / ".local/share/stm32cube/bundles/programmer",
            Path("/opt/st"),
        ]
        candidates: List[Path] = []
        for root in search_roots:
            candidates.extend(root.rglob("STM32_Programmer_CLI") if root.exists() else [])
        candidates.sort(key=lambda p: str(p))
        if candidates:
            cli = str(candidates[-1])
    if cli:
        log.warning("'cube' not found — using standalone STM32_Programmer_CLI: %s", cli)
        return [cli]
    log.error("Neither 'cube' nor 'STM32_Programmer_CLI' found.")
    sys.exit(1)


PROG_CMD: List[str] = _detect_programmer()
PROG = PROG_CMD[0]
OPENOCD = shutil.which("openocd")


# ── Cross-process cube-spawn serializer ────────────────────────────────────
#
# Historical context: cube programmer (libusb under the hood) re-enumerates
# every ST-Link USB device on every invocation to find the requested SN. We
# previously believed two cube processes racing through that enumeration
# window caused libusb EACCES errors. The actual root cause turned out to be
# any ST-Link on the bus exposing a mass-storage / disk-emulation interface:
# the kernel's usb-storage layer claims the device, and cube's libusb_open()
# on it returns EACCES, poisoning EVERY parallel cube invocation regardless
# of `sn=` filtering. See docs/awto-flash.md § "Disable ST-LINK mass-storage".
#
# With every probe on "Debug + VCP only" firmware, parallel cube works
# cleanly and this advisory flock is unnecessary. It remains here, OFF by
# default (_CUBE_SPAWN_HOLD_S=0), as a manual workaround for environments
# where MSC cannot be disabled. Enable via env var STLINK_CUBE_SPAWN_HOLD_S
# or `awto-flasher.py --flash-stagger SECONDS`.
_CUBE_SPAWN_LOCK_PATH = os.environ.get(
    "STLINK_CUBE_SPAWN_LOCK",
    "/tmp/awto-flasher-cube-spawn.lock",
)
_CUBE_SPAWN_HOLD_S = float(os.environ.get("STLINK_CUBE_SPAWN_HOLD_S", "0"))


def set_cube_spawn_hold(seconds: float) -> None:
    """Set the duration the cube-spawn lock is held after spawning cube.

    seconds <= 0 disables the serializer entirely (default).
    Typical useful values: 0.5 .. 2.0 seconds (covers libusb claim window).
    """
    global _CUBE_SPAWN_HOLD_S
    _CUBE_SPAWN_HOLD_S = max(0.0, float(seconds))


def get_cube_spawn_hold() -> float:
    return _CUBE_SPAWN_HOLD_S


@contextlib.contextmanager
def _cube_spawn_serializer():
    """Serialize the cube programmer USB-enumeration window across processes.

    Acquires an advisory exclusive flock on a shared lock file, yields to
    the caller (which immediately spawns cube), then sleeps the configured
    hold duration before releasing. While we hold the lock no other awto
    process can enter cube's libusb enumeration, eliminating EACCES races.
    """
    if _CUBE_SPAWN_HOLD_S <= 0:
        yield
        return
    fd = os.open(_CUBE_SPAWN_LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o666)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        log.info("[cube-spawn] acquired enumeration lock (hold=%.2fs)", _CUBE_SPAWN_HOLD_S)
        yield
        time.sleep(_CUBE_SPAWN_HOLD_S)
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


# ── Connection arg helpers ─────────────────────────────────────────────────
def _probe_connect_args(probe_serial: str, *, freq: int, hard_reset: bool, connect_under_reset: bool = False) -> List[str]:
    args = ["-c", "port=SWD", f"sn={probe_serial}", f"freq={freq}"]
    if connect_under_reset:
        args.append("mode=UR")
    if hard_reset:
        args.append("reset=HWrst")
    return args


def _output_looks_unresponsive(output: str) -> bool:
    markers = (
        "DEV_CONNECT_ERR",
        "DEV_USB_COMM_ERR",
        "No STM32 target found",
        "Unable to get core ID",
        "Error: ST-LINK error",
        "Error: No STM32 target found",
    )
    return any(m in output for m in markers)


# ── Subprocess helper ─────────────────────────────────────────────────────
def _run_prog(cmd: List[str], timeout: int) -> subprocess.CompletedProcess:
    """Run a programmer command in its own session so all child processes
    are killed as a group when the timeout fires."""
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate()
        raise


# ── Probe device-id check ──────────────────────────────────────────────────
def check_probe_device_id(probe: STLinkProbe) -> Optional[str]:
    """Read the SWD device ID via the probe; cache on probe.device_id."""
    for hard_reset in (False, True):
        try:
            cmd = [*PROG_CMD, *_probe_connect_args(probe.serial, freq=4000, hard_reset=hard_reset), "-q"]
            result = _run_prog(cmd, timeout=TIMEOUT_DEVICE_ID_S)
            match = re.search(r"Device ID\s+:\s+(0x[0-9A-Fa-f]+)", result.stdout)
            if match:
                probe.device_id = match.group(1)
                return probe.device_id
        except subprocess.TimeoutExpired:
            log.warning("check_probe_device_id: timeout (hard_reset=%s), probe=%s", hard_reset, probe.last_3)
        except subprocess.CalledProcessError:
            pass
    return None


def is_expected_device(probe: STLinkProbe) -> bool:
    return EXPECTED_DEVICE_ID is not None and probe.device_id == EXPECTED_DEVICE_ID


# ── CPU UID + board lookups ────────────────────────────────────────────────
def read_cpu_serial(probe_serial: str) -> Optional[str]:
    """Read the 96-bit MCU UID through ``probe_serial``; return 24-hex string or None."""
    probe_entry = lookup_probe(probe_serial)
    freq = probe_entry.get("swd_freq", MAX_SWD_FREQ) if probe_entry else MAX_SWD_FREQ
    read_args = []
    for addr in UID_ADDRESSES:
        read_args += ["-r32", addr, "1"]
    for hard_reset in (False, True):
        cmd = [*PROG_CMD, *_probe_connect_args(probe_serial, freq=freq, hard_reset=hard_reset), *read_args]
        try:
            result = _run_prog(cmd, timeout=TIMEOUT_CPU_SERIAL_S)
            words = []
            for addr in UID_ADDRESSES:
                m = re.search(rf"{re.escape(addr)}\s*:\s*([0-9A-Fa-f]{{8}})", result.stdout)
                if m:
                    words.append(m.group(1).upper())
            if len(words) == 3:
                return "".join(words)
        except subprocess.TimeoutExpired:
            log.warning("read_cpu_serial: timeout (hard_reset=%s), probe=%s", hard_reset, probe_serial[-3:])
        except Exception:
            pass
    return None


def elf_build_mode(elf_path: str) -> Optional[str]:
    m = re.search(r"Debug-([A-Z]+)", elf_path)
    return m.group(1) if m else None


def detect_attached_board(probe_serial: str) -> Optional[dict]:
    cpu_serial = read_cpu_serial(probe_serial)
    if not cpu_serial:
        return None
    return lookup_board(cpu_serial)


# ── Registry helpers (interactive) ─────────────────────────────────────────
def register_new_probe(serial: str) -> None:
    from awto_debug.registry import _load_registry, _save_registry, _REGISTRY_PATH

    print(f"\nUnknown ST-Link probe: {serial}")
    label = input("  Probe label/alias (e.g. 935, leave blank to skip): ").strip()
    if not label:
        print("  Skipping registration.")
        return
    model = input("  Probe model (e.g. STLINK-V3MINIE): ").strip() or "unknown"
    try:
        gdb_port = int(input("  GDB server port (e.g. 61235): ").strip())
    except ValueError:
        gdb_port = 0

    reg = _load_registry()
    reg.setdefault("probes", []).append({
        "serial": serial,
        "label": label,
        "model": model,
        "gdb_port": gdb_port,
        "note": "",
    })

    add_board = input("  Register a board for this probe? (y/N): ").strip().lower()
    if add_board == "y":
        board_id = input("  Board id (e.g. pdm-0.0.2, custom-hw-v1): ").strip() or f"unknown-{label}"
        board_label = input("  Board label: ").strip() or board_id
        build_mode = input("  Build mode (default PDM): ").strip().upper() or "PDM"
        print("  Reading CPU serial from target via SWD...")
        cpu_serial = read_cpu_serial(serial)
        if cpu_serial:
            print(f"  cpu_serial: {cpu_serial}")
        else:
            cpu_serial = input("  cpu_serial (24-char hex): ").strip() or None
        board_entry: dict = {
            "id": board_id,
            "label": board_label,
            "build_mode": build_mode,
            "note": "",
        }
        if cpu_serial:
            board_entry["cpu_serial"] = cpu_serial
        reg.setdefault("boards", []).append(board_entry)

    _save_registry(reg)
    print(f"  Saved to {_REGISTRY_PATH}")


def print_board_info(serial: str) -> None:
    probe_entry = lookup_probe(serial)

    if probe_entry:
        label = probe_entry.get("label", serial[-3:])
        model = probe_entry.get("model", "")
        print(f"[registry] Probe '{label}' ({model})")
    else:
        print(f"[registry] Unknown probe {serial} — consider registering it")
        if sys.stdin.isatty():
            ans = input("  Register now? (y/N): ").strip().lower()
            if ans == "y":
                register_new_probe(serial)
        return

    cpu_serial = read_cpu_serial(serial)
    board_entry = lookup_board(cpu_serial) if cpu_serial else None

    if board_entry:
        bid = board_entry.get("id", "?")
        blabel = board_entry.get("label", bid)
        bmode = board_entry.get("build_mode", "?")
        print(f"[registry] Board '{blabel}' (id={bid}, mode={bmode})")
    else:
        if cpu_serial:
            print(f"[registry] No board registered for cpu_serial {cpu_serial}")
            if sys.stdin.isatty():
                ans = input("  Register a board now? (y/N): ").strip().lower()
                if ans == "y":
                    register_new_probe(serial)
        else:
            print("[registry] Could not read CPU serial from target (target unpowered or unreachable)")


def scan_all_probes() -> int:
    from awto_debug.registry import _load_registry

    print("Scanning ST-LINK probes...\n")
    probes = find_probes(list_command=PROG_CMD)
    if not probes:
        log.warning("No ST-LINK probes detected.")
        return 2

    reg = _load_registry()
    reg_probes = {p["serial"]: p for p in reg.get("probes", [])}
    reg_boards = {b["cpu_serial"]: b for b in reg.get("boards", []) if b.get("cpu_serial")}

    rows: List[Tuple[str, str, str, str, str, str]] = []
    all_ok = True
    any_no_target = False
    for probe in probes:
        pe = reg_probes.get(probe.serial)
        plabel = pe.get("label", probe.last_3) if pe else f"?({probe.last_3})"
        pmodel = pe.get("model", "") if pe else "unregistered"
        if pe is None:
            all_ok = False

        cpu_serial = read_cpu_serial(probe.serial)
        if not cpu_serial:
            rows.append((plabel, probe.serial, pmodel, "<no target>", "-", "-"))
            any_no_target = True
            continue

        be = reg_boards.get(cpu_serial)
        if be:
            bid = be.get("id", "?")
            bmode = be.get("build_mode", "?")
            blabel = be.get("label", bid)
        else:
            bid = "<unregistered>"
            bmode = "?"
            blabel = cpu_serial
            all_ok = False

        rows.append((plabel, probe.serial, pmodel, blabel, bmode, bid))

    headers = ("Probe", "Probe SN", "Probe model", "Attached board", "Mode", "Board id")
    widths = [max(len(h), max((len(r[i]) for r in rows), default=0)) for i, h in enumerate(headers)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    print(fmt.format(*("-" * w for w in widths)))
    for r in rows:
        print(fmt.format(*r))
    print()

    if all_ok:
        if any_no_target:
            print("INFO: all registered probes accounted for; one or more have no powered target attached.")
        else:
            print("All probes mapped to a registered board.")
    else:
        print("WARNING: at least one probe is missing a registered probe entry "
              "or is connected to an unregistered board.")
    return 0 if all_ok else 2


def check_elf_matches_board(elf_path: str, probe_serial: str, allow_mismatch: bool) -> bool:
    mode_elf = elf_build_mode(elf_path)
    if not mode_elf:
        return True
    board = detect_attached_board(probe_serial)
    if not board:
        return True
    mode_board = board.get("build_mode")
    if not mode_board or mode_board == mode_elf:
        return True
    print(
        f"[flash] REFUSING to flash: ELF is built for {mode_elf} "
        f"but probe {probe_serial[-3:]} is attached to "
        f"{board.get('label', board.get('id', '?'))} (mode={mode_board}).",
        file=sys.stderr,
    )
    if allow_mismatch:
        print("[flash] --no-mode-check given; continuing anyway.", file=sys.stderr)
        return True
    print("[flash] Pass --no-mode-check to override.", file=sys.stderr)
    return False


# ── Programming / erase ────────────────────────────────────────────────────
def show_device_info(probe: STLinkProbe, is_erase: bool = False) -> None:
    print(f"✓ Selected probe: {probe.serial}")

    if probe.device_id:
        if is_expected_device(probe):
            name = EXPECTED_DEVICE_NAME or "expected device"
            print(f"  Device: {name}")
        else:
            expected = EXPECTED_DEVICE_ID or "?"
            print(f"  ⚠️  Device ID: {probe.device_id} (expected {expected})")

    if is_erase:
        print("\n📊 Expected Erase Speeds:")
        print(f"  • SWD Frequency: {MAX_SWD_FREQ} KHz")
        print("  • Mass erase (1MB): ~3-5 seconds")
    else:
        print("\n📊 Expected Programming Speeds:")
        print(f"  • SWD Frequency: {MAX_SWD_FREQ} KHz")
        print("  • Incremental (unchanged): ~1 second")
        print("  • Full flash: ~9 seconds")
    print()


def program_device(
    elf_path: str,
    probe: STLinkProbe,
    include_sn: bool = False,
    shared: bool = False,
    force_full: bool = False,
    verbose: bool = False,
    passthrough: bool = False,
    timestamps: bool = False,
    freq: Optional[int] = None,
    connect_under_reset: bool = False,
    allow_server_kill: bool = True,
    allow_fallback_freq: bool = False,
) -> Tuple[bool, float]:
    elf_size_kb = os.path.getsize(elf_path) / 1024

    print(f"Programming {probe.serial} (...{probe.last_3}) with {elf_path} ({elf_size_kb:.0f} KB)...")
    print(f"Port: SWD, SN: {probe.serial}")

    primary_freq = freq or MAX_SWD_FREQ
    freq_list: List[int] = [primary_freq]
    if allow_fallback_freq and primary_freq != FALLBACK_SWD_FREQ:
        freq_list.append(FALLBACK_SWD_FREQ)
    for cur_freq in freq_list:
        if cur_freq != primary_freq:
            log.error(
                "Falling back to %d KHz after %d KHz attempts failed — "
                "SERIOUS link issue suspected (USB cable/hub, SWD wiring, probe, or programmer path). "
                "If probe communication and device ID are otherwise stable, %d KHz should work.",
                cur_freq, primary_freq, primary_freq,
            )
        connect_attempts = [connect_under_reset] if connect_under_reset else [False, True]
        for attempt_idx, attempt_under_reset in enumerate(connect_attempts, start=1):
            mode_desc = "connect-under-reset" if attempt_under_reset else "normal-connect"
            print(f"Trying SWD freq={cur_freq} KHz ({mode_desc})...")

            cmd = [*PROG_CMD, "-c", "port=SWD", f"freq={cur_freq}"]
            if attempt_under_reset:
                cmd.append("mode=UR")
            cmd.append("reset=HWrst")
            if include_sn:
                cmd.append(f"sn={probe.serial}")
            if shared:
                cmd.append("shared")
            cmd += ["-d", elf_path]
            if not force_full:
                cmd += ["incremental"]
            else:
                cmd += ["-v"]
            cmd += ["-hardRst"]

            start_time = time.time()
            attempt_unresponsive = False

            _watcher_stop = None
            _watcher_events: List[Tuple[float, int, str]] = []
            _watcher = None
            if allow_server_kill:
                _watcher_stop = threading.Event()
                _watcher = threading.Thread(
                    target=stlink_server_watcher,
                    args=(_watcher_stop, _watcher_events),
                    daemon=True,
                )
                _watcher.start()

            try:
                if passthrough:
                    print("▶ Command:")
                    print("  ", " ".join(cmd))
                    if timestamps:
                        print("[warn] --timestamps is ignored with --passthrough")
                    result = subprocess.run(cmd)
                    elapsed = time.time() - start_time
                    if result.returncode == 0:
                        speed_kbps = int(elf_size_kb / elapsed) if elapsed > 0 else 0
                        print(f"✓ Programming complete in {elapsed:.1f}s ({speed_kbps} KB/s)\n")
                        return True, elapsed
                elif verbose:
                    stdbuf_path = shutil.which("stdbuf")
                    run_cmd = [stdbuf_path, "-oL", "-eL"] + cmd if stdbuf_path else cmd

                    print("▶ Command:")
                    print("  ", " ".join(cmd))
                    if stdbuf_path:
                        print("  (wrapped with stdbuf -oL -eL for line-buffered output)")

                    with _cube_spawn_serializer():
                        proc = subprocess.Popen(
                            run_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                        )

                    start = time.time()
                    libusb_errors: List[str] = []
                    usb_comm_errors: List[str] = []
                    download_complete = False
                    already_programmed = False
                    output_lines: List[str] = []
                    try:
                        for line in iter(proc.stdout.readline, ""):
                            stripped = line.rstrip()
                            output_lines.append(stripped)
                            if timestamps:
                                dt = time.time() - start
                                print(f"[+{dt:6.3f}s] {stripped}")
                            else:
                                print(stripped)
                            if "libusb: error" in stripped:
                                libusb_errors.append(stripped)
                            if "DEV_USB_COMM_ERR" in stripped:
                                usb_comm_errors.append(stripped)
                            if "File download complete" in stripped:
                                download_complete = True
                            if "File is already programmed, no flashing will be done!" in stripped:
                                already_programmed = True
                            # Cube programmer hangs ~70s after these fatal errors before
                            # exiting — kill it immediately, the outcome is decided.
                            if (
                                "libusb: error" in stripped
                                or "Unable to get core ID" in stripped
                                or "DEV_USB_COMM_ERR" in stripped
                            ):
                                print(
                                    f"[flash] Fatal cube error detected — terminating subprocess to skip ~70s hang ({stripped})",
                                    file=sys.stderr,
                                )
                                try:
                                    proc.terminate()
                                    try:
                                        proc.wait(timeout=2.0)
                                    except subprocess.TimeoutExpired:
                                        proc.kill()
                                except Exception:
                                    pass
                                break
                    except KeyboardInterrupt:
                        print("\n[flash] Interrupted — terminating cube programmer...", file=sys.stderr)
                        try:
                            proc.terminate()
                            try:
                                proc.wait(timeout=3.0)
                            except subprocess.TimeoutExpired:
                                proc.kill()
                                proc.wait(timeout=2.0)
                        finally:
                            if _watcher_stop is not None and _watcher is not None:
                                _watcher_stop.set()
                                _watcher.join(timeout=1.0)
                        raise
                    proc.wait()
                    elapsed = time.time() - start_time

                    full_output = "\n".join(output_lines)
                    attempt_unresponsive = _output_looks_unresponsive(full_output)

                    if usb_comm_errors:
                        log.warning(
                            "DEV_USB_COMM_ERR at freq=%d KHz — USB pipe between host and probe timed out. "
                            "Causes: USB 3.0 port, long/cheap cable, unpowered hub, or SWD freq too high. "
                            "Set \"swd_freq\": <lower_value> for probe %s in probes.json to avoid retries.",
                            cur_freq, probe.last_3,
                        )
                    if libusb_errors:
                        print(f"✗ USB transfer errors detected ({len(libusb_errors)} libusb error(s))", file=sys.stderr)
                        for e in libusb_errors:
                            print(f"  {e}", file=sys.stderr)
                    elif not download_complete and not already_programmed:
                        print("✗ 'File download complete' not seen — treating as failure", file=sys.stderr)
                    elif proc.returncode == 0:
                        speed_kbps = int(elf_size_kb / elapsed) if elapsed > 0 else 0
                        if already_programmed and not download_complete:
                            print(f"✓ Target already programmed; no flash needed ({elapsed:.1f}s)\n")
                        else:
                            print(f"✓ Programming complete in {elapsed:.1f}s ({speed_kbps} KB/s)\n")
                        return True, elapsed
                else:
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_CUBE_PROGRAM_S)
                    elapsed = time.time() - start_time
                    print(result.stdout)
                    attempt_unresponsive = _output_looks_unresponsive((result.stdout or "") + "\n" + (result.stderr or ""))
                    if result.returncode == 0:
                        speed_kbps = int(elf_size_kb / elapsed) if elapsed > 0 else 0
                        print(f"✓ Programming complete in {elapsed:.1f}s ({speed_kbps} KB/s)\n")
                        return True, elapsed
            except subprocess.TimeoutExpired:
                print(f"Timeout with freq={cur_freq}")
            except Exception as e:
                print(f"Error with freq={cur_freq}: {e}")
            finally:
                if _watcher_stop is not None and _watcher is not None:
                    _watcher_stop.set()
                    _watcher.join(timeout=1.0)
                    print_watcher_summary(_watcher_events, start_time)
                else:
                    print("[flash] Watcher skipped: reusing shared debugger server session")

            if not attempt_under_reset and (attempt_unresponsive or attempt_idx < len(connect_attempts)):
                log.warning("Target appears unresponsive; retrying with connect-under-reset...")
                continue

            print(f"Attempt with freq={cur_freq} ({mode_desc}) failed, trying next option...")

    log.error("All programming attempts failed for %s", probe.serial)
    return False, 0


def erase_device(
    probe: STLinkProbe,
    include_sn: bool = False,
    shared: bool = False,
    freq: Optional[int] = None,
    connect_under_reset: bool = False,
) -> Tuple[bool, float]:
    print(f"Erasing {probe.serial} (...{probe.last_3})...")
    print(f"Port: SWD, SN: {probe.serial}")

    run_server_cleanup_step("pre-erase cleanup", serial=probe.serial)

    freq_list = [freq] if freq else [MAX_SWD_FREQ]
    for cur_freq in freq_list:
        connect_attempts = [True] if connect_under_reset else [False, True]
        for attempt_under_reset in connect_attempts:
            mode_desc = "connect-under-reset" if attempt_under_reset else "normal-connect"
            print(f"Trying SWD freq={cur_freq} KHz ({mode_desc})...")

            cmd = [*PROG_CMD, "-c", "port=SWD", f"freq={cur_freq}", "reset=HWrst"]
            if attempt_under_reset:
                cmd.append("mode=UR")
            if include_sn:
                cmd.append(f"sn={probe.serial}")
            if shared:
                cmd.append("shared")
            cmd += ["-e", "all", "-y"]

            start_time = time.time()
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_CUBE_ERASE_S)
                elapsed = time.time() - start_time
                print(result.stdout)
                if result.returncode == 0:
                    print(f"✓ Erase complete in {elapsed:.1f}s\n")
                    return True, elapsed
            except subprocess.TimeoutExpired:
                print(f"Timeout with freq={cur_freq}")
            except Exception as e:
                print(f"Error with freq={cur_freq}: {e}")

            if not attempt_under_reset:
                log.warning("Erase failed on normal connect; retrying with connect-under-reset...")
            else:
                log.warning("Attempt with freq=%s (%s) failed.", cur_freq, mode_desc)

    log.error("All erase attempts failed for %s", probe.serial)
    return False, 0


def erase_sector0(probe: STLinkProbe, include_sn: bool = True, freq: Optional[int] = None) -> bool:
    cur_freq = freq or MAX_SWD_FREQ
    print(f"[flash] Erasing sector 0 at {cur_freq} KHz to neutralise running firmware...")
    cmd = [*PROG_CMD, *_probe_connect_args(probe.serial, freq=cur_freq, hard_reset=True, connect_under_reset=True)]
    if not include_sn:
        cmd = [*PROG_CMD, "-c", "port=SWD", f"freq={cur_freq}", "mode=UR", "reset=HWrst"]
    cmd += ["-e", "[0 0]", "-y"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_SECTOR_ERASE_S)
        if result.returncode == 0:
            print("[flash] Sector 0 erased OK")
            return True
        print(f"[flash] Sector 0 erase failed (rc={result.returncode})")
    except Exception as e:
        print(f"[flash] Sector 0 erase error: {e}")
    return False


def _openocd_program_command(elf_path: str, probe_serial: str, freq: int, verify: bool = True) -> List[str]:
    verify_clause = " verify" if verify else ""
    return [
        OPENOCD or "openocd",
        "-d0",
        "-f",
        "interface/stlink.cfg",
        "-f",
        "target/stm32f4x.cfg",
        "-c",
        f'adapter speed {freq}; adapter serial {probe_serial}; init; program "{elf_path}"{verify_clause} reset; shutdown',
    ]


def _openocd_erase_command(probe_serial: str, freq: int) -> List[str]:
    return [
        OPENOCD or "openocd",
        "-d0",
        "-f",
        "interface/stlink.cfg",
        "-f",
        "target/stm32f4x.cfg",
        "-c",
        f"adapter speed {freq}; adapter serial {probe_serial}; init; reset halt; stm32f4x mass_erase 0; shutdown",
    ]


def _openocd_fastpath_marker(probe_serial: str) -> Path:
    """Sidecar file recording last successfully-flashed ELF SHA256 per probe."""
    return Path("/tmp") / f"awto-openocd-lastflash-{probe_serial}.sha256"


def _file_sha256(path: str) -> Optional[str]:
    import hashlib
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _openocd_output_has_error(output: str) -> bool:
    """Detect openocd failure even when returncode==0 (rare but real)."""
    # Strip known-benign messages before scanning for error markers.
    # The GDB server port-binding failure is harmless for ELF programming.
    scrubbed = output
    scrubbed = scrubbed.replace("Error: couldn't bind gdb to socket on port", "")
    scrubbed = scrubbed.replace("Error: couldn't bind tcl to socket on port", "")
    scrubbed = scrubbed.replace("Error: couldn't bind telnet to socket on port", "")
    markers = (
        "Error:",
        "error:",
        "failed",
        "timed out",
        "Polling target",            # only printed when target lost
        "Cannot read \"_program\"",
    )
    return any(m in scrubbed for m in markers)


def program_device_openocd(
    elf_path: str,
    probe: STLinkProbe,
    freq: Optional[int] = None,
    verify: bool = True,
    verbose: bool = False,
) -> Tuple[bool, float]:
    if not OPENOCD:
        print("[flash] error: openocd not found in PATH", file=sys.stderr)
        return False, 0.0

    cur_freq = freq or MAX_SWD_FREQ
    print(f"Programming {probe.serial} (...{probe.last_3}) with OpenOCD")
    print(f"Port: SWD, SN: {probe.serial}, freq={cur_freq} KHz")

    # Fast-path: skip openocd entirely if the ELF SHA256 matches what we
    # last successfully flashed onto this probe. Mirrors cube's "File is
    # already programmed" detection. The sidecar lives in /tmp so it is
    # naturally cleared at boot — a fresh boot will always do a full flash.
    elf_hash = _file_sha256(elf_path)
    marker = _openocd_fastpath_marker(probe.serial)
    if elf_hash and marker.exists():
        try:
            if marker.read_text().strip() == elf_hash:
                elapsed = 0.0
                print(f"✓ Target already programmed (openocd fast-path, sha256 match) in {elapsed:.1f}s\n")
                return True, elapsed
        except OSError:
            pass

    cmd = _openocd_program_command(elf_path, probe.serial, cur_freq, verify=verify)
    print("▶ Command:")
    print("  ", " ".join(cmd))

    start_time = time.time()
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_OPENOCD_PROGRAM_S)
    except subprocess.TimeoutExpired:
        print("[flash] OpenOCD timeout", file=sys.stderr)
        return False, 0.0
    except Exception as exc:
        print(f"[flash] OpenOCD error: {exc}", file=sys.stderr)
        return False, 0.0

    elapsed = time.time() - start_time
    output = (result.stdout or "") + (result.stderr or "")
    verify_marker_ok = "** Verified OK **" in output if verify else True
    success = (
        result.returncode == 0
        and verify_marker_ok
        and not _openocd_output_has_error(output.replace("** Verified OK **", ""))
    )

    if verbose or not success:
        if result.stdout:
            print(result.stdout)
        if result.stderr:
            print(result.stderr, file=sys.stderr)

    if success:
        print(f"✓ OpenOCD programming complete in {elapsed:.1f}s\n")
        if elf_hash:
            try:
                marker.write_text(elf_hash + "\n")
            except OSError:
                pass
        return True, elapsed

    print(f"✗ OpenOCD programming failed (rc={result.returncode})", file=sys.stderr)
    return False, elapsed


def erase_device_openocd(
    probe: STLinkProbe,
    freq: Optional[int] = None,
) -> Tuple[bool, float]:
    if not OPENOCD:
        print("[flash] error: openocd not found in PATH", file=sys.stderr)
        return False, 0.0

    cur_freq = freq or MAX_SWD_FREQ
    print(f"Erasing {probe.serial} (...{probe.last_3}) with OpenOCD")
    print(f"Port: SWD, SN: {probe.serial}, freq={cur_freq} KHz")
    cmd = _openocd_erase_command(probe.serial, cur_freq)
    print("▶ Command:")
    print("  ", " ".join(cmd))

    start_time = time.time()
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_OPENOCD_ERASE_S)
    except subprocess.TimeoutExpired:
        print("[flash] OpenOCD erase timeout", file=sys.stderr)
        return False, 0.0
    except Exception as exc:
        print(f"[flash] OpenOCD erase error: {exc}", file=sys.stderr)
        return False, 0.0

    elapsed = time.time() - start_time
    output = (result.stdout or "") + (result.stderr or "")
    success = result.returncode == 0 and ("shutdown command invoked" in output or "wrote" in output)

    if not success:
        if result.stdout:
            print(result.stdout)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        print(f"✗ OpenOCD erase failed (rc={result.returncode})", file=sys.stderr)
        return False, elapsed

    # Invalidate the fast-path sidecar so the next program goes full-flash.
    try:
        _openocd_fastpath_marker(probe.serial).unlink(missing_ok=True)
    except OSError:
        pass
    print(f"✓ OpenOCD erase complete in {elapsed:.1f}s\n")
    return True, elapsed


def program_with_recovery(
    elf_path: str,
    probe: STLinkProbe,
    allow_server_kill: bool = True,
    **kwargs,
) -> Tuple[bool, float]:
    no_mode_check = kwargs.pop("no_mode_check", False)
    allow_fallback_freq = kwargs.pop("allow_fallback_freq", False)
    fail_fast = kwargs.pop("fail_fast", False)
    connect_under_reset = kwargs.get("connect_under_reset", False)
    t0 = time.monotonic()
    if allow_server_kill:
        run_server_cleanup_step("pre-flash cleanup", serial=probe.serial)
    print_board_info(probe.serial)
    if not check_elf_matches_board(elf_path, probe.serial, no_mode_check):
        return False, 0.0
    success, elapsed = program_device(elf_path, probe, allow_server_kill=allow_server_kill,
                                       allow_fallback_freq=allow_fallback_freq, **kwargs)
    if not success and fail_fast:
        log.error("--fail-fast: aborting after first failed attempt (no retries)")
        total = time.monotonic() - t0
        print(f"Total flash time: {total:.1f}s")
        return False, elapsed
    if not success:
        force_full_was_set = kwargs.get("force_full", False)
        if not force_full_was_set:
            erase_sector0(probe, include_sn=True)
            log.warning("Incremental flash failed; retrying with full flash at max freq...")
            full_kwargs = dict(kwargs)
            full_kwargs["force_full"] = True
            full_kwargs["freq"] = MAX_SWD_FREQ
            success, elapsed = program_device(elf_path, probe, allow_server_kill=allow_server_kill,
                                               allow_fallback_freq=allow_fallback_freq, **full_kwargs)
    if not success:
        log.error("All programming attempts failed; attempting USB reset of ST-Link...")
        if usb_reset_stlink(probe.serial):
            if allow_server_kill:
                run_server_cleanup_step("post-USB-reset cleanup")
            post_reset_kwargs = dict(kwargs)
            post_reset_kwargs["force_full"] = True
            post_reset_kwargs["freq"] = MAX_SWD_FREQ
            post_reset_kwargs["connect_under_reset"] = connect_under_reset
            success, elapsed = program_device(elf_path, probe, allow_server_kill=allow_server_kill,
                                               allow_fallback_freq=allow_fallback_freq, **post_reset_kwargs)
            if not success:
                log.critical("Flash failed even after USB reset — please unplug and replug the ST-Link")
        else:
            log.critical("USB reset unavailable — please unplug and replug the ST-Link")
    total = time.monotonic() - t0
    print(f"Total flash time: {total:.1f}s")
    if success:
        log_build_size(elf_path, mode=elf_build_mode(elf_path) or "GEN")
    return success, elapsed
