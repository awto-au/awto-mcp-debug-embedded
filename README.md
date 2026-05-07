# awto-mcp-debug-embedded

MCP server exposing embedded debugger tools to AI agents (Copilot, Claude, etc.).

Registry files:
- `probes.json` tracks known debug probes/adapters and approval state.
- `cpus.json` tracks discovered target CPUs and approval state.

## Why this server exists

MCP is useful, but embedded teams need more than tool access. This server is
designed around three practical goals:

- Repeatability first: workflows are executed server-side with consistent
    sequencing and safety gates, so runs can be reproduced and audited.
- Token minimization by default: compact responses reduce model context cost,
    while full diagnostics are available only when explicitly requested.
- Model separation from low-level debug control: the model asks for intent
    (snapshot, flash cycle, report), and the server owns the exact command flow.

The result is lower iteration cost when failures happen, especially for
multi-target bring-up and recovery scenarios.

## Known test target

- STM32F4-Discovery can be used as a hardware validation board for STM32
    workflows in this server.

Supports:
- **ST-Link open-source tools** (`st-flash`, `st-info`, `st-util`, `st-trace`)
- **STM32CubeProgrammer** (`cube` CLI / `STM32_Programmer_CLI`) for ST targets
- **Espressif / ESP-IDF** (`esptool.py`, `idf.py`) for ESP32/ESP32-S3/etc.
- **GDB/MI client** — connect to any running GDB server (st-util, cube stlink-gdbserver, OpenOCD)
  for memory reads, register inspection, breakpoints, and execution control.

## Prerequisites

| Tool | Package / Source |
|---|---|
| `st-flash`, `st-info`, `st-util`, `st-trace` | `stlink` (OS package: `dnf install stlink` or build from source) |
| `STM32_Programmer_CLI` or `cube` | [STM32CubeProgrammer](https://www.st.com/en/development-tools/stm32cubeprog.html) |
| `esptool.py` | `pip install esptool` |
| `idf.py` | [ESP-IDF install](https://docs.espressif.com/projects/esp-idf/en/latest/esp32/get-started/) |
| `openocd` | `dnf install openocd` or ESP-IDF bundled `openocd-esp32` |

## Quick start

```bash
bash scripts/dev-setup.sh          # create .venv and install
source .venv/bin/activate
python test_harness.py -v          # run mock tests (no hardware required)
python mcp_server.py               # start MCP server
```

## VS Code / Copilot

Add to your workspace `.vscode/mcp.json` (already present):

```json
{
    "servers": {
        "awto-debug-embedded": {
            "type": "stdio",
            "command": "${workspaceFolder}/.venv/bin/python",
            "args": ["${workspaceFolder}/mcp_server.py"]
        }
    }
}
```

## Tools

### Probe discovery
- `probe_list()` — list attached ST-Link probes + report available CLI backends
- `probe_info(serial?)` — MCU device-id, flash size, UID

Approval policy:
- Newly discovered probes and CPUs are registered as `pending`.
- MCP operations are blocked for pending/ignored devices.
- User approval is required before use (`probe_approve`, `cpu_approve`).

### CPU registry
`cpu_list`, `cpu_approve`, `cpu_ignore`, `cpu_forget`

Identity rules:
- ESP CPUs are keyed by MAC address.
- STM32 CPUs are keyed by CPU type (`device_name` / family string).

### Managed Workflows (preferred for AI agents)
`debug_session_start`, `debug_session_set_mode`, `debug_session_status`,
`debug_session_memory_snapshot`, `debug_session_safe_flash_cycle`,
`debug_parallel_flash_program`, `debug_user_action_request`,
`debug_session_report`

These tools move sequencing and safety policy into the MCP server so clients can
request intent-level actions instead of manually orchestrating low-level commands.

Token/use guidance:
- Default to `response_mode='compact'` and `detail_level='compact'` to keep
    model context small.
- Enable `deep_debug=true` or `detail_level='full'` only while triaging issues.
- Use `debug_parallel_flash_program` for multi-target flashing to reduce
    iterative agent retries.
- Use `debug_user_action_request` when manual/local execution by a human is
    cheaper or required.

### ST-Link open-source (st-flash / st-info / st-util)
`stlink_flash`, `stlink_erase`, `stlink_read`, `stlink_verify`, `stlink_reset`,
`stlink_memory_snapshot`, `stlink_gdb_start`, `stlink_gdb_stop`,
`stlink_trace_start`, `stlink_trace_stop`

### STM32CubeProgrammer
`cube_flash`, `cube_erase`, `cube_info`, `cube_read_uid`, `cube_otp_read`,
`cube_otp_dump`, `cube_connection_properties`, `cube_recover`,
`cube_gdb_start`, `cube_gdb_stop`

### GDB client (GDB/MI over TCP)
`gdb_connect`, `gdb_disconnect`, `gdb_read_memory`, `gdb_read_registers`,
`gdb_set_breakpoint`, `gdb_delete_breakpoint`, `gdb_continue`, `gdb_halt`,
`gdb_step`, `gdb_status`

### Espressif / esptool
`esp_chip_info`, `esp_flash_write`, `esp_flash_erase`, `esp_flash_read`, `esp_reset`

### Espressif / idf.py
`idf_build`, `idf_flash`, `idf_monitor_start`, `idf_monitor_stop`,
`idf_menuconfig`, `idf_size`, `idf_openocd_start`, `idf_openocd_stop`

## Architecture

```
mcp_server.py          FastMCP stdio server — all tool definitions
probe_detect.py        pyusb ST-Link enumeration; serial port scan for ESP32;
                       backend availability (shutil.which)
process_manager.py     Long-running process lifecycle (GDB servers, OpenOCD,
                       idf.py monitor) — ProcessManager singleton
debugger_stlink.py     st-flash / st-info / st-util subprocess wrappers
debugger_cube.py       STM32CubeProgrammer / cube CLI subprocess wrappers
debugger_esp.py        esptool.py + idf.py subprocess wrappers
gdb_client.py          GDB/MI TCP client — memory, registers, breakpoints
ttu_cli.py             CLI entry point (argparse subcommands)
test_harness.py        unittest — mock subprocess tests, no hardware required
```

## Coding style

See [CODING_STYLE.md](CODING_STYLE.md) (follows awto-au conventions).
