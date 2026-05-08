"""Probe every approved+connected ST-Link, query enriched chip info."""
import json
import struct
import tempfile
from pathlib import Path

import probe_detect as pd
import debugger_cube as cube
import debugger_stlink as stlink
from awto_debug import stm32_db


def chip_info(serial: str) -> dict:
    out: dict = {"serial": serial}
    try:
        probe = cube.probe_info(serial)
    except Exception as exc:
        return {"serial": serial, "error": f"probe_info failed: {exc}"}
    out.update({
        "device_id":   probe.get("device_id"),
        "device_name": probe.get("device_name"),
        "board_name":  probe.get("board_name"),
    })

    devid_str = str(probe.get("device_id") or "")
    db = None
    if devid_str.lower().startswith("0x"):
        try:
            db = stm32_db.lookup_device(int(devid_str, 16))
        except ValueError:
            pass
    if db:
        out["series"] = db["series"]
        out["cpu"] = db["cpu"]
        out["fsize_default_kb"] = (db["fsize_default_bytes"] // 1024) if db["fsize_default_bytes"] else None
        out["fsize_address"] = f"0x{db['fsize_address']:08X}" if db["fsize_address"] else None
        out["uid_address"] = f"0x{db['uid_address']:08X}" if db["uid_address"] else None
        out["flash_variants"] = [v["name"] + f" ({v['size_kb']} KB)" for v in db["flash_variants"]]

    fsize_addr = db["fsize_address"] if db else None
    uid_addr = db["uid_address"] if db else None

    if fsize_addr is not None:
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as tmp:
            tp = tmp.name
        try:
            aligned = fsize_addr & ~0x3
            offset = fsize_addr - aligned
            stlink.flash_read(tp, f"0x{aligned:08X}", 4, serial)
            data = Path(tp).read_bytes()
            if len(data) >= offset + 2:
                out["flash_size_kb"] = struct.unpack("<H", data[offset:offset + 2])[0]
        except Exception as exc:
            out["flash_size_error"] = str(exc)[:120]
        finally:
            Path(tp).unlink(missing_ok=True)

    if uid_addr is not None:
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as tmp:
            tp = tmp.name
        try:
            stlink.flash_read(tp, f"0x{uid_addr:08X}", 12, serial)
            data = Path(tp).read_bytes()
            if len(data) >= 12:
                w0, w1, w2 = struct.unpack("<III", data[:12])
                out["uid_hex"] = f"{w2:08X}{w1:08X}{w0:08X}"
        except Exception as exc:
            out["uid_error"] = str(exc)[:120]
        finally:
            Path(tp).unlink(missing_ok=True)
    return out


def main() -> None:
    probes = [p for p in pd.get_all_probes()
              if p.kind == "stlink" and p.state == "approved"]
    results = []
    for p in probes:
        nick = f" [{p.nick}]" if p.nick else ""
        print(f"--- {p.model} {p.serial[-12:]}{nick} ---")
        info = chip_info(p.serial)
        results.append(info)
        print(json.dumps(info, indent=2))
        print()
    Path("/tmp/awto-chip-scan.json").write_text(json.dumps(results, indent=2) + "\n")


if __name__ == "__main__":
    main()
