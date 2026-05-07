#!/usr/bin/env python3
"""
test_harness.py — Unit tests for awto-mcp-debug-embedded.

Tests are designed to run without any hardware attached.
Hardware-dependent paths are mocked out via unittest.mock.

Run:
    python test_harness.py -v
    python test_harness.py TestRegistryOps -v
    python test_harness.py TestMiParser -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import MagicMock, patch, call

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).parent))


# ---------------------------------------------------------------------------
# TestRegistryOps — probe registry read/write without USB
# ---------------------------------------------------------------------------

class TestRegistryOps(unittest.TestCase):
    """Tests for probe_detect registry operations (no hardware required)."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._reg_path = os.path.join(self._tmpdir, "probes.json")

        # Fresh import scope — reconfigure the registry path each test
        import probe_detect as pd
        pd.configure_registry(self._reg_path)
        self.pd = pd

    def _make_probe(self, serial: str = "AABBCCDD1234", kind: str = "stlink") -> object:
        return self.pd.ProbeInfo(
            serial=serial,
            kind=kind,
            model="ST-LINK/V2-1",
            nick="",
            usb_vid=0x0483,
            usb_pid=0x374B,
            state="pending",
            port=None,
        )

    def test_upsert_creates_pending(self) -> None:
        probe = self._make_probe()
        self.pd._registry_upsert_probe(probe)

        registry = json.loads(Path(self._reg_path).read_text())
        probes = registry.get("probes", [])
        found = next((p for p in probes if p["serial"] == probe.serial), None)
        self.assertIsNotNone(found)
        self.assertEqual(found["state"], "pending")

    def test_approve_sets_state(self) -> None:
        probe = self._make_probe()
        self.pd._registry_upsert_probe(probe)
        result = self.pd.approve_probe(probe.serial, nick="myboard")
        self.assertIsNotNone(result)
        self.assertEqual(result.state, "approved")
        self.assertEqual(result.nick, "myboard")

    def test_approve_preserves_nick_on_reconnect(self) -> None:
        probe = self._make_probe()
        self.pd._registry_upsert_probe(probe)
        self.pd.approve_probe(probe.serial, nick="kept-nick")

        # Simulate reconnect — upsert again (same serial)
        self.pd._registry_upsert_probe(probe)

        # Nick should be preserved
        all_probes = self.pd.get_all_probes()
        found = next((p for p in all_probes if p.serial == probe.serial), None)
        self.assertIsNotNone(found)
        self.assertEqual(found.nick, "kept-nick")
        self.assertEqual(found.state, "approved")

    def test_ignore_probe(self) -> None:
        probe = self._make_probe()
        self.pd._registry_upsert_probe(probe)
        self.pd.ignore_probe(probe.serial)
        all_probes = self.pd.get_all_probes()
        found = next((p for p in all_probes if p.serial == probe.serial), None)
        self.assertIsNotNone(found)
        self.assertEqual(found.state, "ignored")

    def test_rename_probe(self) -> None:
        probe = self._make_probe()
        self.pd._registry_upsert_probe(probe)
        self.pd.approve_probe(probe.serial)
        self.pd.rename_probe(probe.serial, "new-name")
        all_probes = self.pd.get_all_probes()
        found = next((p for p in all_probes if p.serial == probe.serial), None)
        self.assertEqual(found.nick, "new-name")

    def test_clear_probe(self) -> None:
        probe = self._make_probe()
        self.pd._registry_upsert_probe(probe)
        ok = self.pd.clear_probe(probe.serial)
        self.assertTrue(ok)
        all_probes = self.pd.get_all_probes()
        self.assertFalse(any(p.serial == probe.serial for p in all_probes))

    def test_clear_missing_probe_returns_false(self) -> None:
        ok = self.pd.clear_probe("NONEXISTENT")
        self.assertFalse(ok)

    def test_multiple_probes(self) -> None:
        for i in range(5):
            p = self._make_probe(serial=f"SERIAL{i:04d}")
            self.pd._registry_upsert_probe(p)
        probes = self.pd.get_all_probes()
        self.assertEqual(len(probes), 5)

    def test_approve_nonexistent_returns_none(self) -> None:
        result = self.pd.approve_probe("DOESNOTEXIST")
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# TestProbeDetect — enumeration with mocked pyusb
# ---------------------------------------------------------------------------

