"""
process_manager.py — long-running process lifecycle for GDB servers, OpenOCD, idf monitor.

ProcessManager is a thread-safe singleton that starts, tracks, and stops
child processes. Each managed process is a ProcessHandle.

Usage:
    from process_manager import get_manager

    mgr = get_manager()
    handle = mgr.start(["st-util", "-p", "4242"], tag="gdb-stlink", port=4242)
    print(handle.pid)
    mgr.stop(handle.id)
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("awto.procmgr")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ProcessHandle:
    id: str                      # UUID string — stable identifier for MCP tools
    tag: str                     # human label, e.g. "gdb-stlink", "openocd-esp"
    cmd: list[str]
    port: Optional[int]          # TCP port if applicable
    pid: int
    started_at: float            # time.monotonic()
    _proc: subprocess.Popen = field(repr=False, compare=False)

    @property
    def alive(self) -> bool:
        return self._proc.poll() is None

    @property
    def returncode(self) -> Optional[int]:
        return self._proc.poll()

    def uptime_s(self) -> float:
        return time.monotonic() - self.started_at

    def as_dict(self) -> dict:
        return {
            "id":         self.id,
            "tag":        self.tag,
            "cmd":        self.cmd,
            "port":       self.port,
            "pid":        self.pid,
            "alive":      self.alive,
            "uptime_s":   round(self.uptime_s(), 1),
            "returncode": self.returncode,
        }


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

class ProcessManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._processes: dict[str, ProcessHandle] = {}
        self._reaper = threading.Thread(target=self._reap_loop, daemon=True, name="proc-reaper")
        self._reaper.start()

    def start(
        self,
        cmd: list[str],
        *,
        tag: str = "",
        port: Optional[int] = None,
        cwd: Optional[str] = None,
        env: Optional[dict] = None,
        startup_wait_s: float = 0.5,
    ) -> ProcessHandle:
        """
        Start a child process and return its handle.

        startup_wait_s: seconds to wait before returning (lets the process
        fail fast if the command is not found or exits immediately).

        Raises RuntimeError if the process exits during the startup window.
        """
        handle_id = str(uuid.uuid4())
        log.info("Starting [%s] %s", tag or handle_id[:8], " ".join(cmd))
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                env=env,
            )
        except FileNotFoundError:
            raise RuntimeError(f"Command not found: {cmd[0]}")
        except OSError as exc:
            raise RuntimeError(f"Failed to start {cmd[0]}: {exc}") from exc

        handle = ProcessHandle(
            id=handle_id,
            tag=tag or cmd[0],
            cmd=cmd,
            port=port,
            pid=proc.pid,
            started_at=time.monotonic(),
            _proc=proc,
        )

        with self._lock:
            self._processes[handle_id] = handle

        # Brief startup window to catch immediate failures
        if startup_wait_s > 0:
            time.sleep(startup_wait_s)
            rc = proc.poll()
            if rc is not None:
                stderr_out = b""
                if proc.stderr:
                    try:
                        stderr_out = proc.stderr.read(2048)
                    except Exception:
                        pass
                with self._lock:
                    self._processes.pop(handle_id, None)
                err_text = stderr_out.decode(errors="replace").strip()
                raise RuntimeError(
                    f"Process [{tag}] exited immediately (rc={rc}): {err_text[:300]}"
                )

        log.info("Process [%s] started pid=%d%s",
                 tag, proc.pid, f" port={port}" if port else "")
        return handle

    def stop(self, handle_id: str, timeout_s: float = 5.0) -> Optional[int]:
        """
        Stop a managed process by handle ID.

        Sends SIGTERM, waits timeout_s, then SIGKILL if still running.
        Returns the process returncode, or None if handle not found.
        """
        with self._lock:
            handle = self._processes.get(handle_id)
        if handle is None:
            log.warning("stop: handle %s not found", handle_id[:8])
            return None

        proc = handle._proc
        log.info("Stopping [%s] pid=%d", handle.tag, handle.pid)
        try:
            proc.terminate()
            try:
                proc.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                log.warning("Process [%s] did not stop — sending SIGKILL", handle.tag)
                proc.kill()
                proc.wait(timeout=2)
        except OSError:
            pass  # already dead

        rc = proc.returncode
        with self._lock:
            self._processes.pop(handle_id, None)
        log.info("Process [%s] stopped rc=%s", handle.tag, rc)
        return rc

    def stop_by_tag(self, tag: str) -> list[int | None]:
        """Stop all processes with the given tag. Returns list of returncodes."""
        with self._lock:
            ids = [h.id for h in self._processes.values() if h.tag == tag]
        return [self.stop(hid) for hid in ids]

    def get(self, handle_id: str) -> Optional[ProcessHandle]:
        with self._lock:
            return self._processes.get(handle_id)

    def list_running(self) -> list[ProcessHandle]:
        """Return all currently-alive managed processes."""
        with self._lock:
            return [h for h in self._processes.values() if h.alive]

    def list_all(self) -> list[dict]:
        """Return all handles (alive + recently dead) as dicts."""
        with self._lock:
            return [h.as_dict() for h in self._processes.values()]

    def stop_all(self) -> None:
        """Stop every managed process (call on server shutdown)."""
        with self._lock:
            ids = list(self._processes.keys())
        for hid in ids:
            try:
                self.stop(hid)
            except Exception as exc:
                log.warning("stop_all: error stopping %s: %s", hid[:8], exc)

    def _reap_loop(self) -> None:
        """Background thread — removes dead processes from the registry after 60s."""
        REAP_AFTER_S = 60.0
        while True:
            time.sleep(10)
            now = time.monotonic()
            with self._lock:
                dead = [
                    hid for hid, h in self._processes.items()
                    if not h.alive and (now - h.started_at) > REAP_AFTER_S
                ]
            for hid in dead:
                with self._lock:
                    handle = self._processes.pop(hid, None)
                if handle:
                    log.debug("Reaped dead process [%s] pid=%d", handle.tag, handle.pid)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_manager: Optional[ProcessManager] = None
_manager_lock = threading.Lock()


def get_manager() -> ProcessManager:
    """Return the global ProcessManager singleton."""
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = ProcessManager()
    return _manager
