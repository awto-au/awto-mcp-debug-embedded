"""Temporarily disable / re-enable VS Code extensions via its state database.

VS Code persists its disabled-extension list in a SQLite database
(~/.config/Code/User/globalStorage/state.vscdb).  Writing directly to that
DB is the only way to disable an extension without uninstalling it — VS Code's
CLI ``--disable-extension`` flag only affects the single launch it is passed to.

VS Code must be restarted (or the extension host reloaded) for changes to
take effect.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

# Default ST debugging-related VS Code extension IDs.
DEFAULT_ST_EXTENSION_IDS: Tuple[str, ...] = (
    "stmicroelectronics.stm32-vscode-extension",
    "stmicroelectronics.stm32cube-ide-debug-core",
    "stmicroelectronics.stm32cube-ide-debug-generic-gdbserver",
    "stmicroelectronics.stm32cube-ide-debug-stlink-gdbserver",
    "stmicroelectronics.stm32cube-ide-registers",
    "eclipse-cdt.memory-inspector",
    "mcu-debug.memory-view",
    "mcu-debug.debug-tracker-vscode",
    "marus25.cortex-debug",
    "mcu-debug.rtos-views",
    "stmicroelectronics.stm32cube-ide-rtos",
    "mcu-debug.peripheral-viewer",
    "ms-vscode.vscode-embedded-tools",
    "stmicroelectronics.stm32cube-ide-debug-jlink-gdbserver",
)

# Public alias — use this name in app-level code and docs.
ST_DEBUG_EXTENSIONS = DEFAULT_ST_EXTENSION_IDS

_DISABLED_KEY = "extensionsIdentifiers/disabled"


def _state_db_path() -> Path:
    candidates = [
        Path.home() / ".config/Code/User/globalStorage/state.vscdb",
        Path.home() / ".config/Code - Insiders/User/globalStorage/state.vscdb",
        Path.home() / "Library/Application Support/Code/User/globalStorage/state.vscdb",
        Path.home() / "AppData/Roaming/Code/User/globalStorage/state.vscdb",
    ]
    for p in candidates:
        if p.exists():
            return p
    raise RuntimeError(
        "VS Code state.vscdb not found. Is VS Code installed? "
        f"Searched: {', '.join(str(p) for p in candidates)}"
    )


def _ext_uuid(ext_id: str) -> Optional[str]:
    """Return the marketplace UUID from the installed extension's package.json."""
    ext_dir = Path.home() / ".vscode" / "extensions"
    if not ext_dir.is_dir():
        return None
    prefix = ext_id.lower() + "-"
    for d in sorted(ext_dir.iterdir(), reverse=True):  # newest version first
        if d.is_dir() and d.name.lower().startswith(prefix):
            pkg = d / "package.json"
            if pkg.exists():
                try:
                    meta = json.loads(pkg.read_text()).get("__metadata", {})
                    uid = meta.get("id")
                    if uid:
                        return str(uid)
                except Exception:
                    pass
    return None


def _read_disabled(db: Path) -> List[Dict]:
    con = sqlite3.connect(str(db))
    try:
        row = con.execute(
            "SELECT value FROM ItemTable WHERE key=?", (_DISABLED_KEY,)
        ).fetchone()
        if row is None:
            return []
        return json.loads(row[0]) or []
    finally:
        con.close()


def _write_disabled(db: Path, entries: List[Dict]) -> None:
    con = sqlite3.connect(str(db))
    try:
        con.execute(
            "INSERT OR REPLACE INTO ItemTable (key, value) VALUES (?, ?)",
            (_DISABLED_KEY, json.dumps(entries)),
        )
        con.commit()
    finally:
        con.close()


def _is_installed(ext_id: str) -> bool:
    """Return True if the extension folder exists in ~/.vscode/extensions/."""
    ext_dir = Path.home() / ".vscode" / "extensions"
    if not ext_dir.is_dir():
        return False
    prefix = ext_id.lower() + "-"
    return any(d.name.lower().startswith(prefix) for d in ext_dir.iterdir() if d.is_dir())


