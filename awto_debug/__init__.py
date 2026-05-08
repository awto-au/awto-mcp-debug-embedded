"""awto_debug — STM32 debug/programming toolkit.

Cube-primary STM32CubeProgrammer wrapper with reliability fixes (USB-spawn
flock, fast-fail libusb error detection, SWD freq fallback, connect-under-reset,
sector-0 erase recovery, USB-reset retry), plus:

  * USB probe enumeration & VCP mapping (usb)
  * Probe registry (registry) — superset of MCP-server schema
  * Stale GDB-server cleanup (servers)
  * Pre-flash udev/EACCES preflight (preflight)
  * STM32CubeProgrammer wrapper (programmer)
  * OpenOCD fallback (openocd or programmer.program_device_openocd)
  * GDB server launch (gdb_server)
  * VS Code extension toggle (extensions) — opt-in only
  * STM32 OTP block walker (otp)
  * Build-size logger (sizes)
  * Tee-stream + flash runtime helpers (runtime)
  * Probe selector (probe_selector)

Originally adapted from the standalone stlink-toolkit; all modifications
made here only (the upstream toolkit is left untouched).
"""

from __future__ import annotations

# Re-export the most commonly used symbols. Modules are intentionally NOT
# eagerly imported so that lightweight callers (e.g. probe enumeration) don't
# pay the cost of pulling in psutil/cube/openocd dependencies.

from awto_debug.log import get_logger, notice, NOTICE  # noqa: F401

__all__ = ["get_logger", "notice", "NOTICE"]
