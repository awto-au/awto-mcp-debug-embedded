"""Probe selection and resolution utilities for awto-flasher.

Handles serial/SID/nickname resolution, auto-probe detection, and live probe
discovery with hard-reset recovery.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from awto_debug.registry import get_mode_probe_map, mode_probe_auto_update_enabled, update_mode_probe_map
from awto_debug import programmer as toolkit_programmer
from awto_debug.usb import STLinkProbe, find_probes, usb_reset_stlink

if TYPE_CHECKING:
	from argparse import Namespace


def live_probes() -> list[STLinkProbe]:
	"""Return list of ST-LINK probes currently connected via USB."""
	return find_probes()


def registered_probes(toolkit_root: Path) -> list[dict]:
	"""Load probe registry from probes.json in toolkit root."""
	try:
		data = json.loads((toolkit_root / "probes.json").read_text(encoding="utf-8"))
		probes = data.get("probes", [])
		if isinstance(probes, list):
			return probes
	except Exception:
		pass
	return []


def resolve_serial_from_registry_selector(
	*,
	toolkit_root: Path,
	sid: str | None = None,
	nick: str | None = None,
) -> str:
	"""Resolve probe serial from registry by short-id (label) or nickname.

	Raises:
	    RuntimeError: If selector not found, ambiguous, or missing serial.
	"""
	probes = registered_probes(toolkit_root)

	if sid:
		matches = [p for p in probes if str(p.get("label", "")) == sid]
		if not matches:
			raise RuntimeError(f"No probe label found in probes.json for --sid {sid}")
		if len(matches) > 1:
			raise RuntimeError(f"Multiple probes share label '{sid}' in probes.json")
		serial = matches[0].get("serial")
		if not isinstance(serial, str) or not serial:
			raise RuntimeError(f"Probe label '{sid}' has no serial in probes.json")
		return serial

	if nick:
		nick_l = nick.strip().lower()
		matches = [
			p for p in probes
			if isinstance(p.get("nick"), str) and p.get("nick", "").strip().lower() == nick_l
		]
		if not matches:
			raise RuntimeError(f"No probe nickname found in probes.json for --nick {nick}")
		if len(matches) > 1:
			raise RuntimeError(f"Multiple probes share nickname '{nick}' in probes.json")
		serial = matches[0].get("serial")
		if not isinstance(serial, str) or not serial:
			raise RuntimeError(f"Probe nickname '{nick}' has no serial in probes.json")
		return serial

	raise RuntimeError("Internal error: selector resolution requires sid or nick")


def find_live_probe(serial: str, probes: list[STLinkProbe] | None = None) -> STLinkProbe | None:
	"""Find a live probe by serial number."""
	for probe in probes or live_probes():
		if probe.serial == serial:
			return probe
	return None


def resolve_auto_probe(elf_path: str, toolkit_root: Path, update_map: bool) -> STLinkProbe:
	"""Resolve probe by ELF build mode with optional pinning to mode->probe map.

	Raises:
	    RuntimeError: If build mode cannot be inferred, no probes found, or multiple
	                   probes match the mode.
	"""
	mode = toolkit_programmer.elf_build_mode(elf_path)
	if not mode:
		raise RuntimeError(f"Could not infer build mode from ELF path: {elf_path}")

	probes = live_probes()
	if not probes:
		raise RuntimeError("No ST-LINK probes found")

	pinned_serial = get_mode_probe_map().get(mode.upper())
	if pinned_serial:
		pinned_probe = find_live_probe(pinned_serial, probes)
		if pinned_probe is not None:
			return pinned_probe

	matches: list[STLinkProbe] = []
	for probe in probes:
		board = toolkit_programmer.detect_attached_board(probe.serial)
		if board and board.get("build_mode") == mode.upper():
			matches.append(probe)

	if not matches:
		raise RuntimeError(f"No live probe attached to a {mode.upper()} board")
	if len(matches) > 1:
		raise RuntimeError(f"{len(matches)} probes match mode {mode.upper()} — use --sn")

	selected = matches[0]
	if update_map:
		update_mode_probe_map(mode, selected.serial)
	return selected


def attempt_hard_reset_recovery(reason: str, phase_callback=None) -> bool:
	"""Attempt to recover from probe detection failure via hard USB reset.

	Returns True if any reset was successful; False if no probes available or all resets failed.
	"""
	print(f"[flash] Probe detection failed ({reason}). Attempting hard USB reset recovery...")
	try:
		available = live_probes()
		if not available:
			print("[flash] No probes available for recovery reset", file=sys.stderr)
			return False

		any_success = False
		for probe in available:
			if phase_callback:
				phase_callback(f"recovery-reset:{probe.last_3}")
			try:
				success = usb_reset_stlink(probe.serial)
				if success:
					print(f"[flash] Recovery reset successful: {probe.last_3}")
					any_success = True
				else:
					print(f"[flash] Recovery reset failed: {probe.last_3}", file=sys.stderr)
			except Exception as e:
				print(f"[flash] Recovery reset error on {probe.last_3}: {e}", file=sys.stderr)

		if any_success:
			print("[flash] Waiting for probes to re-enumerate...")
			time.sleep(1.5)  # Wait for USB re-enumeration
			return True
		return False
	except Exception as e:
		print(f"[flash] Recovery reset attempt failed: {e}", file=sys.stderr)
		return False


def resolve_selected_probes(
	args: Namespace,
	toolkit_root: Path,
	elf_path: str | None = None,
	phase_callback=None,
) -> list[STLinkProbe]:
	"""Resolve the list of probes to operate on based on CLI arguments.

	Supports: --all, --sn SERIAL, --sid LABEL, --nick NAME, --auto-probe
	Also handles hard-reset recovery attempts.

	Raises:
	    RuntimeError: If probe resolution fails or ambiguous.
	"""
	if sum(1 for v in (args.sn, args.sid, args.nick) if v) > 1:
		raise RuntimeError("Use only one of --sn, --sid, or --nick")

	if args.all:
		probes = live_probes()
		if not probes:
			raise RuntimeError("No ST-LINK probes found")
		return probes

	serial = args.sn
	if not serial and (args.sid or args.nick):
		serial = resolve_serial_from_registry_selector(
			toolkit_root=toolkit_root,
			sid=args.sid,
			nick=args.nick,
		)

	if serial:
		probe = find_live_probe(serial)
		if probe is None:
			# Attempt recovery via hard reset
			if attempt_hard_reset_recovery(f"probe {serial} not found", phase_callback):
				probe = find_live_probe(serial)
			if probe is None:
				raise RuntimeError(f"Probe not found on USB: {serial}")
		return [probe]

	if args.auto_probe:
		if not elf_path:
			raise RuntimeError("--auto-probe requires an ELF path")
		update_map = args.auto_update_mode_map or mode_probe_auto_update_enabled()
		try:
			return [resolve_auto_probe(elf_path, toolkit_root, update_map)]
		except RuntimeError as e:
			# Attempt recovery via hard reset
			if attempt_hard_reset_recovery(str(e), phase_callback):
				return [resolve_auto_probe(elf_path, toolkit_root, update_map)]
			raise

	probes = live_probes()
	if len(probes) == 1:
		return probes
	if not probes:
		# Attempt recovery via hard reset
		if attempt_hard_reset_recovery("no probes found", phase_callback):
			probes = live_probes()
			if probes:
				return probes
		raise RuntimeError("No ST-LINK probes found")
	raise RuntimeError("Multiple probes detected — use --sn, --auto-probe, or --all")
