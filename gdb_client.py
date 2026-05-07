"""
gdb_client.py — GDB/MI protocol client over TCP.

Connects to any running GDB server (st-util, cube stlink-gdbserver, OpenOCD)
using the GDB Machine Interface (MI) protocol.

Usage:
    from gdb_client import get_client

    client = get_client()
    client.connect(port=4242)
    regs = client.read_registers()
    mem  = client.read_memory("0x20000000", 64)
    bp   = client.set_breakpoint("main")
    client.continue_()
    client.halt()
    client.disconnect()

GDB/MI reference: https://sourceware.org/gdb/current/onlinedocs/gdb/GDB_002fMI.html
"""

from __future__ import annotations

import logging
import re
import select
import socket
import threading
import time
from typing import Any, Optional

log = logging.getLogger("awto.gdb")

# ---------------------------------------------------------------------------
# GDB/MI response parser
# ---------------------------------------------------------------------------

# MI output record types
_RESULT_RE = re.compile(r"^(\d*)\^(done|running|connected|error|exit)(,(.*))?$")
_ASYNC_RE   = re.compile(r"^(\d*)([*+=])(.+)$")
_STREAM_RE  = re.compile(r"^([~@&])\"(.*)\"$")


def _unquote(s: str) -> str:
    """Remove GDB/MI string quoting."""
    if s.startswith('"') and s.endswith('"'):
        s = s[1:-1]
    return s.replace("\\n", "\n").replace("\\t", "\t").replace('\\"', '"').replace("\\\\", "\\")


def _parse_kv(text: str) -> dict[str, Any]:
    """Very basic GDB/MI key=value parser (non-recursive for common cases)."""
    result: dict[str, Any] = {}
    # Simple k="v" pairs
    for m in re.finditer(r'(\w+)="([^"]*)"', text):
        result[m.group(1)] = _unquote(m.group(2))
    return result


class MiResponse:
    """Parsed GDB/MI result record."""

    def __init__(self, token: str, result_class: str, payload: str, raw: str) -> None:
        self.token = token
        self.result_class = result_class     # done | running | connected | error | exit
        self.payload = payload
        self.raw = raw
        self.kv = _parse_kv(payload) if payload else {}

    @property
    def ok(self) -> bool:
        return self.result_class in ("done", "running", "connected")

    @property
    def error_msg(self) -> str:
        return self.kv.get("msg", self.payload or "unknown error")

    def __repr__(self) -> str:
        return f"MiResponse({self.result_class!r}, {self.kv})"


# ---------------------------------------------------------------------------
# GDB/MI client
# ---------------------------------------------------------------------------