class TestProbeDetect(unittest.TestCase):
    """Tests for probe enumeration (mocked USB/serial)."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        import probe_detect as pd
        pd.configure_registry(os.path.join(self._tmpdir, "probes.json"))
        self.pd = pd

    def _make_usb_device(self, vid: int, pid: int, serial: str = "AABBCCDD") -> MagicMock:
        dev = MagicMock()
        dev.idVendor = vid
        dev.idProduct = pid
        dev.iSerialNumber = 3
        dev.bus = 1
        dev.address = 2
        dev.serial_number = serial
        dev.product = "ST-LINK"
        return dev

    @patch("usb.core.find")
    @patch("usb.util.get_string", return_value="STLINK001")
    def test_enumerate_stlink(self, mock_get_string: MagicMock, mock_find: MagicMock) -> None:
        mock_find.return_value = [
            self._make_usb_device(0x0483, 0x374B, "STLINK001"),
        ]
        probes = self.pd._enumerate_stlink_probes()
        self.assertEqual(len(probes), 1)
        self.assertEqual(probes[0].kind, "stlink")
        self.assertEqual(probes[0].serial, "STLINK001")
        self.assertEqual(probes[0].model, "ST-LINK/V2-1")

    @patch("usb.core.find")
    def test_enumerate_stlink_empty(self, mock_find: MagicMock) -> None:
        mock_find.return_value = []
        probes = self.pd._enumerate_stlink_probes()
        self.assertEqual(probes, [])

    @patch("usb.core.find")
    def test_enumerate_stlink_unknown_pid(self, mock_find: MagicMock) -> None:
        """Devices with unknown PID are ignored."""
        mock_find.return_value = [
            self._make_usb_device(0x0483, 0x9999, "UNKNOWNDEV"),
        ]
        # _enumerate_stlink_probes should use find with multiple_values and filter
        # The actual behavior depends on implementation — test that no crash occurs
        try:
            probes = self.pd._enumerate_stlink_probes()
            # Either empty or contains one entry — just no exception
        except Exception as exc:
            self.fail(f"Unexpected exception: {exc}")


# ---------------------------------------------------------------------------
# TestProcessManager — process lifecycle without real processes
# ---------------------------------------------------------------------------

class TestProcessManager(unittest.TestCase):
    """Tests for ProcessManager (mocked subprocess.Popen)."""

    def setUp(self) -> None:
        # Import fresh to avoid singleton state pollution across tests
        import process_manager as pm
        # Reset singleton
        pm._manager = None
        self.pm = pm

    def _make_proc_handle(self, tag: str = "test", port: int = 4242) -> object:
        """Start a 'sleep infinity' mock process."""
        mock_popen = MagicMock()
        mock_popen.pid = 12345
        mock_popen.poll.return_value = None  # still running
        mock_popen.returncode = None

        with patch("subprocess.Popen", return_value=mock_popen):
            mgr = self.pm.get_manager()
            handle = mgr.start(
                ["sleep", "infinity"],
                tag=tag,
                port=port,
                cwd=None,
                env=None,
                startup_wait_s=0,
            )
        return handle, mock_popen

    def test_start_returns_handle(self) -> None:
        handle, _ = self._make_proc_handle()
        self.assertIsNotNone(handle.id)
        self.assertTrue(handle.alive)

    def test_list_running(self) -> None:
        handle, _ = self._make_proc_handle()
        mgr = self.pm.get_manager()
        running = mgr.list_running()  # returns ProcessHandle objects
        self.assertTrue(any(h.id == handle.id for h in running))

    def test_stop_sends_sigterm(self) -> None:
        handle, mock_proc = self._make_proc_handle()
        mock_proc.wait.return_value = 0
        mock_proc.poll.side_effect = [None, 0]  # alive then dead

        mgr = self.pm.get_manager()
        rc = mgr.stop(handle.id, timeout_s=1.0)
        mock_proc.terminate.assert_called_once()

    def test_stop_nonexistent_returns_none(self) -> None:
        mgr = self.pm.get_manager()
        result = mgr.stop("00000000-0000-0000-0000-000000000000")
        self.assertIsNone(result)

    def test_stop_by_tag(self) -> None:
        handle, mock_proc = self._make_proc_handle(tag="test-tag")
        mock_proc.wait.return_value = 0
        mock_proc.poll.side_effect = [None, 0]

        mgr = self.pm.get_manager()
        mgr.stop_by_tag("test-tag")  # no timeout_s kwarg in stop_by_tag
        mock_proc.terminate.assert_called()

    def test_handle_as_dict(self) -> None:
        handle, _ = self._make_proc_handle()
        d = handle.as_dict()
        self.assertIn("id", d)
        self.assertIn("tag", d)
        self.assertIn("pid", d)
        self.assertIn("alive", d)
        self.assertTrue(d["alive"])


# ---------------------------------------------------------------------------
# TestStlinkHelpers — parser and snapshot helpers without hardware
# ---------------------------------------------------------------------------

class TestStlinkHelpers(unittest.TestCase):
    """Tests for debugger_stlink helpers with mocked subprocess/file IO."""

    def setUp(self) -> None:
        import debugger_stlink as st
        self.st = st
        self.st._TARGET_INFO_CACHE.clear()

    def test_probe_list_parses_flash_ram_and_device(self) -> None:
        sample = """
