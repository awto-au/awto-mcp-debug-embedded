"""Process detection utilities for awto-flasher.

Finds and manages blocking debug processes that may lock ST-LINK probes.
"""

from __future__ import annotations

import subprocess
import sys


def find_blocking_debug_processes() -> list[tuple[str, str]]:
	"""Return (pid, command) entries for likely probe-locking debug processes.

	Returns:
	    List of (pid, command) tuples for processes that may block probe access.
	"""
	patterns = (
		"stlink-gdbserver",
		"st-util",
		"openocd",
		"ST-LINK_gdbserver",
		"stlinkserver",
	)

	result = subprocess.run(
		["ps", "-eo", "pid=,args="],
		capture_output=True,
		text=True,
		check=False,
	)
	if result.returncode != 0:
		return []

	matches: list[tuple[str, str]] = []
	for raw in result.stdout.splitlines():
		line = raw.strip()
		if not line:
			continue
		parts = line.split(maxsplit=1)
		if len(parts) != 2:
			continue
		pid, cmd = parts
		low = cmd.lower()
		# Exclude ourselves (awto-flasher.py)
		if "scripts/awto" in low:
			continue
		if any(p.lower() in low for p in patterns):
			matches.append((pid, cmd))

	return matches


def abort_if_blocking_debug_processes() -> bool:
	"""Check for blocking debug processes and abort if found.

	Returns:
	    True if blocking processes were found (should abort); False otherwise.
	"""
	blocking = find_blocking_debug_processes()
	if not blocking:
		return False

	print("[flash] Refusing to proceed: blocking debug/server process(es) still running", file=sys.stderr)
	for pid, cmd in blocking:
		print(f"[flash]   pid={pid} cmd={cmd}", file=sys.stderr)
	print("[flash] Stop these processes (or run --cleanup-servers-only) and retry.", file=sys.stderr)
	return True