class GdbMiClient:
    """
    Thread-safe GDB/MI client over a TCP connection to a GDB server.

    Call connect() before any other methods.
    All send/receive operations are protected by a single lock.
    """

    DEFAULT_TIMEOUT_S = 5.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sock: Optional[socket.socket] = None
        self._token = 0
        self._port: Optional[int] = None
        self._host = "localhost"
        self._buf = ""
        self._halted = False

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self, port: int = 4242, host: str = "localhost", timeout_s: float = 5.0) -> str:
        """
        Connect to the GDB server and send initial MI setup commands.

        Returns a status string. Raises RuntimeError on failure.
        """
        with self._lock:
            if self._sock is not None:
                return f"already connected to {self._host}:{self._port}"
            self._host = host
            self._port = port
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout_s)
            try:
                sock.connect((host, port))
            except ConnectionRefusedError:
                raise RuntimeError(
                    f"GDB server not running on {host}:{port}. "
                    "Start a GDB server first (stlink_gdb_start or cube_gdb_start)."
                )
            except OSError as exc:
                raise RuntimeError(f"GDB connect error: {exc}") from exc
            sock.settimeout(None)
            self._sock = sock
            self._buf = ""

        # Drain any banner output
        self._drain_banner()

        # Set async mode and confirm connection
        self._mi_exec("-gdb-set mi-async on")
        resp = self._mi_exec("-gdb-show version")
        log.info("GDB/MI connected to %s:%d", host, port)
        return f"Connected to GDB server at {host}:{port}"

    def disconnect(self) -> str:
        """Cleanly disconnect from the GDB server."""
        with self._lock:
            if self._sock is None:
                return "not connected"
            try:
                self._sock.sendall(b"-gdb-exit\n")
            except OSError:
                pass
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
            self._halted = False
        log.info("GDB/MI disconnected")
        return "disconnected"

    @property
    def connected(self) -> bool:
        return self._sock is not None

    # ------------------------------------------------------------------
    # Internal MI send/receive
    # ------------------------------------------------------------------

    def _drain_banner(self) -> None:
        """Read and discard initial GDB banner output (stream records)."""
        assert self._sock is not None
        self._sock.settimeout(0.5)
        try:
            while True:
                chunk = self._sock.recv(4096)
                if not chunk:
                    break
        except (socket.timeout, OSError):
            pass
        self._sock.settimeout(None)

    def _next_token(self) -> str:
        with self._lock:
            self._token += 1
            return str(self._token)

    def _send_recv(self, cmd: str, timeout_s: float = DEFAULT_TIMEOUT_S) -> MiResponse:
        """Send a single MI command and return its result record."""
        with self._lock:
            if self._sock is None:
                raise RuntimeError("Not connected to GDB server. Call gdb_connect() first.")
            token = str(self._token + 1)
            self._token += 1
            line = f"{token}{cmd}\n"
            try:
                self._sock.sendall(line.encode())
            except OSError as exc:
                self._sock = None
                raise RuntimeError(f"GDB send error: {exc}") from exc

            return self._recv_result(token, timeout_s)

    def _recv_result(self, token: str, timeout_s: float) -> MiResponse:
        """Read MI output lines until we get the result record matching *token*."""
        assert self._sock is not None
        deadline = time.monotonic() + timeout_s
        self._sock.settimeout(0.2)
        try:
            while time.monotonic() < deadline:
                # Try to get a line from buffer
                while "\n" in self._buf:
                    line, self._buf = self._buf.split("\n", 1)
                    line = line.strip()
                    if not line or line == "(gdb)":
                        continue
                    log.debug("MI< %s", line)
                    if m := _RESULT_RE.match(line):
                        tok, cls, _, payload = m.groups()
                        if tok == token:
                            return MiResponse(tok, cls, payload or "", line)
                    # else: async/stream record — continue reading

                # Read more data from socket
                try:
                    rlist, _, _ = select.select([self._sock], [], [], 0.1)
                    if rlist:
                        chunk = self._sock.recv(4096)
                        if not chunk:
                            raise RuntimeError("GDB server closed the connection")
                        self._buf += chunk.decode(errors="replace")
                except OSError as exc:
                    raise RuntimeError(f"GDB recv error: {exc}") from exc

        finally:
            self._sock.settimeout(None)

        raise RuntimeError(f"GDB/MI timeout waiting for token {token} response")

    def _mi_exec(self, cmd: str, timeout_s: float = DEFAULT_TIMEOUT_S) -> MiResponse:
        """Send MI command; raise RuntimeError if result_class == 'error'."""
        resp = self._send_recv(cmd, timeout_s)
        if not resp.ok:
            raise RuntimeError(f"GDB/MI error: {resp.error_msg}")
        return resp

    # ------------------------------------------------------------------
    # Debug operations
    # ------------------------------------------------------------------

    def halt(self) -> str:
        """Halt (interrupt) the target execution."""
        with self._lock:
            if self._sock is None:
                raise RuntimeError("Not connected")
            # Send Ctrl-C interrupt
            try:
                self._sock.sendall(b"\x03")
            except OSError as exc:
                raise RuntimeError(f"GDB halt error: {exc}") from exc
        time.sleep(0.2)
        self._halted = True
        log.info("Target halted")
        return "halted"

    def continue_(self) -> str:
        """Continue target execution."""
        self._mi_exec("-exec-continue")
        self._halted = False
        return "running"

    def step(self) -> str:
        """Single-step the target (step into)."""
        resp = self._mi_exec("-exec-step")
        return "stepped"

    def step_over(self) -> str:
        """Step over (next)."""
        self._mi_exec("-exec-next")
        return "stepped-over"

    def read_memory(self, address: str, length: int) -> str:
        """
        Read *length* bytes from *address*.

        Returns a hex dump string (address: xx xx xx ...)
        """
        resp = self._mi_exec(
            f"-data-read-memory-bytes {address} {length}",
            timeout_s=10.0,
        )
        # Parse: memory=[{begin=...,offset=...,end=...,contents="hex..."}]
        if m := re.search(r'contents="([0-9a-fA-F]+)"', resp.raw):
            hex_data = m.group(1)
            # Format as hex dump
            chunks = [hex_data[i:i+2] for i in range(0, len(hex_data), 2)]
            lines = []
            addr_int = int(address, 16) if address.startswith("0x") else int(address)
            for i, chunk_start in enumerate(range(0, len(chunks), 16)):
                row = chunks[chunk_start:chunk_start + 16]
                addr_str = f"0x{addr_int + chunk_start:08x}"
                hex_str = " ".join(row)
                lines.append(f"{addr_str}: {hex_str}")
            return "\n".join(lines)
        return resp.raw

    def read_registers(self) -> dict[str, str]:
        """
        Read all CPU registers.

        Returns dict of register_name → value (hex string).
        """
        # Get register names
        resp_names = self._mi_exec("-data-list-register-names")
        names: list[str] = re.findall(r'"([^"]*)"', resp_names.payload or "")

        # Get register values
        resp_vals = self._mi_exec("-data-list-register-values x")
        # Format: register-values=[{number="0",value="0x..."},...] 
        result: dict[str, str] = {}
        for m in re.finditer(r'number="(\d+)",value="([^"]+)"', resp_vals.raw):
            num = int(m.group(1))
            val = m.group(2)
            name = names[num] if num < len(names) else f"r{num}"
            if name:
                result[name] = val
        return result

    def set_breakpoint(self, location: str) -> dict[str, Any]:
        """
        Set a breakpoint at *location* (address '0x...' or symbol name 'main').

        Returns dict with: number, address, function, file, line.
        """
        resp = self._mi_exec(f"-break-insert {location}")
        bp: dict[str, Any] = {}
        if m := re.search(r'number="(\d+)"', resp.raw):
            bp["number"] = int(m.group(1))
        if m := re.search(r'addr="([^"]+)"', resp.raw):
            bp["address"] = m.group(1)
        if m := re.search(r'func="([^"]+)"', resp.raw):
            bp["function"] = m.group(1)
        if m := re.search(r'file="([^"]+)"', resp.raw):
            bp["file"] = m.group(1)
        if m := re.search(r'line="(\d+)"', resp.raw):
            bp["line"] = int(m.group(1))
        return bp

    def delete_breakpoint(self, number: int) -> str:
        """Delete breakpoint by number."""
        self._mi_exec(f"-break-delete {number}")
        return f"Breakpoint {number} deleted"

    def list_breakpoints(self) -> list[dict[str, Any]]:
        """Return all current breakpoints."""
        resp = self._mi_exec("-break-list")
        bps: list[dict[str, Any]] = []
        for m in re.finditer(
            r'number="(\d+)"[^}]*addr="([^"]*)"[^}]*(?:func="([^"]*)")?[^}]*(?:file="([^"]*)")?[^}]*(?:line="(\d+)")?',
            resp.raw,
        ):
            bps.append({
                "number": int(m.group(1)),
                "address": m.group(2),
                "function": m.group(3) or "",
                "file": m.group(4) or "",
                "line": int(m.group(5)) if m.group(5) else None,
            })
        return bps

    def write_memory(self, address: str, hex_data: str) -> str:
        """
        Write bytes to memory.

        Args:
            address:  Target address (e.g. '0x20000100').
            hex_data: Hex string without spaces (e.g. 'deadbeef').
        """
        resp = self._mi_exec(f"-data-write-memory-bytes {address} {hex_data}")
        return f"Wrote {len(hex_data)//2} bytes to {address}"

    def status(self) -> dict[str, Any]:
        """Return current GDB client status."""
        return {
            "connected": self.connected,
            "host": self._host,
            "port": self._port,
            "halted": self._halted,
        }


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_client: Optional[GdbMiClient] = None
_client_lock = threading.Lock()


def get_client() -> GdbMiClient:
    """Return the global GdbMiClient singleton."""
    global _client
    with _client_lock:
        if _client is None:
            _client = GdbMiClient()
    return _client
