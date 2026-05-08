"""Coloured logger for awto_debug.

Uses stdlib ``logging`` so callers can add their own handlers/levels.
The default handler writes to stderr with ANSI colour codes:

    ERROR    -> red       (something went wrong; user attention required)
    WARNING  -> yellow    (recoverable but worth noticing)
    NOTICE   -> purple    (custom level 25 — important user-facing event,
                            not a failure condition)
    INFO     -> default
    DEBUG    -> dim grey
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Final

NOTICE: Final[int] = 25
logging.addLevelName(NOTICE, "NOTICE")

_RESET = "\033[0m"
_COLOURS = {
    logging.DEBUG: "\033[2m",        # dim
    logging.INFO: "",                # default
    NOTICE: "\033[35m",              # purple/magenta
    logging.WARNING: "\033[33m",     # yellow
    logging.ERROR: "\033[31m",       # red
    logging.CRITICAL: "\033[1;31m",  # bold red
}


class _ColourFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:  # noqa: D401
        msg = super().format(record)
        if not _use_colour():
            return msg
        colour = _COLOURS.get(record.levelno, "")
        return f"{colour}{msg}{_RESET}" if colour else msg


def _use_colour() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stderr.isatty()


_LOGGER_NAME = "awto.flash"
_logger = logging.getLogger(_LOGGER_NAME)


def _bootstrap() -> None:
    # Inside MCP server, root logger already has colorlog + file/syslog handlers.
    # Propagate so a single sink controls formatting; only attach our own
    # coloured stderr handler when no root handlers are configured (e.g. CLI).
    if _logger.handlers:
        return
    if not logging.getLogger().handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(_ColourFormatter("[%(name)s] %(message)s"))
        _logger.addHandler(handler)
        _logger.propagate = False
    _logger.setLevel(logging.INFO)


_bootstrap()


def get_logger(name: str = _LOGGER_NAME) -> logging.Logger:
    """Return the toolkit logger (or a child logger)."""
    if name == _LOGGER_NAME:
        return _logger
    return _logger.getChild(name)


def notice(msg: str, *args, **kwargs) -> None:
    """Log a NOTICE (purple) — user-visible event that should rarely happen."""
    _logger.log(NOTICE, msg, *args, **kwargs)


def info(msg: str, *args, **kwargs) -> None:
    _logger.info(msg, *args, **kwargs)


def warning(msg: str, *args, **kwargs) -> None:
    _logger.warning(msg, *args, **kwargs)


def error(msg: str, *args, **kwargs) -> None:
    _logger.error(msg, *args, **kwargs)


def critical(msg: str, *args, **kwargs) -> None:
    _logger.critical(msg, *args, **kwargs)


def debug(msg: str, *args, **kwargs) -> None:
    _logger.debug(msg, *args, **kwargs)

def latest_flash_result_line(log_file, start_offset: int) -> str | None:
	"""Return the most recent [flash][result] line written after start_offset.

	Args:
	    log_file: Path-like object or string path to flash log
	    start_offset: Byte offset to start searching from

	Returns:
	    The last [flash][result] line found, or None if not found or error occurred.
	"""
	from collections import deque
	from pathlib import Path

	log_file = Path(log_file)
	if not log_file.exists():
		return None

	try:
		with log_file.open("r", encoding="utf-8", errors="replace") as f:
			f.seek(max(0, start_offset))
			lines = deque(f, maxlen=800)
		for line in reversed(lines):
			line = line.rstrip("\n")
			if "[flash][result]" in line:
				return line
	except Exception:
		return None

	return None


def print_flash_completion(exit_code: int, log_file, start_offset: int) -> None:
	"""Print the flash result to stderr based on log file content or exit code.

	Args:
	    exit_code: Exit code from flash operation
	    log_file: Path-like object or string path to flash log
	    start_offset: Byte offset to start searching for result line
	"""
	result_line = latest_flash_result_line(log_file, start_offset)
	if result_line:
		print(result_line, file=sys.stderr)
		return

	status = "GO" if exit_code == 0 else "NO-GO"
	print(f"[flash][result] {status} exit={exit_code} source=awto-flasher.py", file=sys.stderr)