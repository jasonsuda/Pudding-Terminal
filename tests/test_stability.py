# SPDX-License-Identifier: GPL-3.0-only
"""Exercise long-run stability guards, diagnostics, log rotation, and opt-in serial reconnect."""

import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import serial


SOURCE = Path(__file__).resolve().parents[1] / "Pudding Terminal.py"
spec = importlib.util.spec_from_file_location("pudding_terminal_stability", SOURCE)
terminal = importlib.util.module_from_spec(spec)
spec.loader.exec_module(terminal)


class StabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.enterClassContext(patch.dict(os.environ, {"QT_QPA_PLATFORM": "offscreen"}))
        cls.app = terminal.QApplication.instance() or terminal.QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.state = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="pudding-stab-")))
        self.enterContext(patch.object(terminal, "APP_DIR", self.state))
        self.enterContext(patch.object(serial.tools.list_ports, "comports", return_value=[]))

    def test_serial_reconnect_defaults_to_disabled_and_respects_override(self):
        self.assertFalse(terminal.DEFAULTS["serial/reconnect"])
        prefs = terminal.Preferences()
        self.assertFalse(prefs.get("serial/reconnect"))
        values = {key: prefs.get(key) for key in terminal.DEFAULTS}
        dialog = terminal.SettingsDialog(values, page="serial", connected=False)
        try:
            self.assertFalse(dialog.controls["serial/reconnect"].isChecked())
            self.assertFalse(dialog.values()["serial/reconnect"])
            dialog.controls["serial/reconnect"].setChecked(True)
            self.assertTrue(dialog.values()["serial/reconnect"])
        finally:
            dialog.close()
            dialog.deleteLater()

    def test_serial_opt_in_auto_reconnect_preserves_session_log(self):
        logger = terminal.SessionLogger()
        logger.start("device_logs", interval=0, timestamp=False)
        self.addCleanup(logger.stop)

        attempts = 0
        delivered_after_reconnect = False

        class FakeSerial:
            def __init__(self, *args, **kwargs):
                nonlocal attempts
                attempts += 1
                self.attempt = attempts
                self.reads = 0

            @property
            def in_waiting(self):
                return 8

            def read(self, _limit):
                nonlocal delivered_after_reconnect
                self.reads += 1
                if self.attempt == 1:
                    if self.reads == 1:
                        return b"before-drop\r\n"
                    raise serial.SerialException("synthetic link drop")
                if not delivered_after_reconnect:
                    delivered_after_reconnect = True
                    return b"after-reconnect\r\n"
                return b""

            def write(self, data):
                return len(data)

            def close(self):
                pass

        cfg = {
            "type": "Serial",
            "serial/port": "COM1",
            "serial/baud": 9600,
            "serial/data": "8",
            "serial/parity": "None",
            "serial/stop": "1",
            "serial/flow": "None",
            "serial/reconnect": True,
        }
        worker = terminal.ConnectionWorker(cfg, (80, 24), logger=logger)
        connected = []
        reconnecting = []
        problems = []
        worker.connected.connect(lambda: connected.append(time.monotonic()))
        worker.serial_reconnecting.connect(lambda: reconnecting.append(time.monotonic()))
        worker.problem.connect(problems.append)

        orig_wait = worker.stop_event.wait

        def fast_wait(timeout=None):
            return orig_wait(0.01 if timeout and timeout > 0.1 else timeout)

        with patch.object(serial, "Serial", side_effect=FakeSerial), \
             patch.object(worker.stop_event, "wait", side_effect=fast_wait):
            worker.start()
            deadline = time.monotonic() + 5.0
            while (len(connected) < 2 or not delivered_after_reconnect) and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.01)
            worker.stop()
            worker.wait(5000)
            self.app.processEvents()

        logger.end_input()
        logger.stop()
        log_files = list((self.state / "device_logs").glob("Session_*.txt"))
        self.assertEqual(len(log_files), 1)
        content = log_files[0].read_text(encoding="utf-8")
        self.assertIn("before-drop", content)
        self.assertIn("after-reconnect", content)
        self.assertEqual(len(reconnecting), 1)
        self.assertEqual(len(connected), 2)
        self.assertEqual(problems, [])

    def test_serial_default_does_not_reconnect_after_read_error(self):
        class DroppingSerial:
            def __init__(self, *args, **kwargs):
                self.reads = 0

            @property
            def in_waiting(self):
                return 4

            def read(self, _limit):
                self.reads += 1
                if self.reads == 1:
                    return b"once\r\n"
                raise serial.SerialException("synthetic read error")

            def close(self):
                pass

        cfg = {
            "type": "Serial",
            "serial/port": "COM1",
            "serial/baud": 9600,
            "serial/data": "8",
            "serial/parity": "None",
            "serial/stop": "1",
            "serial/flow": "None",
        }
        worker = terminal.ConnectionWorker(cfg, (80, 24))
        reconnecting = []
        problems = []
        worker.serial_reconnecting.connect(lambda: reconnecting.append(True))
        worker.problem.connect(problems.append)

        with patch.object(serial, "Serial", side_effect=DroppingSerial) as factory:
            worker.start()
            self.assertTrue(worker.wait(5000))
            self.app.processEvents()

        self.assertEqual(factory.call_count, 1)
        self.assertEqual(reconnecting, [])
        self.assertEqual(len(problems), 1)

    def test_session_logger_idle_rotation_and_write_fault_isolation(self):
        logger = terminal.SessionLogger()
        logger.start("device_logs", interval=0.05, timestamp=False)
        self.addCleanup(logger.stop)
        logger.receive(b"first-file\r\n")
        time.sleep(0.08)
        logger.rotate_if_due()
        logger.receive(b"second-file\r\n")
        files = sorted((self.state / "device_logs").glob("Session_*.txt"))
        self.assertGreaterEqual(len(files), 2)
        self.assertIn("first-file", files[0].read_text(encoding="utf-8"))
        self.assertIn("second-file", files[-1].read_text(encoding="utf-8"))

        with patch.object(logger, "_write_text", side_effect=OSError(28, "synthetic disk full")):
            logger.receive(b"dropped-line\r\n")
        self.assertIsNotNone(logger.fault)
        self.assertIsNone(logger.handle)
        logger.receive(b"after-fault\r\n")
        self.assertGreater(logger.bytes_without_log, 0)

    def test_diagnostics_and_guard_ui_survive_timer_exception_and_blocked_path(self):
        diag = terminal.Diagnostics()
        self.enterContext(patch.object(terminal, "DIAGNOSTICS", diag))
        self.enterContext(patch.object(terminal.sys, "excepthook", diag.exception))
        self.enterContext(patch.object(terminal.threading, "excepthook", diag.thread_exception))
        diag.open_files()
        self.addCleanup(diag.close)

        window = terminal.MainWindow()
        self.addCleanup(lambda: (window.close(), window.deleteLater(), self.app.processEvents()))

        ran_after_error = False
        with patch.object(window.terminal.model, "resize", side_effect=TypeError("synthetic resize error")):
            window.terminal._resize_grid()
            t = terminal.single_shot_timer(window, lambda: None)

            def mark():
                nonlocal ran_after_error
                ran_after_error = True

            t.timeout.connect(mark)
            t.start(0)
            deadline = time.monotonic() + 2.0
            while not ran_after_error and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.01)

        self.assertTrue(ran_after_error)
        diag.close()
        rt_files = list((self.state / "diagnostics").glob("Runtime_*.jsonl"))
        self.assertEqual(len(rt_files), 1)
        events = [json.loads(line) for line in rt_files[0].read_text(encoding="utf-8").splitlines() if line.strip()]
        exc_events = [e for e in events if e.get("event") == "uncaught_exception"]
        self.assertTrue(any(e.get("callback") == "TerminalWidget._resize_grid" and e.get("type") == "TypeError" for e in exc_events))

    def test_diagnostics_blocked_path_falls_back_to_memory_status(self):
        (self.state / "diagnostics").write_text("blocker", encoding="utf-8")
        diag = terminal.Diagnostics()
        self.enterContext(patch.object(terminal, "DIAGNOSTICS", diag))
        diag.open_files()
        self.addCleanup(diag.close)

        self.assertIsNotNone(diag.error)
        try:
            raise ValueError("synthetic memory-only failure")
        except ValueError:
            terminal.record_exception("UnitTest.callback")

        window = terminal.MainWindow()
        self.addCleanup(lambda: (window.close(), window.deleteLater(), self.app.processEvents()))
        window._refresh_diagnostic_status()
        self.assertFalse(window.diagnostic_status.isHidden())
        self.assertIn("UnitTest.callback", window.diagnostic_status.toolTip())
        self.assertIn("ValueError", window.diagnostic_status.toolTip())
