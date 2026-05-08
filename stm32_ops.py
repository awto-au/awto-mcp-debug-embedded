"""
stm32_ops.py - prefer-stlink wrapper around the cube/stlink debuggers.

Polling STM32CubeProgrammer (cube / STM32_Programmer_CLI) is slow and racey:
each invocation re-attaches SWD, prints an interactive banner, and on some
parts (e.g. H7 internal flash via -r64) fails partway through. For repeated,
cheap operations - read region, mass erase, verify - st-flash is faster and
more reliable. Cube is still preferred for things only it does well
(option-byte / OTP read, recovery, .elf flashing).

This module exposes thin wrappers that try st-flash first and fall back to
cube on failure, returning a structured result that records which backend
actually ran. See issue #4.
"""

from __future__ import annotations

import logging
import shutil
from typing import Any, Optional

import debugger_cube as cube
import debugger_stlink as stlink

log = logging.getLogger("awto.stm32_ops")


def _have_stlink() -> bool:
    return shutil.which("st-flash") is not None


def _have_cube() -> bool:
    return cube.find_cube_programmer() is not None


def read_flash(
    output_path: str,
    address: str,
    length: int,
    serial: Optional[str] = None,
) -> dict[str, Any]:
    """Read a flash region. Prefers st-flash; falls back to STM32CubeProgrammer."""
    errors: list[str] = []
    if _have_stlink():
        try:
            out = stlink.flash_read(output_path, address, length, serial)
            return {"backend": "st-flash", "output_path": output_path, "result": out}
        except Exception as exc:
            errors.append(f"st-flash: {exc}")
            log.warning("st-flash read failed, falling back to cube: %s", exc)
    if _have_cube():
        try:
            out = cube.flash_read(output_path, address, length, serial)
            return {
                "backend": "cube",
                "output_path": output_path,
                "result": out,
                "fallback_from": errors or None,
            }
        except Exception as exc:
            errors.append(f"cube: {exc}")
    raise RuntimeError("read_flash failed on all backends: " + " | ".join(errors))


def erase_flash(serial: Optional[str] = None) -> dict[str, Any]:
    """Mass-erase target flash. Prefers st-flash; falls back to cube."""
    errors: list[str] = []
    if _have_stlink():
        try:
            out = stlink.flash_erase(serial)
            return {"backend": "st-flash", "result": out}
        except Exception as exc:
            errors.append(f"st-flash: {exc}")
            log.warning("st-flash erase failed, falling back to cube: %s", exc)
    if _have_cube():
        try:
            out = cube.flash_erase(serial)
            return {"backend": "cube", "result": out, "fallback_from": errors or None}
        except Exception as exc:
            errors.append(f"cube: {exc}")
    raise RuntimeError("erase_flash failed on all backends: " + " | ".join(errors))


def verify_flash(
    firmware_path: str,
    serial: Optional[str] = None,
) -> dict[str, Any]:
    """Verify flash against a firmware file. Prefers st-flash; falls back to cube."""
    errors: list[str] = []
    if _have_stlink():
        try:
            out = stlink.flash_verify(firmware_path, serial)
            return {"backend": "st-flash", "result": out}
        except Exception as exc:
            errors.append(f"st-flash: {exc}")
            log.warning("st-flash verify failed, falling back to cube: %s", exc)
    if _have_cube():
        try:
            # cube.flash_write with verify=True is the closest equivalent;
            # there is no stand-alone "verify only" in CubeProgrammer.
            # Caller can compare sha256 of read_flash output instead.
            raise RuntimeError(
                "STM32CubeProgrammer has no verify-only mode; "
                "use read_flash() and compare sha256 instead."
            )
        except Exception as exc:
            errors.append(f"cube: {exc}")
    raise RuntimeError("verify_flash failed on all backends: " + " | ".join(errors))
