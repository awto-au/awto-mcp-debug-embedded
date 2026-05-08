"""Runtime helpers for flash-oriented CLI wrappers.

Provides shared signal logging, tee'd terminal/log-file output, and a compact
final result summary for scripts that wrap STM32CubeProgrammer flows.
"""

from __future__ import annotations

import datetime
import re
import signal
import sys
import time
import traceback
from pathlib import Path
from typing import TextIO

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mK]")
_FREQ_RE = re.compile(r"\bfreq=(\d+)")
_RUNTIME_RE = re.compile(r"\[flash\] Script total runtime:\s*([0-9.]+)s")
_TOTAL_FLASH_TIME_RE = re.compile(r"Total flash time:\s*([0-9.]+)s")


def _ts_ms() -> str:
	return datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]


def _format_signal_context(frame) -> str:
	if frame is None:
		return "<no-frame>"
	code = getattr(frame, "f_code", None)
	filename = getattr(code, "co_filename", "?")
	func = getattr(code, "co_name", "?")
	lineno = getattr(frame, "f_lineno", 0)
	return f"{filename}:{lineno} in {func}()"


class FlashRunState:
	def __init__(self) -> None:
		self.start = time.perf_counter()
		self.modes: list[str] = []
		self.freqs: list[str] = []
		self.saw_erase = False
		self.errors: list[str] = []
		self.saw_success = False
		self.aborted = False
		self.reported_runtime_s: float | None = None
		self.total_flash_time_s: float | None = None

	def ingest(self, line: str) -> bool:
		text = line.strip()
		if not text:
			return False

		m_rt = _RUNTIME_RE.search(text)
		if m_rt:
			try:
				self.reported_runtime_s = float(m_rt.group(1))
			except ValueError:
				self.reported_runtime_s = None
			return True

		m_ft = _TOTAL_FLASH_TIME_RE.search(text)
		if m_ft:
			try:
				self.total_flash_time_s = float(m_ft.group(1))
			except ValueError:
				self.total_flash_time_s = None

		if "cube programmer -c" in text:
			if " incremental" in text:
				self.modes.append("inc")
			elif " -v" in text:
				self.modes.append("full")
			else:
				self.modes.append("unknown")

			m = _FREQ_RE.search(text)
			if m:
				self.freqs.append(f"{m.group(1)}kHz")

		if "Erasing " in text or "erase" in text.lower():
			self.saw_erase = True

		if "Programming complete" in text or "Target already programmed" in text:
			self.saw_success = True

		if "Aborted by user" in text:
			self.aborted = True

		if (
			"ST-LINK error" in text
			or "libusb:" in text
			or "USB transfer errors" in text
			or "All programming attempts failed" in text
			or text.startswith("✗")
		):
			self.errors.append(text)

		return False

	def render_summary(self, exit_code: int) -> str:
		if self.reported_runtime_s is not None:
			elapsed_ms = int(self.reported_runtime_s * 1000)
		else:
			elapsed_ms = int((time.perf_counter() - self.start) * 1000)
		had_process_errors = bool(self.errors)
		if exit_code != 0:
			status = "NO-GO"
		elif had_process_errors:
			status = "GO-WARN"
		else:
			status = "GO"

		if self.modes:
			seen_modes: list[str] = []
			for mode in self.modes:
				if not seen_modes or seen_modes[-1] != mode:
					seen_modes.append(mode)
			mode_text = "->".join(seen_modes)
		else:
			mode_text = "n/a"

		speed_text = self.freqs[-1] if self.freqs else "n/a"
		erase_text = "yes" if self.saw_erase else "no"

		if self.aborted:
			err_text = "aborted"
		elif not self.errors:
			err_text = "none"
		else:
			uniq: list[str] = []
			for err in self.errors:
				if err not in uniq:
					uniq.append(err)
				if len(uniq) == 2:
					break
			err_text = " | ".join(uniq)

		return (
			f"[flash][result] {status} duration={elapsed_ms}ms "
			f"mode={mode_text} speed={speed_text} flash_time="
			f"{self.total_flash_time_s if self.total_flash_time_s is not None else 'n/a'}s "
			f"erase={erase_text} errors={err_text}"
		)


