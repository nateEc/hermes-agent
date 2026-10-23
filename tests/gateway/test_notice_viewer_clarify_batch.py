"""A notice uses only batch support declared by the session's currently attached viewers."""

import asyncio
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.session_cli_turn import clarify_batch_turn, cli_turn_scope
from gateway.session_controls import AuthorityConnection
from tests.gateway.test_local_viewer_clarify_batch import QUESTIONS, _Peer
from tests.gateway.test_surface_admission import _authority
from tests.waiting import async_wait_until


def _turn(authority, kind="process_complete"):
    from gateway.run_turn_runner import TurnRunner
    turn = TurnRunner.__new__(TurnRunner)
    turn._approval_owner = (authority, "s", 1)
    turn._ctx = SimpleNamespace(source=SimpleNamespace(platform=Platform.LOCAL),
                                inbound_message_id="notice", persist_user_display_kind=kind)
    return turn


async def _rpc(connection, method, **params):
    result = await connection.dispatch({"id": 1, "method": method, "params": {"session_id": "s", **params}})
    assert "result" in result, result
    return result["result"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["process_complete", "async_delegation_complete", "internal_notification"])
async def test_notice_callback_uses_live_batch_viewer_without_inheriting_other_turn_fields(tmp_path, monkeypatch, kind):
    from gateway import run_turn_runner

    async def answer(event):
        return "ok"
    authority = await _authority(tmp_path, monkeypatch, answer)
    monkeypatch.setattr(authority, "_schedule", lambda ref: None)
    peer = _Peer()
    connection = AuthorityConnection(authority, peer, {"user_id": "owner"})
    cards = []
    turn = _turn(authority, kind)
    agent = SimpleNamespace(_interrupt_requested=False)
    turn._ctx.agent_holder = [agent]
    turn._ask_clarify_question = lambda question, *_a, **_k: cards.append(question) or ("blue", True)
    monkeypatch.setattr(run_turn_runner, "_finite_viewer_turn", lambda: False)
    worker = None
    try:
        await _rpc(connection, "session.resume")
        await _rpc(connection, "prompt.submit", submission_id="human", text="start", clarify_batch=True)
        # A different admission's author/API text/model swap never becomes this notice's state.
        with cli_turn_scope("human", {"clarify_batch": True, "api_text": "private turn note"}):
            worker = asyncio.create_task(asyncio.to_thread(turn._clarify_callback_sync, QUESTIONS))
            await async_wait_until(lambda: worker.done() or any(frame.get("method") == "clarify" for frame in peer.frames),
                                   timeout=10, what="notice clarification batch")
            requests = [frame for frame in peer.frames if frame.get("method") == "clarify"]
            assert len(requests) == 1 and cards == []
            assert [q["qid"] for q in requests[0]["params"]["questions"]] == [q["qid"] for q in QUESTIONS]
            connection._resolve_response({"jsonrpc": "2.0", "id": requests[0]["id"], "result": {
                "answers": {"q0": "red", "q1": "large"}, "outcome": "submitted"}})
            result = await asyncio.wait_for(worker, 10)
        assert result == {"answers": {"q0": "red", "q1": "large"}, "outcome": "submitted"}
        # An ordinary viewer submit without the flag retains the per-admission contract.
        turn._ctx.persist_user_display_kind = None
        assert not clarify_batch_turn(turn)
    finally:
        agent._interrupt_requested = True
        await connection.close()
        if worker is not None:
            await asyncio.wait_for(worker, 10)
        authority.db.close()


@pytest.mark.asyncio
async def test_capability_follows_accepted_submits_and_live_subscription_lifetime(tmp_path, monkeypatch):
    async def answer(event):
        return "ok"
    authority = await _authority(tmp_path, monkeypatch, answer)
    monkeypatch.setattr(authority, "_schedule", lambda ref: None)
    first = AuthorityConnection(authority, _Peer(), {"user_id": "owner"})
    second = AuthorityConnection(authority, _Peer(), {"user_id": "owner"})
    turn = _turn(authority)
    try:
        await _rpc(first, "session.resume")
        rejected = await first.dispatch({"id": 1, "method": "prompt.submit", "params": {
            "session_id": "s", "submission_id": "bad", "text": "start", "clarify_batch": "true"}})
        assert "error" in rejected and not clarify_batch_turn(turn)
        await _rpc(first, "prompt.submit", submission_id="a", text="start", clarify_batch=True)
        assert clarify_batch_turn(turn)
        await _rpc(second, "session.resume")
        assert not clarify_batch_turn(turn), "an undeclared viewer may understand only per-question cards"
        await _rpc(second, "prompt.submit", submission_id="b", text="start", clarify_batch=True)
        assert clarify_batch_turn(turn)
        await _rpc(second, "session.detach", subscription_id=second.subscriptions["s"])
        assert clarify_batch_turn(turn)
        await _rpc(first, "session.detach", subscription_id=first.subscriptions["s"])
        assert not clarify_batch_turn(turn)
        await _rpc(first, "session.resume")
        assert not clarify_batch_turn(turn), "reattachment alone must not revive a retired declaration"
        await _rpc(first, "prompt.submit", submission_id="c", text="start", clarify_batch=True)
        assert clarify_batch_turn(turn)
        await _rpc(first, "prompt.submit", submission_id="d", text="start", clarify_batch=False)
        assert not clarify_batch_turn(turn)
        await _rpc(first, "prompt.submit", submission_id="e", text="start", clarify_batch=True)
        authority._retire_overflowed("s", first.transport)
        assert not clarify_batch_turn(turn)
    finally:
        await first.close()
        await second.close()
        authority.db.close()
