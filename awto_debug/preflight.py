"""Preflight access checks for ST-Link USB nodes.

Performs fast (sub-second) checks BEFORE spawning cube programmer / libusb so
we avoid the 5-30s of confusing libusb retry/USB-reset noise that EACCES on
``/dev/bus/usb/BBB/DDD`` produces. Surfaces a single clear remediation message
pointing at ``stlink-toolkit/scripts/install-udev-rules.sh``.

See GitHub issue awto-au/l8-427#107 and ``docs/awto-flash.md`` § Host udev
rules for background.
"""

from __future__ import annotations

import grp
import os
import pwd
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from awto_debug.usb import _find_all_stlink_usb_devices

UDEV_INSTALLER_REL = "stlink-toolkit/scripts/install-udev-rules.sh"
UDEV_RULE_PATH = "/etc/udev/rules.d/60-awto-stlink.rules"


def _node_path(bus: int, address: int) -> str:
	return f"/dev/bus/usb/{bus:03d}/{address:03d}"


def _stat_perms(path: str) -> Tuple[Optional[int], Optional[str], Optional[str]]:
	"""Return (mode_octal, owner, group) for path or (None, None, None)."""
	try:
		st = os.stat(path)
	except OSError:
		return None, None, None
	owner = group = "?"
	try:
		owner = pwd.getpwuid(st.st_uid).pw_name
	except KeyError:
		owner = str(st.st_uid)
	try:
		group = grp.getgrgid(st.st_gid).gr_name
	except KeyError:
		group = str(st.st_gid)
	return st.st_mode & 0o777, owner, group


def _emit(line: str) -> None:
	print(line, file=sys.stderr, flush=True)


def _emit_remediation(node: str, sn: str) -> None:
	mode, owner, group = _stat_perms(node)
	mode_str = f"{mode:o}" if mode is not None else "?"
	_emit(
		f"[flash][preflight] EACCES on {node} (sn=...{sn[-3:]}, "
		f"owner={owner}, group={group}, perms={mode_str})."
	)
	_emit(f"[flash][preflight] Run: sudo bash {UDEV_INSTALLER_REL}")
	_emit(
		"[flash][preflight] then unplug+replug the probe (or wait for the "
		"next change event)."
	)
	_emit("[flash][preflight] See docs/awto-flash.md § Host udev rules for details.")


def _check_session_plugdev_mismatch() -> bool:
	"""Return True if user is in `plugdev` /etc/group entry but NOT in the
	current process's supplementary groups (i.e. they need to log out / `newgrp
	plugdev` for membership to take effect).

	This is the exact condition that produced the 1 May 2026 confusion: udev
	rule installed correctly, user added to plugdev, but flashing still
	depended on logind's timing-sensitive uaccess ACL because the running
	shell hadn't picked up the new group yet.
	"""
	try:
		g = grp.getgrnam("plugdev")
	except KeyError:
		return False  # handled by _warn_missing_plugdev
	try:
		user = pwd.getpwuid(os.getuid()).pw_name
	except KeyError:
		return False
	if user not in g.gr_mem:
		return False
	return g.gr_gid not in os.getgroups()


def _emit_stale_session_remediation() -> None:
	user = pwd.getpwuid(os.getuid()).pw_name
	_emit(
		f"[flash][preflight] ERROR: '{user}' is listed in /etc/group plugdev "
		f"but the current shell session has not picked up that membership "
		f"(supplementary groups: {sorted(os.getgroups())})."
	)
	_emit(
		"[flash][preflight] Flashing would currently work only via logind's "
		"timing-sensitive uaccess ACL — the exact mode that produces "
		"intermittent EACCES / submit_bulk_transfer errno=14 mid-write."
	)
	_emit("[flash][preflight] Fix: log out and back in (or run `newgrp plugdev` in this shell).")
	_emit("[flash][preflight] Override: re-run with --allow-stale-session-groups (not recommended).")


def _warn_missing_plugdev() -> None:
	"""If `plugdev` group is missing on host, warn — even if uaccess currently works.

	The shipped /etc/udev/rules.d/49-stlinkv*.rules silently fall back to
	root:root when plugdev is absent, leaving access to logind's timing-
	sensitive uaccess ACL — a future regression.
	"""
	try:
		grp.getgrnam("plugdev")
	except KeyError:
		_emit(
			"[flash][preflight] WARNING: host has no `plugdev` group. The "
			"shipped /etc/udev/rules.d/49-stlinkv*.rules silently drop "
			"GROUP=plugdev MODE=0660, leaving probes at logind uaccess only "
			"(timing-sensitive — produces intermittent EACCES)."
		)
		_emit(
			f"[flash][preflight] WARNING: install repo-local rule with: "
			f"sudo bash {UDEV_INSTALLER_REL}"
		)


def _devices_for_serials(serials: Optional[List[str]]) -> List[dict]:
	"""Return USB device records matching the given serials (or all probes)."""
	devs = _find_all_stlink_usb_devices()
	if not serials:
		return devs
	want = {s for s in serials if s}
	return [d for d in devs if (d.get("serial") or "") in want]