class _TeeStream:
	"""Mirror a stream to terminal + plain-text log file with line timestamps."""

	def __init__(self, original: TextIO, log_fh: TextIO, state: FlashRunState):
		self._orig = original
		self._log = log_fh
		self._state = state
		self._term_buf = ""
		self._log_buf = ""

	def write(self, data: str) -> int:
		if "\r" in data and "\n" not in data:
			self._orig.write(data)
			plain = _ANSI_RE.sub("", data)
			if plain:
				try:
					self._log.write(plain)
				except (OSError, ValueError):
					pass
			return len(data)

		self._term_buf += data
		self._log_buf += _ANSI_RE.sub("", data)

		while "\n" in self._term_buf and "\n" in self._log_buf:
			term_line, self._term_buf = self._term_buf.split("\n", 1)
			log_line, self._log_buf = self._log_buf.split("\n", 1)
			if not log_line.strip():
				continue
			if self._state.ingest(log_line):
				continue
			ts = _ts_ms()
			self._orig.write(f"{ts} {term_line}\n")
			try:
				self._log.write(f"{ts} {log_line}\n")
			except (OSError, ValueError):
				pass

		return len(data)

	def flush(self) -> None:
		self._orig.flush()
		try:
			self._log.flush()
		except (OSError, ValueError):
			pass

	def __getattr__(self, name):
		return getattr(self._orig, name)


class FlashRuntime:
	def __init__(self, *, log_file: Path, argv: list[str] | None = None) -> None:
		self.log_file = log_file
		self.argv = list(sys.argv if argv is None else argv)
		self.current_phase = "startup"
		self.last_signal: dict[str, object] | None = None
		self.flash_state = FlashRunState()

	def set_phase(self, phase: str) -> None:
		self.current_phase = phase

	def install_signal_logging(self) -> None:
		def _handle(signum: int, frame) -> None:
			signal_name = signal.Signals(signum).name
			self.last_signal = {
				"name": signal_name,
				"phase": self.current_phase,
				"location": _format_signal_context(frame),
				"time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
			}
			print(
				f"[flash][signal] Received {signal_name} during phase={self.current_phase} at {self.last_signal['location']}",
				file=sys.stderr,
			)

			prev = signal.default_int_handler if signum == signal.SIGINT else signal.SIG_DFL
			if callable(prev):
				prev(signum, frame)

		signal.signal(signal.SIGINT, _handle)
		signal.signal(signal.SIGTERM, _handle)

	def install_tee(self) -> None:
		log_fh = self.log_file.open("a", encoding="utf-8")
		ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
		cmd = " ".join(self.argv)
		log_fh.write(f"\n{'='*72}\n[flash-log] {ts}  cmd: {cmd}\n{'='*72}\n")
		log_fh.flush()
		sys.stdout = _TeeStream(sys.stdout, log_fh, self.flash_state)
		sys.stderr = _TeeStream(sys.stderr, log_fh, self.flash_state)

	def report_keyboard_interrupt(self) -> None:
		if self.last_signal is not None:
			print(
				"\n[flash] Aborted by user (Ctrl-C) "
				f"after {self.last_signal['name']} during phase={self.last_signal['phase']} "
				f"at {self.last_signal['location']}",
				file=sys.stderr,
			)
			stack = " | ".join(
				f"{frame.name}@{Path(frame.filename).name}:{frame.lineno}"
				for frame in traceback.extract_stack(limit=8)[:-1]
			)
			if stack:
				print(f"[flash][signal] Python stack near abort: {stack}", file=sys.stderr)
		else:
			print(
				f"\n[flash] KeyboardInterrupt with no recorded signal during phase={self.current_phase}",
				file=sys.stderr,
			)

	def report_unhandled_exception(self, exc: Exception) -> None:
		print(
			f"[flash][error] Unhandled {type(exc).__name__} during phase={self.current_phase}: {exc}",
			file=sys.stderr,
		)
		stack = " | ".join(
			f"{frame.name}@{Path(frame.filename).name}:{frame.lineno}"
			for frame in traceback.extract_tb(exc.__traceback__, limit=8)
		)
		if stack:
			print(f"[flash][error] Traceback: {stack}", file=sys.stderr)

	def render_summary(self, exit_code: int) -> str:
		return self.flash_state.render_summary(exit_code)