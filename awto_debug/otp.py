"""Generic STM32 OTP block walker — Python mirror of l8_library/awto_otp.

Reads the 512-byte OTP region (16 blocks * 32 bytes) at OTP_BASE through the
ST-LINK using ``cube programmer -r8``. Writes individual bytes with ``-w8``.

Block layout (matches awto_otp.h):
    offset  size  field
    ------  ----  -----
     0      1     valid_flag    0xFF=valid, 0x00=invalidated
     1      1     schema_ver    caller-defined
     2      2     reserved      0xFFFF
     4      4     crc32         CRC-32 over bytes 8..31 (payload)
     8      24    payload       caller-defined opaque bytes

Lock byte at OTP_LOCK_BASE+N (one per block); 0x00 means block is permanently
locked. Writing OTP is bits-only (1 -> 0); 0xFF means erased.
"""

from __future__ import annotations

import re
import subprocess
import zlib
from dataclasses import dataclass
from typing import List, Optional

from awto_debug import log
from awto_debug.programmer import PROG_CMD, _probe_connect_args, _run_prog

OTP_BASE = 0x1FFF7800
OTP_LOCK_BASE = 0x1FFF7A00
OTP_BLOCK_COUNT = 16
OTP_BLOCK_SIZE = 32
OTP_HEADER_SIZE = 8
OTP_PAYLOAD_MAX = OTP_BLOCK_SIZE - OTP_HEADER_SIZE  # 24
OTP_TOTAL_SIZE = OTP_BLOCK_COUNT * OTP_BLOCK_SIZE   # 512

STATE_FREE = "free"
STATE_ACTIVE = "active"
STATE_INVALIDATED = "invalidated"
STATE_CORRUPT = "corrupt"


@dataclass
class OtpBlock:
    index: int
    raw: bytes              # 32 bytes
    state: str              # STATE_*
    schema_ver: Optional[int]
    valid_flag: int
    crc_stored: int
    crc_computed: int
    payload: bytes          # 24 bytes
    locked: bool


@dataclass
class OtpView:
    raw: bytes              # full 512 bytes
    blocks: List[OtpBlock]
    locks: bytes            # 16 bytes
    active_index: Optional[int]


def _crc32(payload: bytes) -> int:
    # zlib.crc32 is IEEE 802.3 (polynomial 0xEDB88320), matches awto_otp.c
    return zlib.crc32(payload) & 0xFFFFFFFF


def _classify(raw: bytes, locked: bool) -> OtpBlock:
    valid_flag = raw[0]
    schema_ver = raw[1]
    crc_stored = int.from_bytes(raw[4:8], "little")
    payload = raw[OTP_HEADER_SIZE:OTP_BLOCK_SIZE]
    crc_computed = _crc32(payload)

    if valid_flag == 0x00:
        state = STATE_INVALIDATED
        sv: Optional[int] = None
    elif valid_flag != 0xFF:
        state = STATE_CORRUPT
        sv = None
    elif raw == b"\xff" * OTP_BLOCK_SIZE:
        state = STATE_FREE
        sv = None
    elif schema_ver == 0xFF:
        state = STATE_CORRUPT
        sv = None
    elif crc_stored != crc_computed:
        state = STATE_CORRUPT
        sv = schema_ver
    else:
        state = STATE_ACTIVE
        sv = schema_ver

    return OtpBlock(
        index=-1,
        raw=raw,
        state=state,
        schema_ver=sv,
        valid_flag=valid_flag,
        crc_stored=crc_stored,
        crc_computed=crc_computed,
        payload=payload,
        locked=locked,
    )


# ── ST-LINK I/O ────────────────────────────────────────────────────────────
def _read_bytes(probe_serial: str, addr: int, count: int, *, freq: int = 24000, timeout: int = 10) -> bytes:
    """Read ``count`` bytes starting at ``addr`` via cube programmer ``-r8``."""
    cmd = [
        *PROG_CMD,
        *_probe_connect_args(probe_serial, freq=freq, hard_reset=False),
        "-r8", f"0x{addr:08X}", str(count),
    ]
    res = _run_prog(cmd, timeout=timeout)
    if res.returncode != 0:
        raise RuntimeError(f"cube programmer -r8 failed: rc={res.returncode}\n{res.stdout}\n{res.stderr}")
    return _parse_r8_dump(res.stdout, addr, count)


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_DUMP_LINE = re.compile(r"^\s*(0x[0-9A-Fa-f]+)\s*:\s*((?:[0-9A-Fa-f]{2}[ \t]*)+)", re.MULTILINE)


