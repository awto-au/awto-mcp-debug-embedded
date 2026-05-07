#!/usr/bin/env python3
"""
ttu_cli.py — Command-line interface for awto-mcp-debug-embedded.

Provides quick access to the most common debug operations without
requiring an MCP client.

Usage examples:
    # List probes and approve
    python ttu_cli.py probe list
    python ttu_cli.py probe approve <serial> --nick myboard
    python ttu_cli.py probe ignore  <serial>

    # Flash STM32 with st-flash
    python ttu_cli.py flash stlink firmware.hex

    # Flash STM32 with CubeProgrammer
    python ttu_cli.py flash cube firmware.elf --serial <serial>

    # Flash ESP32
    python ttu_cli.py flash esp firmware.bin --port /dev/ttyUSB0

    # Chip info
    python ttu_cli.py info stlink
    python ttu_cli.py info esp --port /dev/ttyUSB0

    # Start GDB server
    python ttu_cli.py gdb-server stlink --port 4242

    # List background processes
    python ttu_cli.py proc list
    python ttu_cli.py proc stop <handle_id>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


def _print_json(data: object) -> None:
    print(json.dumps(data, indent=2, default=str))


# ---------------------------------------------------------------------------
# Subcommand: probe
# ---------------------------------------------------------------------------

def cmd_probe(args: argparse.Namespace) -> int:
    import probe_detect as pd

    if args.registry:
        pd.configure_registry(args.registry)

    action = args.probe_action

    if action == "list":
        probes = pd.get_all_probes()
        live = {p.serial for p in pd.enumerate_all_probes()}
        if not probes and not live:
            print("No probes detected and registry is empty.")
            return 0
        print(f"{'Serial':<24}  {'Kind':<6}  {'Model':<22}  {'Nick':<14}  {'State':<8}  {'Connected'}")
        print("-" * 95)
        seen: set[str] = set()
        for p in probes:
            seen.add(p.serial)
            print(
                f"{p.serial[-24:]:<24}  {p.kind:<6}  {p.model:<22}  "
                f"{(p.nick or ''):<14}  {p.state:<8}  {'yes' if p.serial in live else 'no'}"
            )
        for lp in pd.enumerate_all_probes():
            if lp.serial not in seen:
                print(
                    f"{lp.serial[-24:]:<24}  {lp.kind:<6}  {lp.model:<22}  "
                    f"{'':14}  {'pending':<8}  yes"
                )
        return 0

    if action == "approve":
        probe = pd.approve_probe(args.serial, args.nick or "")
        if probe is None:
            # Try to find it live
            for lp in pd.enumerate_all_probes():
                if lp.serial == args.serial or lp.serial.endswith(args.serial):
                    pd._registry_upsert_probe(lp)
                    probe = pd.approve_probe(lp.serial, args.nick or "")
                    break
        if probe is None:
            print(f"ERROR: Probe {args.serial!r} not found.", file=sys.stderr)
            return 1
        print(f"Approved: {probe.serial}")
        return 0

    if action == "ignore":
        ok = pd.ignore_probe(args.serial)
        if not ok:
            print(f"ERROR: Probe {args.serial!r} not found.", file=sys.stderr)
            return 1
        print(f"Ignored: {args.serial}")
        return 0

    if action == "rename":
        ok = pd.rename_probe(args.serial, args.nick)
        if not ok:
            print(f"ERROR: Probe {args.serial!r} not found.", file=sys.stderr)
            return 1
        print(f"Renamed: {args.serial} → {args.nick}")
        return 0

    if action == "forget":
        ok = pd.clear_probe(args.serial)
        if not ok:
            print(f"ERROR: Probe {args.serial!r} not found in registry.", file=sys.stderr)
            return 1
        print(f"Forgotten: {args.serial}")
        return 0

    if action == "backends":
        import dataclasses
        from probe_detect import check_backends
        bs = check_backends()
        _print_json(dataclasses.asdict(bs))
        return 0

    print(f"Unknown probe action: {action}", file=sys.stderr)
    return 1


# ---------------------------------------------------------------------------
# Subcommand: info
# ---------------------------------------------------------------------------

def cmd_info(args: argparse.Namespace) -> int:
    target = args.target

    if target in ("stlink", "st"):
        try:
            import debugger_stlink as s
            _print_json(s.chip_info(args.serial or None))
        except RuntimeError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

    elif target in ("cube",):
        try:
            import debugger_cube as c
            _print_json(c.probe_info(args.serial or None))
        except RuntimeError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

    elif target in ("esp",):
        try:
            import debugger_esp as e
            _print_json(e.chip_info(port=args.port or None))
        except RuntimeError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

    else:
        print(f"Unknown target: {target!r}. Use: stlink, cube, esp", file=sys.stderr)
        return 1

    return 0


# ---------------------------------------------------------------------------
# Subcommand: flash
# ---------------------------------------------------------------------------

def cmd_flash(args: argparse.Namespace) -> int:
    target = args.target
    firmware = args.firmware

    if target in ("stlink", "st"):
        try:
            import debugger_stlink as s
            result = s.flash_write(
                firmware,
                address=args.address,
                serial=args.serial or None,
                reset=not args.no_reset,
            )
            print(result)
        except RuntimeError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

    elif target == "cube":
        try:
            import debugger_cube as c
            result = c.flash_write(
                firmware,
                serial=args.serial or None,
                verify=not args.no_verify,
                reset=not args.no_reset,
            )
            print(result)
        except RuntimeError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

    elif target == "esp":
        try:
            import debugger_esp as e
            result = e.flash_write(
                firmware,
                port=args.port or None,
                baud=args.baud,
                offset=args.address,
            )
            print(result)
        except RuntimeError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

    else:
        print(f"Unknown target: {target!r}. Use: stlink, cube, esp", file=sys.stderr)
        return 1

    return 0


# ---------------------------------------------------------------------------
# Subcommand: gdb-server
# ---------------------------------------------------------------------------

def cmd_gdb_server(args: argparse.Namespace) -> int:
    target = args.target

    if target in ("stlink", "st"):
        try:
            import debugger_stlink as s
            handle = s.gdb_server_start(port=args.port, serial=args.serial or None)
            print(f"Started st-util GDB server: port={handle.port}, pid={handle.pid}")
            print(f"Handle ID: {handle.id}")
            print("Press Ctrl-C to stop.")
            handle._proc.wait()
        except (RuntimeError, KeyboardInterrupt) as exc:
            if isinstance(exc, RuntimeError):
                print(f"ERROR: {exc}", file=sys.stderr)
                return 1

    elif target == "cube":
        try:
            import debugger_cube as c
            handle = c.gdb_server_start(port=args.port, serial=args.serial or None)
            print(f"Started CubeProgrammer GDB server: port={handle.port}, pid={handle.pid}")
            print(f"Handle ID: {handle.id}")
            print("Press Ctrl-C to stop.")
            handle._proc.wait()
        except (RuntimeError, KeyboardInterrupt) as exc:
            if isinstance(exc, RuntimeError):
                print(f"ERROR: {exc}", file=sys.stderr)
                return 1

    elif target == "esp":
        try:
            import debugger_esp as e
            handle = e.openocd_start(gdb_port=args.port)
            print(f"Started OpenOCD GDB server: port={handle.port}, pid={handle.pid}")
            print(f"Handle ID: {handle.id}")
            print("Press Ctrl-C to stop.")
            handle._proc.wait()
        except (RuntimeError, KeyboardInterrupt) as exc:
            if isinstance(exc, RuntimeError):
                print(f"ERROR: {exc}", file=sys.stderr)
                return 1

    else:
        print(f"Unknown target: {target!r}. Use: stlink, cube, esp", file=sys.stderr)
        return 1

    return 0


# ---------------------------------------------------------------------------
# Subcommand: proc
# ---------------------------------------------------------------------------

def cmd_proc(args: argparse.Namespace) -> int:
    from process_manager import get_manager

    action = args.proc_action
    mgr = get_manager()

    if action == "list":
        procs = mgr.list_all()
        if not procs:
            print("No managed processes.")
            return 0
        _print_json(procs)

    elif action == "stop":
        rc = mgr.stop(args.handle_id)
        if rc is None:
            print(f"ERROR: Handle {args.handle_id!r} not found.", file=sys.stderr)
            return 1
        print(f"Stopped (rc={rc})")

    else:
        print(f"Unknown proc action: {action!r}", file=sys.stderr)
        return 1

    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ttu_cli",
        description="awto-debug-embedded CLI — quick access to embedded debug tools",
    )
    p.add_argument("--registry", default="probes.json", help="Probe registry file path")
    sub = p.add_subparsers(dest="command", required=True)

    # ── probe ──
    pp = sub.add_parser("probe", help="Probe management")
    pp_sub = pp.add_subparsers(dest="probe_action", required=True)

    pp_sub.add_parser("list", help="List all detected probes")
    pp_sub.add_parser("backends", help="List available debugger backends")

    approve_p = pp_sub.add_parser("approve", help="Approve a pending probe")
    approve_p.add_argument("serial", help="Probe serial (or suffix)")
    approve_p.add_argument("--nick", default="", help="Friendly name")

    ignore_p = pp_sub.add_parser("ignore", help="Ignore a probe")
    ignore_p.add_argument("serial", help="Probe serial")

    rename_p = pp_sub.add_parser("rename", help="Rename a probe")
    rename_p.add_argument("serial", help="Probe serial")
    rename_p.add_argument("nick", help="New nickname")

    forget_p = pp_sub.add_parser("forget", help="Remove probe from registry")
    forget_p.add_argument("serial", help="Probe serial")

    # ── info ──
    ip = sub.add_parser("info", help="Read chip info")
    ip.add_argument("target", choices=["stlink", "st", "cube", "esp"], help="Debugger backend")
    ip.add_argument("--serial", default="", help="ST-Link serial (optional)")
    ip.add_argument("--port", default="", help="Serial port for ESP (optional)")

    # ── flash ──
    fp = sub.add_parser("flash", help="Flash firmware")
    fp.add_argument("target", choices=["stlink", "st", "cube", "esp"], help="Debugger backend")
    fp.add_argument("firmware", help="Path to firmware file (.hex/.bin/.elf)")
    fp.add_argument("--serial", default="", help="ST-Link serial (optional)")
    fp.add_argument("--port", default="", help="Serial port for ESP (optional)")
    fp.add_argument("--address", default="0x8000000", help="Flash base address (default 0x8000000)")
    fp.add_argument("--baud", type=int, default=460800, help="ESP flash baud rate (default 460800)")
    fp.add_argument("--no-reset", action="store_true", help="Do not reset after flash")
    fp.add_argument("--no-verify", action="store_true", help="Skip verify (cube only)")

    # ── gdb-server ──
    gp = sub.add_parser("gdb-server", help="Start a GDB server")
    gp.add_argument("target", choices=["stlink", "st", "cube", "esp"], help="GDB server backend")
    gp.add_argument("--port", type=int, default=4242, help="TCP port (default 4242)")
    gp.add_argument("--serial", default="", help="ST-Link serial (optional)")

    # ── proc ──
    proc_p = sub.add_parser("proc", help="Manage background processes")
    proc_sub = proc_p.add_subparsers(dest="proc_action", required=True)
    proc_sub.add_parser("list", help="List all managed processes")
    stop_p = proc_sub.add_parser("stop", help="Stop a process")
    stop_p.add_argument("handle_id", help="Process handle UUID")

    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    dispatch = {
        "probe":      cmd_probe,
        "info":       cmd_info,
        "flash":      cmd_flash,
        "gdb-server": cmd_gdb_server,
        "proc":       cmd_proc,
    }
    fn = dispatch.get(args.command)
    if fn is None:
        parser.print_help()
        sys.exit(1)

    sys.exit(fn(args))


if __name__ == "__main__":
    main()
