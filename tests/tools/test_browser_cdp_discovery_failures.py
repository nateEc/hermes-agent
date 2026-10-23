"""CDP discovery faults remain actionable, redacted and task-local (#143418)."""

import contextvars
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading

import pytest

from tools import browser_cdp_tool  # Registers the real handler.
from tools import browser_session_cdp
from tools.registry import registry


@pytest.fixture
def discovery_server(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
    state = {"status": 503, "body": b"{}"}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(state["status"])
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(state["body"])

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}?token=fixture-cdp-secret"
    (home / "config.yaml").write_text(json.dumps({"browser": {"cdp_url": endpoint}}))
    try:
        yield endpoint, state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


@pytest.mark.parametrize("status,body,reason", [
    (503, b"{}", "HTTPError"),
    (200, b"not-json", "JSONDecodeError"),
    (200, b"{}", "webSocketDebuggerUrl"),
])
def test_discovery_faults_reach_the_tool_without_credentials_and_clear_on_recovery(
    discovery_server, status, body, reason, caplog,
):
    endpoint, state = discovery_server
    state.update(status=status, body=body)
    result = json.loads(registry.dispatch("browser_cdp", {"method": "Target.getTargets"}))
    assert "CDP discovery" in result["error"]
    assert reason in result["error"]
    assert "not a WebSocket URL" not in result["error"]
    assert "retry" in result["error"].lower()
    assert result["cdp_docs"] == browser_cdp_tool.CDP_DOCS_URL
    assert "fixture-cdp-secret" not in json.dumps(result) + caplog.text

    from tools.browser_session_cdp import last_cdp_discovery_failure

    assert last_cdp_discovery_failure(endpoint)
    websocket = "ws://127.0.0.1:9222/devtools/browser/recovered"
    state.update(status=200, body=json.dumps({"webSocketDebuggerUrl": websocket}).encode())
    assert browser_session_cdp.resolve_cdp_override(endpoint) == websocket
    assert last_cdp_discovery_failure(endpoint) == ""


def test_failed_discovery_state_is_bounded_to_the_current_resolution_and_context(discovery_server):
    endpoint, _ = discovery_server
    first, second = contextvars.copy_context(), contextvars.copy_context()
    result = json.loads(first.run(browser_cdp_tool.browser_cdp, "Target.getTargets"))
    assert "CDP discovery" in result["error"]

    from tools.browser_session_cdp import last_cdp_discovery_failure

    assert "HTTPError" in first.run(last_cdp_discovery_failure, endpoint)
    assert second.run(last_cdp_discovery_failure, endpoint) == ""
    # A new resolution, including a direct WebSocket/no-override path, retires stale errors.
    for direct in ("ws://127.0.0.1:9222/devtools/browser/direct", ""):
        first.run(browser_session_cdp.resolve_cdp_override, direct)
        assert first.run(last_cdp_discovery_failure, endpoint) == ""
        first.run(browser_session_cdp.resolve_cdp_override, endpoint)
        assert "HTTPError" in first.run(last_cdp_discovery_failure, endpoint)
