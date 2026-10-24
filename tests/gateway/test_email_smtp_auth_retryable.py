"""SMTP AUTH reply classification reaches the real reconnect gate (#144106)."""

import asyncio
import socketserver
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


class _SMTPAuthWire(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(5)
        self.wfile.write(b"220 loopback.test SMTP\r\n")
        for line in self.rfile:
            command = line.split(None, 1)[0].upper()
            if command == b"EHLO":
                self.wfile.write(b"250-loopback.test\r\n250 AUTH PLAIN\r\n")
            elif command == b"AUTH":
                self.server.auth_attempts += 1
                self.wfile.write(f"{self.server.reply_code} synthetic AUTH reply\r\n".encode())
            elif command == b"QUIT":
                self.wfile.write(b"221 goodbye\r\n")
                return


@pytest.fixture
def smtp_wire():
    with socketserver.TCPServer(("127.0.0.1", 0), _SMTPAuthWire) as server:
        server.auth_attempts = 0
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join(timeout=5)


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [421, 450, 454, 534, 535])
async def test_smtp_auth_reply_controls_secondary_reconnect(code, smtp_wire, tmp_path, monkeypatch):
    from gateway.config import Platform, PlatformConfig
    from gateway.run import _profile_runtime_scope
    from gateway.run_adapters import GatewayAdapterLifecycleMixin
    from plugins.platforms.email.adapter import EmailAdapter

    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (home / ".env").write_text(
        "EMAIL_ADDRESS=agent@loopback.test\nEMAIL_PASSWORD=synthetic-password\n"
        "EMAIL_SMTP_HOST=127.0.0.1\nEMAIL_SMTP_SECURITY=plain\n"
        f"EMAIL_SMTP_PORT={smtp_wire.server_address[1]}\n",
        encoding="utf-8",
    )
    smtp_wire.reply_code = code
    retained = []
    schedule = Mock()
    runner = SimpleNamespace(
        _running=True,
        _schedule_secondary_profile_reconnect=schedule,
        _retain_background_task=retained.append,
    )
    with _profile_runtime_scope(home):
        adapter = EmailAdapter(PlatformConfig(enabled=True))
        assert adapter._probe_smtp() is False
        assert smtp_wire.auth_attempts == 1
        assert adapter.fatal_error_code == "email_auth_error"
        GatewayAdapterLifecycleMixin._schedule_secondary_profile_startup_reconnect(
            runner, "secondary", Platform.EMAIL, adapter,
        )
        await asyncio.gather(*retained)

    assert adapter.fatal_error_retryable is (400 <= code < 500)
    if code < 500:
        schedule.assert_called_once_with("secondary", Platform.EMAIL, adapter)
    else:
        schedule.assert_not_called()