def _parse_r8_dump(text: str, addr: int, count: int) -> bytes:
    """Parse the cube programmer hex dump output into raw bytes."""
    text = _ANSI_RE.sub("", text)
    chunks: dict[int, bytes] = {}
    for m in _DUMP_LINE.finditer(text):
        line_addr = int(m.group(1), 16)
        hex_bytes = m.group(2).split()
        chunks[line_addr] = bytes(int(b, 16) for b in hex_bytes)
    if not chunks:
        raise RuntimeError(f"could not parse -r8 output:\n{text}")
    out = bytearray()
    cur = addr
    while len(out) < count:
        if cur not in chunks:
            raise RuntimeError(f"missing dump line for 0x{cur:08X} in:\n{text}")
        chunk = chunks[cur]
        out.extend(chunk)
        cur += len(chunk)
    return bytes(out[:count])


def _write_byte(probe_serial: str, addr: int, value: int, *, freq: int = 24000, timeout: int = 10) -> None:
    """Program a single byte via cube programmer ``-w8``.

    NOTE: cube programmer requires 4-byte alignment for ``-w8`` against OTP/flash,
    so this only works for byte addresses that fall on a 4-byte boundary. For
    arbitrary-byte writes use :func:`_write_word`.
    """
    cmd = [
        *PROG_CMD,
        *_probe_connect_args(probe_serial, freq=freq, hard_reset=False),
        "-w8", f"0x{addr:08X}", f"0x{value:02X}",
    ]
    res = _run_prog(cmd, timeout=timeout)
    if res.returncode != 0:
        raise RuntimeError(f"cube programmer -w8 0x{addr:08X}=0x{value:02X} failed: rc={res.returncode}\n{res.stdout}\n{res.stderr}")


def _write_word(probe_serial: str, addr: int, value: int, *, freq: int = 24000, timeout: int = 10) -> None:
    """Program a single 32-bit word via cube programmer ``-w32``.

    OTP is single-bit-program (1 -> 0); writing 0xFF bytes is a no-op so it is
    safe to mask unmodified bytes with 0xFF.
    """
    if addr & 0x3:
        raise ValueError(f"_write_word requires 4-byte aligned address, got 0x{addr:08X}")
    cmd = [
        *PROG_CMD,
        *_probe_connect_args(probe_serial, freq=freq, hard_reset=False),
        "-w32", f"0x{addr:08X}", f"0x{value:08X}",
    ]
    res = _run_prog(cmd, timeout=timeout)
    if res.returncode != 0:
        raise RuntimeError(f"cube programmer -w32 0x{addr:08X}=0x{value:08X} failed: rc={res.returncode}\n{res.stdout}\n{res.stderr}")


# ── High-level walker ─────────────────────────────────────────────────────
def read_view(probe_serial: str, *, freq: int = 24000) -> OtpView:
    raw = _read_bytes(probe_serial, OTP_BASE, OTP_TOTAL_SIZE, freq=freq)
    locks = _read_bytes(probe_serial, OTP_LOCK_BASE, OTP_BLOCK_COUNT, freq=freq)
    blocks: List[OtpBlock] = []
    active_index: Optional[int] = None
    for i in range(OTP_BLOCK_COUNT):
        block_raw = raw[i * OTP_BLOCK_SIZE:(i + 1) * OTP_BLOCK_SIZE]
        block = _classify(block_raw, locked=(locks[i] == 0x00))
        block.index = i
        blocks.append(block)
        if active_index is None and block.state == STATE_ACTIVE:
            active_index = i
    return OtpView(raw=raw, blocks=blocks, locks=locks, active_index=active_index)


def find_active(view: OtpView) -> Optional[OtpBlock]:
    if view.active_index is None:
        return None
    return view.blocks[view.active_index]


def find_free_index(view: OtpView) -> Optional[int]:
    for b in view.blocks:
        if b.state == STATE_FREE:
            return b.index
    return None


def build_block_bytes(schema_ver: int, payload: bytes) -> bytes:
    """Build the 32-byte block image for the given payload (mirrors awto_otp_write)."""
    if len(payload) > OTP_PAYLOAD_MAX:
        raise ValueError(f"payload too long ({len(payload)} > {OTP_PAYLOAD_MAX})")
    pl = payload + b"\xff" * (OTP_PAYLOAD_MAX - len(payload))
    crc = _crc32(pl)
    header = bytes([
        0xFF,                           # valid_flag
        schema_ver & 0xFF,              # schema_ver
        0xFF, 0xFF,                     # reserved
    ]) + crc.to_bytes(4, "little")
    return header + pl


