import re
import time
from collections import Counter
from typing import Dict, List, Optional, Tuple

STLINK_GDB_PATTERNS = [
    "stlink-gdbserver",
    "ST-LINK_gdbserver",
    "cube stlink-gdbserver",
    "stlink-server",
    "stlink-server-start",
]


def _require_psutil():
    try:
        import psutil  # type: ignore
        return psutil
    except ImportError as exc:
        raise RuntimeError(
            "Missing required support package: psutil. "
            "Install with Fedora dnf: sudo dnf install -y python3-psutil "
            "or with pip: python3 -m pip install psutil"
        ) from exc


def _detect_running_gdb_servers() -> List[Dict[str, object]]:
    psutil = _require_psutil()

    servers: List[Dict[str, object]] = []
    for proc in psutil.process_iter(["pid", "cmdline", "name"]):
        try:
            cmd_tokens = proc.info.get("cmdline") or []
            if not cmd_tokens:
                continue
            joined = " ".join(cmd_tokens)
            if "stlink-gdbserver" not in joined and "ST-LINK_gdbserver" not in joined:
                continue

            serial: Optional[str] = None
            port: Optional[str] = None
            is_shared = "--shared" in cmd_tokens or " shared" in joined

            for idx, tok in enumerate(cmd_tokens):
                if tok == "--serial-number" and idx + 1 < len(cmd_tokens):
                    serial = cmd_tokens[idx + 1]
                elif tok.startswith("--serial-number="):
                    serial = tok.split("=", 1)[1]
                elif tok == "--port-number" and idx + 1 < len(cmd_tokens):
                    port = cmd_tokens[idx + 1]
                elif tok.startswith("--port-number="):
                    port = tok.split("=", 1)[1]

            servers.append({
                "pid": int(proc.info["pid"]),
                "serial": serial,
                "port": port,
                "shared": bool(is_shared),
                "cmdline": joined,
            })
        except Exception:
            continue

    return servers


def find_running_shared_server_for_serial(serial: str) -> Optional[Dict[str, object]]:
    for srv in _detect_running_gdb_servers():
        if not srv.get("shared"):
            continue
        srv_serial = srv.get("serial")
        if isinstance(srv_serial, str) and srv_serial == serial:
            return srv
    return None


def kill_stale_servers(port: str = "", serial: str = "", silent: bool = False) -> List[Tuple[int, str]]:
    psutil = _require_psutil()

    patterns = list(STLINK_GDB_PATTERNS)
    if port:
        patterns.append(rf"--port-number[ =]{re.escape(port)}")
    if serial:
        patterns.append(rf"--serial-number[ =]{re.escape(serial)}")

    killed: List[Tuple[int, str]] = []
    for proc in psutil.process_iter(["pid", "cmdline", "name"]):
        try:
            cmdline = " ".join(proc.info["cmdline"] or [])
            if any(re.search(p, cmdline) for p in patterns):
                short = proc.info["name"] or cmdline[:60]
                if not silent:
                    print(f"[flash] Found blocking process: pid={proc.pid} ({short})")
                proc.kill()
                killed.append((proc.pid, short))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    if killed:
        pids = [pid for pid, _ in killed]
        t0 = time.monotonic()
        deadline = t0 + 2.0
        remaining = list(pids)
        while remaining and time.monotonic() < deadline:
            time.sleep(0.01)
            remaining = []
            for pid in pids:
                try:
                    psutil.Process(pid)
                    remaining.append(pid)
                except psutil.NoSuchProcess:
                    pass
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        if remaining:
            if not silent:
                print(f"[flash] WARNING: PIDs {remaining} did not exit within 2s")
        elif not silent:
            print(f"[flash] Killed {len(killed)} blocking process(es), confirmed dead in {elapsed_ms}ms")
    elif not silent:
        print("[flash] No blocking ST-Link/GDB server processes found")

    return killed


def run_server_cleanup_step(reason: str, port: str = "", serial: str = "") -> List[Tuple[int, str]]:
    print(f"[flash][step] Server cleanup: {reason}")
    if serial:
        print(f"[flash][step] Target serial: {serial}")
    if port:
        print(f"[flash][step] Target port: {port}")
    killed = kill_stale_servers(port=port, serial=serial)
    if killed:
        summary = ", ".join(f"pid={pid} ({name})" for pid, name in killed)
        print(f"[flash][step] Cleanup result: killed {len(killed)} process(es): {summary}")
    else:
        print("[flash][step] Cleanup result: nothing to kill")
    return killed


def stlink_server_watcher(stop_event, events: List[Tuple[float, int, str]], interval: float = 0.3) -> None:
    while not stop_event.is_set():
        killed = kill_stale_servers(silent=True)
        if killed:
            now = time.monotonic()
            for pid, short in killed:
                events.append((now, pid, short))
        stop_event.wait(interval)


def print_watcher_summary(events: List[Tuple[float, int, str]], started_at: float) -> None:
    if not events:
        print("[flash] Watcher summary: no blocking ST-Link process detected during programming")
        return
    counts = Counter(short for _, _, short in events)
    first_dt = events[0][0] - started_at
    summary = ", ".join(f"{name} x{count}" for name, count in counts.items())
    print(f"[flash] Watcher summary: {len(events)} kill event(s); first at +{first_dt:.3f}s; {summary}")
