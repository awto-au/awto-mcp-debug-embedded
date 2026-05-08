import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

_REGIONS: Dict[str, Tuple[int, int]] = {
    "RAM": (0x20000000, 192 * 1024),
    "CCM": (0x10000000, 64 * 1024),
    "FLASH": (0x0800C000, (16 + 64 + 384) * 1024),
    "EXT": (0x08080000, 512 * 1024),
}

_log_dir: Optional[Path] = None


def configure(regions: Optional[Dict[str, Tuple[int, int]]] = None, log_dir: Optional[Path] = None) -> None:
    global _REGIONS, _log_dir
    if regions is not None:
        _REGIONS = dict(regions)
    if log_dir is not None:
        _log_dir = Path(log_dir)


_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_DIM = "\033[2m"
_RESET = "\033[0m"

_LINE_RE = re.compile(
    r"^(\d{4}-\d\d-\d\d \d\d:\d\d)  ([A-Z?]+)\s+"
    r"RAM\s+(\d+)B\s+(\S+)%\s+"
    r"CCM\s+(\d+)B\s+(\S+)%\s+"
    r"FLASH\s+(\d+)B\s+(\S+)%\s+"
    r"EXT\s+(\d+)B\s+(\S+)%"
)


def _sizes_log(elf: str, mode: str) -> Path:
    base = _log_dir if _log_dir is not None else Path(elf).parent
    wire = _elf_wire_suffix(elf)
    log = base / f"sizes-{mode}-{wire}.data"

    # Migrate legacy htc-only histories the first time the new naming scheme is used.
    if wire == "htc":
        legacy = base / f"sizes-{mode}.data"
        if not log.exists() and legacy.exists():
            legacy.rename(log)

    return log


def _elf_wire_suffix(elf: str) -> str:
    stem = Path(elf).stem.lower()
    m = re.match(r"awto-(?:con|pdm)-([a-z0-9_-]+)$", stem)
    if m:
        return m.group(1)
    m = re.search(r"-(htc|scs|dingo)$", stem)
    if m:
        return m.group(1)
    return "unknown"


def _git_cmd(cwd: Path, *args: str) -> Optional[str]:
    try:
        out = subprocess.check_output(["git", *args], cwd=cwd, stderr=subprocess.DEVNULL, text=True, timeout=5)
    except Exception:
        return None
    return out.strip()


def _git_metadata(elf: str) -> Tuple[str, str]:
    start_dir = (_log_dir if _log_dir is not None else Path(elf).parent).resolve()
    repo_root = _git_cmd(start_dir, "rev-parse", "--show-toplevel")
    if not repo_root:
        return "UNKNOWN", "unknown"

    repo = Path(repo_root)
    commit_hash = _git_cmd(repo, "rev-parse", "--short=12", "HEAD") or "UNKNOWN"
    status = _git_cmd(repo, "status", "--porcelain")
    if status is None:
        dirty = "unknown"
    else:
        dirty = "true" if status else "false"
    return commit_hash, dirty


def _elf_region_usage(elf: str) -> Optional[Dict[str, Tuple[int, int]]]:
    size_bin = shutil.which("arm-none-eabi-size")
    if not size_bin:
        return None
    try:
        out = subprocess.check_output([size_bin, "--format=sysv", elf], stderr=subprocess.DEVNULL, text=True, timeout=5)
    except Exception:
        return None

    usage = {name: 0 for name in _REGIONS}
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            size = int(parts[1])
            addr = int(parts[2])
        except ValueError:
            continue
        for name, (base, length) in _REGIONS.items():
            if base <= addr < base + length:
                usage[name] += size
                break

    return {name: (usage[name], total) for name, (_, total) in _REGIONS.items()}


def _read_log_lines(log: Path, mode: str) -> list:
    if not log.exists():
        return []
    return [ln for ln in log.read_text().splitlines() if (m := _LINE_RE.match(ln)) and m.group(2) == mode]


def _last_pcts(log: Path, mode: str) -> Optional[Dict[str, float]]:
    lines = _read_log_lines(log, mode)
    if not lines:
        return None
    m = _LINE_RE.match(lines[-1])
    return {"RAM": float(m.group(4)), "CCM": float(m.group(6)), "FLASH": float(m.group(8)), "EXT": float(m.group(10))}


def _last_bytes(log: Path, mode: str) -> Optional[Dict[str, int]]:
    lines = _read_log_lines(log, mode)
    if not lines:
        return None
    m = _LINE_RE.match(lines[-1])
    return {"RAM": int(m.group(3)), "CCM": int(m.group(5)), "FLASH": int(m.group(7)), "EXT": int(m.group(9))}


def log_build_size(elf: str, mode: str = "GEN") -> None:
    regions = _elf_region_usage(elf)
    if regions is None:
        return

    log = _sizes_log(elf, mode)
    prev_pct = _last_pcts(log, mode)
    prev_bytes = _last_bytes(log, mode)
    ts = time.strftime("%Y-%m-%d %H:%M")
    git_hash, dirty = _git_metadata(elf)

    plain_parts = []
    color_parts = []
    for name in ("RAM", "CCM", "FLASH", "EXT"):
        used, total = regions[name]
        pct = used * 100.0 / total
        plain = f"{name} {used:>7}B {pct:5.1f}%"
        plain_parts.append(plain)

        if prev_pct is not None:
            delta = pct - prev_pct[name]
            flag = f" ({delta:+.1f}%)" if abs(delta) >= 0.05 else ""
            if delta < -0.04:
                color = _GREEN
            elif delta <= 0.04:
                color = ""
            elif delta < 1.0:
                color = _YELLOW
            else:
                color = _RED
            reset = _RESET if color else ""
            color_parts.append(f"{color}{name} {used:>7}B {pct:5.1f}%{flag}{reset}")
        else:
            color_parts.append(plain)

    no_change = prev_bytes is not None and all(regions[n][0] == prev_bytes[n] for n in ("RAM", "CCM", "FLASH", "EXT"))
    suffix = " *" if no_change else ""
    prefix = f"{ts}  {mode:<3}   "
    metadata = f"   GIT_HASH {git_hash}   DIRTY {dirty}"
    plain_line = prefix + "   ".join(plain_parts) + metadata + suffix
    color_line = prefix + "   ".join(color_parts) + metadata + suffix

    with log.open("a") as f:
        f.write(plain_line + "\n")

    all_lines = _read_log_lines(log, mode)
    prior = all_lines[-(6):-1]
    if prior:
        print(_DIM + "\n".join(f"[size] {ln}" for ln in prior) + _RESET)
    print(f"[size] {color_line}")