def check_access(
	serials: Optional[List[str]] = None,
	*,
	warn_only: bool = False,
	allow_stale_session_groups: bool = False,
) -> bool:
	"""Verify R/W access to the /dev/bus/usb node for each ST-Link probe.

	Args:
	    serials: Specific probe serials to check, or None to check all attached.
	    warn_only: If True, never return False — just emit warnings.
	    allow_stale_session_groups: If True, downgrade the "user in plugdev
	        /etc/group but not in current process groups" check to a warning.

	Returns:
	    True if access OK (or warn_only); False if at least one node is EACCES
	    or the session-group mismatch is detected.
	"""
	_warn_missing_plugdev()

	# Re-attempt enumeration if pyusb itself raised EACCES (which it can do
	# silently, returning empty list). Surface a generic remediation in that
	# case so we don't spuriously claim "no probes".
	devs = _devices_for_serials(serials)
	if not devs and serials:
		_emit(
			f"[flash][preflight] no ST-Link probes visible matching "
			f"serials={','.join(s[-3:] for s in serials)} — probe disconnected, "
			"or libusb itself blocked by EACCES."
		)
		_emit(f"[flash][preflight] Run: sudo bash {UDEV_INSTALLER_REL}")
		return warn_only

	# Check actual device-node access first.  If all nodes are readable+writable
	# (e.g. udev rule uses MODE="0666") the stale-session-group check is moot.
	all_ok = True
	all_accessible = True
	for dev in devs:
		bus = int(dev.get("bus", 0))
		address = int(dev.get("address", 0))
		sn = dev.get("serial") or ""
		node = _node_path(bus, address)
		if not os.path.exists(node):
			# Node not present — can't check; let cube/libusb handle it.
			continue
		if not os.access(node, os.R_OK | os.W_OK):
			all_accessible = False
			_emit_remediation(node, sn)
			if not warn_only:
				all_ok = False

	# Only raise the stale-session warning when devices are NOT accessible via
	# direct permission (i.e. the group membership actually matters here).
	if not all_accessible and _check_session_plugdev_mismatch():
		_emit_stale_session_remediation()
		if not warn_only and not allow_stale_session_groups:
			return False

	return all_ok


def check_perms_main(serials: Optional[List[str]] = None, *, allow_stale_session_groups: bool = False) -> int:
	"""Entrypoint for ``--check-perms`` flag. Exit 0 if access OK, 1 otherwise."""
	ok = check_access(serials, allow_stale_session_groups=allow_stale_session_groups)
	if ok:
		# Emit a positive line so CI gating logs are unambiguous.
		devs = _devices_for_serials(serials)
		count = sum(1 for d in devs if os.path.exists(_node_path(int(d["bus"]), int(d["address"]))))
		_emit(f"[flash][preflight] OK ({count} probe(s) accessible)")
		return 0
	return 1


def check_rule_installed() -> bool:
	"""Return True if the repo-local udev rule appears to be installed."""
	return Path(UDEV_RULE_PATH).is_file()


# ── Parallel-flash bus contention detection ──────────────────────────────────


def _get_probe_bus_groups(serials: List[str]) -> dict:
	"""Return {bus_number: [serial, ...]} for the given probe serials."""
	devs = _devices_for_serials(serials)
	bus_map: dict = {}
	for dev in devs:
		bus = int(dev.get("bus", 0))
		sn = dev.get("serial") or "?"
		bus_map.setdefault(bus, []).append(sn)
	return bus_map


def probes_share_bus(serials: List[str]) -> bool:
	"""Return True if any two of the given probe serials share a USB bus (root hub)."""
	groups = _get_probe_bus_groups(serials)
	return any(len(sns) > 1 for sns in groups.values())


def check_parallel_bus_contention(
	serials: List[str],
	*,
	allow_shared_bus: bool = False,
) -> bool:
	"""Check whether parallel flash is safe given the current USB topology.

	When two cube programmer processes run concurrently on probes attached to the
	same USB bus (root hub / xhci controller), the rapid sector-CRC reads in the
	incremental flash path cause libusb errno=14 (EFAULT / URB submit failure),
	wedging one or both probes and forcing a USB reset+retry cycle (~7s penalty).

	Full erase+download (--full) is not affected because its larger streaming
	transfers tolerate shared-bus concurrency. Incremental is only safe when the
	probes are on separate USB controllers (different Bus number in `lsusb -t`).

	Args:
	    serials: Probe serial numbers to check.
	    allow_shared_bus: If True, accept a shared bus and emit a warning instead
	        of returning False. The caller must then force --full flash to avoid
	        the CRC scan that triggers contention.

	Returns:
	    True  — safe to proceed (separate buses, or shared bus explicitly allowed).
	    False — shared bus detected and allow_shared_bus is False; caller must abort.
	"""
	if len(serials) < 2:
		return True

	groups = _get_probe_bus_groups(serials)
	shared_buses = {bus: sns for bus, sns in groups.items() if len(sns) > 1}

	if not shared_buses:
		buses_desc = ", ".join(f"bus{b:03d}" for b in sorted(groups))
		_emit(
			f"[flash][preflight] bus check OK: probes on separate USB buses "
			f"({buses_desc}) — parallel incremental flash is safe."
		)
		return True

	for bus, sns in sorted(shared_buses.items()):
		_emit(
			f"[flash][preflight] probes {[s[-3:] for s in sns]} share "
			f"USB bus {bus:03d} (same root hub / xhci controller)."
		)
	_emit(
		"[flash][preflight] Parallel flash on a shared USB bus causes libusb "
		"errno=14 (URB submit failure) during concurrent sector-CRC reads "
		"(incremental flash path). Full erase+download is not affected."
	)
	_emit(
		"[flash][preflight] Fix: connect each probe to a port backed by a "
		"DIFFERENT USB controller. Check `lsusb -t`: probes must appear on "
		"separate Bus lines (e.g. Bus 001 and Bus 002)."
	)

	if allow_shared_bus:
		_emit(
			"[flash][preflight] --allow-shared-bus: shared bus accepted. "
			"Incremental CRC scan will be skipped (--full erase+download, ~15s)."
		)
		return True

	_emit(
		"[flash][preflight] Pass --allow-shared-bus to override "
		"(forces --full flash, bypasses the CRC scan that causes contention)."
	)
	return False