def disable_extensions(
    ext_ids: Iterable[str] = DEFAULT_ST_EXTENSION_IDS,
) -> List[Tuple[str, str]]:
    """Add extensions to VS Code's persistent disabled list.

    Only disables extensions that are actually installed; silently skips others.
    Returns list of (ext_id, status) where status is one of:
    'disabled', 'already-disabled', 'not-installed'.
    """
    db = _state_db_path()
    entries = _read_disabled(db)
    existing_ids = {e["id"].lower() for e in entries}
    results: List[Tuple[str, str]] = []
    changed = False
    for ext in ext_ids:
        if not _is_installed(ext):
            results.append((ext, "not-installed"))
            continue
        if ext.lower() in existing_ids:
            print(f"[ext] already disabled: {ext}")
            results.append((ext, "already-disabled"))
            continue
        entry: Dict = {"id": ext}
        uuid = _ext_uuid(ext)
        if uuid:
            entry["uuid"] = uuid
        entries.append(entry)
        changed = True
        print(f"[ext] disabled: {ext}")
        results.append((ext, "disabled"))
    if changed:
        _write_disabled(db, entries)
    return results


def enable_extensions(
    ext_ids: Iterable[str] = DEFAULT_ST_EXTENSION_IDS,
) -> List[Tuple[str, str]]:
    """Remove extensions from VS Code's persistent disabled list.

    Returns list of (ext_id, status) where status is one of:
    'enabled', 'already-enabled'.
    """
    db = _state_db_path()
    entries = _read_disabled(db)
    ids_to_remove = {e.lower() for e in ext_ids}
    was_disabled = {e["id"].lower() for e in entries}
    new_entries = [e for e in entries if e["id"].lower() not in ids_to_remove]
    results: List[Tuple[str, str]] = []
    for ext in ext_ids:
        if ext.lower() in was_disabled:
            print(f"[ext] enabled: {ext}")
            results.append((ext, "enabled"))
        else:
            print(f"[ext] already enabled: {ext}")
            results.append((ext, "already-enabled"))
    _write_disabled(db, new_entries)
    return results


def disable_extensions_for_flash(
    ext_ids: Iterable[str] = DEFAULT_ST_EXTENSION_IDS,
) -> List[Tuple[str, str]]:
    """Disable ST debug extensions before a flash operation (with step logging)."""
    print("[flash][step] Disabling ST debug extensions")
    results = disable_extensions(ext_ids)
    disabled = [ext for ext, status in results if status == "disabled"]
    skipped = [ext for ext, status in results if status == "not-installed"]
    already = [ext for ext, status in results if status == "already-disabled"]
    if disabled:
        print(f"[flash][step] Disabled {len(disabled)} extension(s): {', '.join(e.split('.')[-1] for e in disabled)}")
    if already:
        print(f"[flash][step] Already disabled: {', '.join(e.split('.')[-1] for e in already)}")
    if skipped:
        print(f"[flash][step] Not installed (skipped): {', '.join(e.split('.')[-1] for e in skipped)}")
    return results


def enable_extensions_after_flash(
    ext_ids: Iterable[str] = DEFAULT_ST_EXTENSION_IDS,
) -> List[Tuple[str, str]]:
    """Re-enable ST debug extensions after a flash operation (with step logging)."""
    print("[flash][step] Re-enabling ST debug extensions")
    results = enable_extensions(ext_ids)
    enabled = [ext for ext, status in results if status == "enabled"]
    already = [ext for ext, status in results if status == "already-enabled"]
    if enabled:
        print(f"[flash][step] Re-enabled {len(enabled)} extension(s): {', '.join(e.split('.')[-1] for e in enabled)}")
    if already:
        print(f"[flash][step] Already enabled: {', '.join(e.split('.')[-1] for e in already)}")
    return results


def list_installed_extensions(code_cmd: str = "code") -> List[str]:
    """Return list of installed extension IDs via the VS Code CLI."""
    resolved = shutil.which(code_cmd)
    if resolved is None:
        raise RuntimeError(f"VS Code CLI '{code_cmd}' not found on PATH.")
    result = subprocess.run(
        [resolved, "--list-extensions"],
        check=True,
        capture_output=True,
        text=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]
