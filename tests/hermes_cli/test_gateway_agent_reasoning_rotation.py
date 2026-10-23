"""Classic CLI session rotation carries the viewer's reasoning preference (#143414)."""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from hermes_cli.gateway_agent import GatewayAgent
from hermes_cli.gateway_connection import GatewayConnection
from hermes_cli.gateway_session_store import GatewaySessionStore
from tests.hermes_cli import test_gateway_agent as scripted_gateway

make_agent = scripted_gateway.make_agent


@pytest.mark.parametrize("shown", [True, False])
def test_rotation_resends_the_current_preference_once_per_session(make_agent, shown):
    callback = (lambda text: None) if shown else None
    agent, client = make_agent([], reasoning_callback=callback)
    for sid in ("local-new", "local-resumed", "local-branch"):
        agent.session_id = sid
        agent.session_id = sid  # idempotent re-attachment must not write twice
        writes = [p for m, p in client.calls if m == "config.set" and p.get("key") == "show_reasoning"]
        assert writes[-1] == {"session_id": sid, "key": "show_reasoning", "value": "1" if shown else "0"}
        agent.reasoning_callback = callback  # the ordinary callback setter still deduplicates
    assert [p["session_id"] for m, p in client.calls
            if m == "config.set" and p.get("key") == "show_reasoning"] == [
        scripted_gateway.SID, "local-new", "local-resumed", "local-branch"]


def test_rotation_updates_the_real_gateway_display_gate_without_changing_launch_config(tmp_path, monkeypatch):
    from gateway import local_viewer_events, run
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    from gateway.session_authority import initialize_session_authority
    from gateway.session_controls import AuthorityConnection

    home = tmp_path / "home"
    home.mkdir()
    config = 'model: {default: m, provider: custom, base_url: "http://127.0.0.1:9/v1"}\nplatform_toolsets: {cli: []}\n'
    (home / "config.yaml").write_text(config)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(run, "_hermes_home", home)
    owners = []

    @asynccontextmanager
    async def connect():
        sessions = SessionStore(home / "sessions", GatewayConfig())
        runner = SimpleNamespace(session_store=sessions, _session_db=sessions._db,
                                 adapters={}, _draining=False, _cached_agent_for=lambda route: None)
        runner._adapter_for_source = lambda source: runner.adapters.get(source.platform)
        authority = await initialize_session_authority(runner, profile_id=str(home), instance_id="rotation")
        owner = AuthorityConnection(authority, object(), {"user_id": "human"})
        owners.append(authority)

        class Client(scripted_gateway.FakeClient):
            async def rpc(self, method, *, _timeout=None, **params):
                reply = await owner.dispatch({"id": 1, "method": method, "params": params})
                assert "result" in reply, reply
                return reply["result"]

        try:
            yield Client([])
        finally:
            await owner.close()
            sessions._db.close()

    connection = GatewayConnection(connect=connect)
    store = GatewaySessionStore(connection)
    try:
        connection.start(30)
        first = store.create({})
        agent = GatewayAgent(session_db=store, session_id=first, reasoning_callback=lambda text: None)
        fresh = store.create({})
        authority = owners[0]
        old_turn = SimpleNamespace(_approval_owner=(authority, first, 1))
        new_turn = SimpleNamespace(_approval_owner=(authority, fresh, 1))
        assert local_viewer_events.show_reasoning(old_turn)
        assert not local_viewer_events.show_reasoning(new_turn), "the fresh CLI session must start unset"
        agent.session_id = fresh
        assert local_viewer_events.show_reasoning(new_turn), "rotation lost the gateway's live display flag"
        agent.reasoning_callback = None
        assert not local_viewer_events.show_reasoning(new_turn)
        assert local_viewer_events.show_reasoning(old_turn), "the old session's display flag was overwritten"
        assert (home / "config.yaml").read_text() == config
    finally:
        connection.close()