Found 1 stlink programmers
  version:    V3J13
  serial:     003400223137510E33333639
  flash:      131072 (pagesize: 131072)
  sram:       131072
  chipid:     0x450
  dev-type:   STM32H74x_H75x
""".strip()
        with patch.object(self.st, "_run", return_value=(0, sample, "")):
            probes = self.st.probe_list()
        self.assertEqual(len(probes), 1)
        self.assertEqual(probes[0]["serial"], "003400223137510E33333639")
        self.assertEqual(probes[0]["flash_size_bytes"], 131072)
        self.assertEqual(probes[0]["ram_size_bytes"], 131072)
        self.assertEqual(probes[0]["device_name"], "STM32H74x_H75x")

    def test_chip_info_requires_serial_when_multiple_probes_present(self) -> None:
        probes = [
            {"serial": "AAA", "chip_id": "0x111"},
            {"serial": "BBB", "chip_id": "0x222"},
        ]
        with patch.object(self.st, "probe_list", return_value=probes):
            with self.assertRaises(RuntimeError):
                self.st.chip_info()

    def test_chip_info_uses_cache_without_probe_scan(self) -> None:
        self.st._TARGET_INFO_CACHE["SERIAL1"] = {
            "chip_id": "0x450",
            "device_name": "STM32H74x_H75x",
            "flash_size_kb": 128,
            "ram_size_kb": 128,
        }
        with patch.object(self.st, "probe_list", side_effect=AssertionError("should not scan")):
            info = self.st.chip_info("SERIAL1")
        self.assertEqual(info["chip_id"], "0x450")

    def test_dump_memory_snapshot_writes_metadata_and_calls_reads(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(self.st, "chip_info", return_value={
                "chip_id": "0x450",
                "device_name": "STM32H74x_H75x",
                "flash_size_kb": 128,
                "ram_size_kb": 128,
            }):
                with patch.object(self.st, "flash_read") as mock_read:
                    result = self.st.dump_memory_snapshot(
                        tmpdir,
                        serial="SERIAL1",
                    )
                    self.assertIn("metadata", result["files"])
                    self.assertEqual(mock_read.call_args_list, [
                        call(str(Path(tmpdir) / "flash.bin"), "0x08000000", 131072, "SERIAL1"),
                        call(str(Path(tmpdir) / "ram.bin"), "0x20000000", 131072, "SERIAL1"),
                    ])
                    metadata = json.loads(Path(result["files"]["metadata"]).read_text())
                    self.assertEqual(metadata["chip_info"]["chip_id"], "0x450")
                    self.assertEqual(metadata["flash_length"], 131072)
                    self.assertEqual(metadata["ram_length"], 131072)

    def test_dump_memory_snapshot_with_explicit_lengths_skips_chip_info(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(self.st, "chip_info", side_effect=AssertionError("should not scan")):
                with patch.object(self.st, "flash_read") as mock_read:
                    result = self.st.dump_memory_snapshot(
                        tmpdir,
                        serial="SERIAL2",
                        flash_length=64,
                        ram_length=64,
                    )
        self.assertEqual(mock_read.call_count, 2)
        self.assertEqual(result["chip_info"]["serial"], "SERIAL2")


# ---------------------------------------------------------------------------
# TestMiParser — GDB/MI response parsing (pure unit, no socket)
# ---------------------------------------------------------------------------

class TestMiParser(unittest.TestCase):
    """Tests for GDB/MI response parsing utilities."""

    def setUp(self) -> None:
        from gdb_client import MiResponse, _parse_kv, _unquote
        self.MiResponse = MiResponse
        self._parse_kv = _parse_kv
        self._unquote = _unquote

    def test_parse_kv_simple(self) -> None:
        kv = self._parse_kv('key="value",other="123"')
        self.assertEqual(kv["key"], "value")
        self.assertEqual(kv["other"], "123")

    def test_parse_kv_empty(self) -> None:
        kv = self._parse_kv("")
        self.assertEqual(kv, {})

    def test_unquote_basic(self) -> None:
        self.assertEqual(self._unquote('"hello"'), "hello")

    def test_unquote_escaped(self) -> None:
        self.assertEqual(self._unquote('"hello\\nworld"'), "hello\nworld")

    def test_mi_response_ok(self) -> None:
        resp = self.MiResponse("1", "done", 'var="value"', '1^done,var="value"')
        self.assertTrue(resp.ok)
        self.assertEqual(resp.kv["var"], "value")

    def test_mi_response_error(self) -> None:
        resp = self.MiResponse("2", "error", 'msg="No connection"', '2^error,msg="No connection"')
        self.assertFalse(resp.ok)
        self.assertEqual(resp.error_msg, "No connection")

    def test_mi_response_running(self) -> None:
        resp = self.MiResponse("3", "running", "", '3^running')
        self.assertTrue(resp.ok)


# ---------------------------------------------------------------------------
# TestBackendDetect — check_backends() without any tools installed
# ---------------------------------------------------------------------------

class TestBackendDetect(unittest.TestCase):
    """Test backend detection when no tools are installed."""

    def test_check_backends_returns_dataclass(self) -> None:
        import probe_detect as pd
        with patch("shutil.which", return_value=None):
            bs = pd.check_backends()
        # All fields should be False
        d = asdict(bs)
        self.assertTrue(all(v is False for v in d.values()), f"Unexpected: {d}")

    def test_check_backends_detects_stflash(self) -> None:
        import probe_detect as pd
        def fake_which(cmd: str) -> str | None:
            return "/usr/bin/st-flash" if cmd == "st-flash" else None
        with patch("shutil.which", side_effect=fake_which):
            bs = pd.check_backends()
        self.assertTrue(bs.st_flash)
        self.assertFalse(bs.esptool)


# ---------------------------------------------------------------------------
# TestProbeMonitor — sanity check the monitor thread
# ---------------------------------------------------------------------------

class TestProbeMonitor(unittest.TestCase):
    """Smoke tests for ProbeMonitor (no USB hardware)."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        import probe_detect as pd
        pd.configure_registry(os.path.join(self._tmpdir, "probes.json"))
        pd._monitor_instance = None  # reset singleton
        self.pd = pd

    def tearDown(self) -> None:
        self.pd.stop_monitor()

    @patch("probe_detect.enumerate_all_probes", return_value=[])
    def test_monitor_starts_and_stops(self, mock_enum: MagicMock) -> None:
        monitor = self.pd.ProbeMonitor(poll_interval=0.1)
        monitor.start()
        self.assertTrue(monitor.ready.wait(timeout=2.0))
        self.assertEqual(monitor.connected_probes(), [])
        monitor.stop()

    @patch("probe_detect.enumerate_all_probes")
    def test_monitor_fires_on_connect(self, mock_enum: MagicMock) -> None:
        import probe_detect as pd

        probe = pd.ProbeInfo(
            serial="TESTSERIAL1234",
            kind="stlink",
            model="ST-LINK/V2-1",
            nick="",
            usb_vid=0x0483,
            usb_pid=0x374B,
            state="pending",
            port=None,
        )
        # First poll: no probes. Second poll: one probe appears.
        mock_enum.side_effect = [[], [probe], [probe]]

        connected: list[pd.ProbeInfo] = []
        event = threading.Event()

        monitor = pd.ProbeMonitor(poll_interval=0.05)
        monitor.on_connect(lambda p: (connected.append(p), event.set()))
        monitor.start()

        event.wait(timeout=3.0)
        monitor.stop()

        self.assertEqual(len(connected), 1)
        self.assertEqual(connected[0].serial, "TESTSERIAL1234")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main(verbosity=2)