def write_block(probe_serial: str, index: int, schema_ver: int, payload: bytes, *, freq: int = 24000) -> None:
    """Write one OTP block. Caller must verify the block is FREE first.

    OTP must be programmed in 4-byte aligned words. We split the 32-byte block
    image into 8 little-endian words and write them in reverse order, deferring
    the word containing ``valid_flag`` (word 0) until last so a partial-power
    failure leaves the block CORRUPT (CRC mismatch / valid_flag still 0xFF with
    bad header bytes) rather than ACTIVE-with-bad-payload.
    """
    block = build_block_bytes(schema_ver, payload)
    base = OTP_BASE + index * OTP_BLOCK_SIZE
    log.info("OTP write: block %d at 0x%08X (schema=%d)", index, base, schema_ver)
    words = [int.from_bytes(block[i * 4:(i + 1) * 4], "little") for i in range(OTP_BLOCK_SIZE // 4)]
    # Write words 7..1 first (skip ones that are entirely 0xFF — no bits to clear)
    for w in range(len(words) - 1, 0, -1):
        if words[w] == 0xFFFFFFFF:
            continue
        _write_word(probe_serial, base + w * 4, words[w], freq=freq)
    # Finally word 0 (contains valid_flag in lowest byte)
    if words[0] != 0xFFFFFFFF:
        _write_word(probe_serial, base, words[0], freq=freq)


def invalidate_block(probe_serial: str, index: int, *, freq: int = 24000) -> None:
    base = OTP_BASE + index * OTP_BLOCK_SIZE
    log.warning("OTP invalidate: block %d (writing 0x00 to valid_flag word)", index)
    # Read current word 0 so we don't disturb other bytes; OTP is bit-clear-only
    # so we AND the current word with 0xFFFFFF00 to set valid_flag (low byte) to 0.
    cur = _read_bytes(probe_serial, base, 4, freq=freq)
    cur_word = int.from_bytes(cur, "little")
    new_word = cur_word & 0xFFFFFF00
    _write_word(probe_serial, base, new_word, freq=freq)


def lock_block(probe_serial: str, index: int, *, freq: int = 24000) -> None:
    """PERMANENTLY lock the block. Cannot be undone."""
    addr = OTP_LOCK_BASE + index
    log.warning("OTP lock: PERMANENTLY locking block %d", index)
    _write_byte(probe_serial, addr, 0x00, freq=freq)


# ── Status formatting ─────────────────────────────────────────────────────
def format_status_line(view: OtpView, payload_decoder=None) -> str:
    """Compact one-line summary; ``payload_decoder`` is called on the active
    payload if present and may return a short string (e.g. product info)."""
    free = sum(1 for b in view.blocks if b.state == STATE_FREE)
    invalid = sum(1 for b in view.blocks if b.state == STATE_INVALIDATED)
    corrupt = sum(1 for b in view.blocks if b.state == STATE_CORRUPT)
    active = find_active(view)
    if active is None:
        return f"[otp] no active record  free={free} invalid={invalid} corrupt={corrupt}"
    extra = ""
    if payload_decoder is not None:
        try:
            extra = "  " + payload_decoder(active.schema_ver, active.payload)
        except Exception as exc:
            extra = f"  (decoder error: {exc})"
    locked_str = "locked" if active.locked else "open"
    return (
        f"[otp] block {active.index}/{OTP_BLOCK_COUNT} active  schema={active.schema_ver}"
        f"  free={free} invalid={invalid} corrupt={corrupt}  lock={locked_str}{extra}"
    )


def format_dump(view: OtpView, payload_decoder=None) -> str:
    """Multi-line human-readable dump for ``--read-otp``."""
    lines = [
        f"OTP region: 0x{OTP_BASE:08X}..0x{OTP_BASE + OTP_TOTAL_SIZE - 1:08X}  "
        f"({OTP_BLOCK_COUNT} blocks * {OTP_BLOCK_SIZE} bytes)",
        f"Lock bytes: 0x{OTP_LOCK_BASE:08X}..0x{OTP_LOCK_BASE + OTP_BLOCK_COUNT - 1:08X}",
        "",
        "idx  state         schema  lock    valid  crc(stored / computed)  payload (hex)",
        "---  ------------  ------  ------  -----  -----------------------  -" + "-" * 47,
    ]
    for b in view.blocks:
        state_str = b.state.ljust(12)
        schema_str = "-" if b.schema_ver is None else f"0x{b.schema_ver:02X}"
        lock_str = "LOCKED" if b.locked else "open"
        crc_str = f"{b.crc_stored:08X} / {b.crc_computed:08X}"
        if b.crc_stored == b.crc_computed:
            crc_str += " ok"
        else:
            crc_str += " !! "
        payload_hex = b.payload.hex(" ")
        lines.append(
            f" {b.index:2d}  {state_str}  {schema_str:>6}  {lock_str:<6}  0x{b.valid_flag:02X}   {crc_str}  {payload_hex}"
        )
    lines.append("")
    lines.append(format_status_line(view, payload_decoder=payload_decoder))
    return "\n".join(lines)
