# SPDX-License-Identifier: GPL-3.0-only
"""Exercise production connection paths with synthetic loopback peers."""

import asyncio
from contextlib import asynccontextmanager
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import asyncssh
import serial
import telnetlib3


SOURCE = Path(__file__).resolve().parents[1] / "Pudding Terminal.py"
spec = importlib.util.spec_from_file_location("pudding_terminal", SOURCE)
terminal = importlib.util.module_from_spec(spec)
spec.loader.exec_module(terminal)

LOOPBACK = "127.0.0.1"
TIMEOUT = 8
USERNAME = "loopback-user"
PASSWORD = "synthetic-password"
Button = terminal.QMessageBox.StandardButton


class PasswordServer(asyncssh.SSHServer):
    def password_auth_supported(self):
        return True

    def validate_password(self, username, password):
        return username == USERNAME and password == PASSWORD


async def ssh_echo(process):
    process.stdout.write(b"ssh-ready\r\n")
    while data := await process.stdin.read(1024):
        process.stdout.write(data)


async def telnet_echo(reader, writer):
    try:
        writer.write(b"telnet-ready\r\n")
        while data := await reader.read(1024):
            writer.write(data)
            await writer.drain()
    finally:
        writer.close()


class ObservedWindow(terminal.MainWindow):
    successful_connections = 0

    def connection_ready(self):
        self.successful_connections += 1
        super().connection_ready()


class ConnectionTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.enterClassContext(patch.dict(os.environ, {"QT_QPA_PLATFORM": "offscreen"}))
        cls.app = terminal.QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        def answer_host_dialog(dialog):
            self.trust_defaults.append(dialog.standardButton(dialog.defaultButton()))
            return self.trust_answer

        state = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="pudding-test-")))
        self.enterContext(patch.object(terminal, "APP_DIR", state))
        self.enterContext(patch.object(serial.tools.list_ports, "comports", return_value=[]))
        self.enterContext(patch.object(serial, "Serial",
                                      side_effect=AssertionError("Unexpected physical Serial open")))
        self.warnings = self.enterContext(
            patch.object(terminal.QMessageBox, "warning", return_value=Button.Cancel)
        )
        self.enterContext(patch.object(terminal.QMessageBox, "exec", answer_host_dialog))
        self.enterContext(patch.object(terminal.QInputDialog, "exec", self.answer_credential_dialog))
        self.trust_answer = Button.No
        self.trust_defaults = []
        self.window = ObservedWindow()
        self.addAsyncCleanup(self.close_window)

    @staticmethod
    def answer_credential_dialog(dialog):
        answer = USERNAME if dialog.textEchoMode() == terminal.QLineEdit.EchoMode.Normal else PASSWORD
        dialog.setTextValue(answer)
        return terminal.QDialog.DialogCode.Accepted

    async def wait_for(self, condition, operation, *, require_worker=False):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + TIMEOUT
        while True:
            self.app.processEvents()
            if condition():
                return
            if (require_worker and self.window.worker is None) or loop.time() >= deadline:
                warnings = [(call.args[1], call.args[2]) for call in self.warnings.call_args_list]
                self.fail(f"{operation}. Status: {self.window.status.text()!r}. "
                          f"Worker active: {self.window.worker is not None}. Warnings: {warnings!r}")
            await asyncio.sleep(0.01)

    async def stop_worker(self):
        if self.window.worker is not None:
            self.window.toggle_connection()
            await self.wait_for(lambda: self.window.worker is None,
                                "Worker did not stop after disconnect")
        self.assertFalse(self.window.connected_state, "Connection did not return to idle")

    async def close_window(self):
        await self.stop_worker()
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def select_network(self, kind, port):
        self.window.kind.setCurrentText(kind)
        self.window.host_entries[kind].setText(LOOPBACK)
        self.window.port_entries[kind].setValue(port)

    async def connect(self, kind):
        self.window.toggle_connection()
        await self.wait_for(lambda: self.window.connected_state, f"{kind} did not connect",
                            require_worker=True)

    async def expect_text(self, text):
        await self.wait_for(
            lambda: any(text in row for row in self.window.terminal.model.screen.display),
            f"Terminal did not render {text!r}", require_worker=True,
        )

    @asynccontextmanager
    async def close_server(self, listener):
        try:
            yield listener
        finally:
            try:
                await self.stop_worker()
            finally:
                listener.close()
                try:
                    await asyncio.wait_for(listener.wait_closed(), timeout=TIMEOUT)
                except TimeoutError:
                    self.fail("Loopback server did not close after disconnect")

    def saved_host_keys(self):
        return json.loads(terminal.Preferences().get("ssh/host_keys", "{}"))

    async def start_ssh_server(self, key):
        listener = await asyncssh.create_server(
            PasswordServer, LOOPBACK, 0, server_host_keys=[key],
            process_factory=ssh_echo, encoding=None,
        )
        self.select_network("SSH", listener.get_port())
        return listener

    async def test_serial_loopback_echo_and_reconnect(self):
        opened = []

        def open_loopback(_port, *args, **kwargs):
            transport = serial.serial_for_url("loop://", *args, **kwargs)
            opened.append(transport)
            return transport

        self.window.kind.setCurrentText("Serial")
        self.window.com.setCurrentText("COM1")
        with patch.object(serial, "Serial", side_effect=open_loopback) as factory:
            for sequence in ("serial-first", "serial-second"):
                await self.connect("Serial")
                self.window.send_data(sequence.encode("ascii") + b"\r\n")
                await self.expect_text(sequence)
                await self.stop_worker()
                self.assertFalse(opened[-1].is_open, "Serial transport was left open")
            self.assertEqual(factory.call_count, 2, "Expected one open per connection attempt")

    async def test_serial_open_errors_report_manual_recovery_without_retry(self):
        self.window.kind.setCurrentText("Serial")
        self.window.com.setCurrentText("COM1")
        for exception in (serial.SerialException("synthetic busy port"),
                          PermissionError("synthetic access denied")):
            with self.subTest(error=type(exception).__name__):
                with patch.object(serial, "Serial", side_effect=exception) as factory:
                    self.window.toggle_connection()
                    await self.wait_for(lambda: self.window.worker is None,
                                        "Serial open error did not return to idle")
                self.assertEqual(factory.call_count, 1, "Expected a single Serial open attempt")
                self.assertIn("手動", self.window.status.text(), "Manual recovery hint missing")
                self.assertEqual(self.window.successful_connections, 0, "Failed Serial open connected")
                self.assertTrue(self.window.connect_button.isEnabled(), "Manual retry is unavailable")

    async def test_ssh_rejects_unknown_key_when_user_declines(self):
        key = asyncssh.generate_private_key("ssh-ed25519")
        async with self.close_server(await self.start_ssh_server(key)):
            self.window.toggle_connection()
            await self.wait_for(lambda: self.window.worker is None,
                                "SSH did not stop after declining host trust")
            self.assertEqual(self.trust_defaults, [Button.No], "SSH trust must default to No")
            self.assertEqual(self.window.successful_connections, 0, "SSH accepted the declined key")
            self.assertEqual(self.saved_host_keys(), {}, "Declined host key was saved")

    async def test_ssh_rejects_new_key_when_trust_cannot_be_saved(self):
        key = asyncssh.generate_private_key("ssh-ed25519")
        self.trust_answer = Button.Yes
        original_save = self.window.preferences.save

        def reject_key_save(values):
            if "ssh/host_keys" in values:
                raise OSError("synthetic trust write failure")
            return original_save(values)

        async with self.close_server(await self.start_ssh_server(key)):
            with patch.object(self.window.preferences, "save", side_effect=reject_key_save):
                self.window.toggle_connection()
                await self.wait_for(lambda: self.window.worker is None,
                                    "SSH did not stop after a failed trust save")
            self.assertEqual(self.trust_defaults, [Button.No], "New SSH key was never offered")
            self.assertEqual(self.window.successful_connections, 0, "SSH connected without saving trust")
            self.warnings.assert_called_once()
            self.assertEqual(self.saved_host_keys(), {}, "Failed trust save left a stored key")

    async def test_ssh_auth_echo_pinned_reconnect_and_key_rotation(self):
        key = asyncssh.generate_private_key("ssh-ed25519")
        self.trust_answer = Button.Yes
        async with self.close_server(await self.start_ssh_server(key)) as listener:
            endpoint = f"{LOOPBACK}:{listener.get_port()}"
            expected_keys = {endpoint: key.export_public_key().decode("ascii").strip()}
            await self.connect("SSH")
            await self.expect_text("ssh-ready")
            self.window.send_data(b"ssh-echo\r\n")
            await self.expect_text("ssh-echo")
            self.assertEqual(self.saved_host_keys(), expected_keys, "Accepted SSH key was not saved")
            await self.stop_worker()

            self.trust_answer = Button.No
            await self.connect("SSH")
            self.assertEqual(self.trust_defaults, [Button.No], "Pinned SSH key prompted again")
            await self.stop_worker()

            listener.update(server_host_keys=[asyncssh.generate_private_key("ssh-ed25519")])
            self.window.toggle_connection()
            await self.wait_for(lambda: self.window.worker is None,
                                "SSH did not stop after the same endpoint changed its key")
            self.assertEqual(self.window.successful_connections, 2, "SSH accepted a changed key")
            self.assertIn("SSH 主機金鑰", self.window.status.text(), "Changed key rejection missing")
            self.assertEqual(self.trust_defaults, [Button.No], "Changed SSH key prompted again")
            self.assertEqual(self.saved_host_keys(), expected_keys, "Changed key replaced the pin")

    async def test_telnet_cleartext_warning_defaults_to_cancel(self):
        self.select_network("Telnet", 1)
        with patch.object(telnetlib3, "open_connection") as open_connection:
            self.window.toggle_connection()
            self.warnings.assert_called_once()
            _, title, message, buttons, default = self.warnings.call_args.args
            self.assertTrue(title.startswith("Telnet"), f"Unexpected warning: {title!r}")
            self.assertIn("不會加密", message, "Telnet warning does not describe cleartext")
            self.assertEqual(buttons, Button.Yes | Button.Cancel)
            self.assertEqual(default, Button.Cancel, "Telnet cleartext warning must default to Cancel")
            self.assertIsNone(self.window.worker, "Cancelled Telnet warning created a worker")
            open_connection.assert_not_called()

    async def test_telnet_echo_and_negotiated_resize(self):
        sizes = []

        class RecordingServer(telnetlib3.TelnetServer):
            def on_naws(self, rows, cols):
                sizes.append((cols, rows))
                super().on_naws(rows, cols)

        server = await telnetlib3.create_server(
            host=LOOPBACK, port=0, protocol_factory=RecordingServer,
            shell=telnet_echo, encoding=False, connect_maxwait=1.0,
        )
        async with self.close_server(server):
            self.select_network("Telnet", server.sockets[0].getsockname()[1])
            self.warnings.return_value = Button.Yes
            screen = self.window.terminal.model.screen
            initial_size = (screen.columns, screen.lines)
            await self.connect("Telnet")
            await self.expect_text("telnet-ready")
            await self.wait_for(lambda: initial_size in sizes,
                                f"Telnet initial NAWS did not report {initial_size}", require_worker=True)
            self.window.send_data(b"telnet-echo\r\n")
            await self.expect_text("telnet-echo")
            # A column count of 255 exercises escaping of the Telnet IAC byte.
            self.window.terminal_resized(255, 29)
            await self.wait_for(lambda: (255, 29) in sizes,
                                "Telnet NAWS did not report resized grid (255, 29)", require_worker=True)
