"""Regression coverage for a client error reply to a batch clarify (#144158).

``resolve_response`` used to settle an error frame to ``result=None, answered=False``
without going through :func:`tools.clarify_batch.settle_batch`, so a batch clarify came
back to the agent as ``{"answers": {}, "outcome": "undelivered"}`` — the question looked
never-delivered (agents re-present it, the user sees it twice) and answers already locked
through ``clarify.lock`` were dropped. ``settle_batch``'s contract, the session owner's
registry (``gateway/session_server_requests.py::resolve``) and ``cancel()`` all treat an
error frame as a cancel-all: locked answers stand, outcome ``cancelled``.
"""

from __future__ import annotations

import pytest

from tests.tui_gateway import test_protocol
from tui_gateway import server_requests

server = test_protocol.server
capture = test_protocol.capture


@pytest.mark.parametrize("locked, answer", [(False, None), (True, "kept"), (True, None)])
def test_clarify_batch_error_reply_settles_cancelled_and_keeps_locks(capture, locked, answer):
    """A client error reply to a batch clarify is a cancel-all per settle_batch's contract —
    outcome "cancelled" with the locked answers kept, not "undelivered" with empty answers."""
    srv, buf = capture
    thread, box, req = test_protocol._start_batch_clarify(srv, buf, ["q0", "q1"])
    try:
        if locked:
            response = srv.handle_request({"id": "e1", "method": "clarify.lock",
                                          "params": {"request_id": req.id, "question_id": "q0", "answer": answer}})
            assert response["result"]["status"] == "ok"
        assert srv.dispatch({"jsonrpc": "2.0", "id": req.id,
                             "error": {"code": -32601, "message": "no handler"}}) is None
        thread.join(timeout=10)
        assert not thread.is_alive()
        assert box["answer"] == {"answers": {"q0": answer} if locked else {}, "outcome": "cancelled"}
        assert server_requests.open_requests("s1") == []
        # A late success or interrupt cannot overwrite the error's terminal outcome.
        assert not server_requests.resolve_response({"id": req.id, "result": {"answers": {"q1": "late"}}})
        assert server_requests.cancel("s1") == 0
    finally:
        server_requests.cancel("s1")
        thread.join(timeout=10)
