"""Admitted Mattermost posts keep the gateway's addressed-turn silence contract."""

import json
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from gateway.response_filters import is_intentional_silence_response
from plugins.platforms.mattermost.adapter import MattermostAdapter
from tests.gateway.test_gateway_silence_tokens import _runner


def _adapter(extra=None):
    adapter = MattermostAdapter(PlatformConfig(
        enabled=True, token="test-token",
        extra={"url": "https://mattermost.invalid", "require_mention": False, **(extra or {})},
    ))
    adapter._bot_user_id, adapter._bot_username = "bot-id", "hermes-bot"
    adapter._reply_mode = "thread"
    adapter.handle_message = AsyncMock()
    return adapter


def _post(text, *, channel_type="O", root_id="", props=None, post_id="post-1"):
    return {"event": "posted", "data": {
        "channel_type": channel_type, "sender_name": "@alice",
        "post": json.dumps({"id": post_id, "user_id": "alice-id", "channel_id": "channel-1",
                            "message": text, "root_id": root_id, "props": props or {}}),
    }}


@pytest.mark.asyncio
@pytest.mark.parametrize("text, options, expected", [
    ("hello", {"channel_type": "D"}, True),
    ("@hermes-bot check this", {}, True),
    ("@HERMES-BOT check this", {"root_id": "root"}, True),
    ("@bot-id check this", {}, True),
    ("@alice and @hermes-bot check this", {}, True),
    (" /status", {}, True),
    ("@hermes-bot /status", {}, True),
    ("@alice check this", {"root_id": "root"}, False),
    ("@alice check this", {}, False),
    ("side chatter", {}, False),
    ("done?", {"root_id": "root"}, None),
    ("status notice", {"root_id": "root", "props": {"from_bot": "true"}}, False),
    ("status notice", {"root_id": "root", "props": {"from_bot": True}}, False),
    ("done?", {"root_id": "root", "props": {"from_bot": "false"}}, None),
    ("@hermes-bot check this", {"root_id": "root", "props": {"from_bot": "true"}}, True),
    ("hello", {"channel_type": "D", "props": {"from_bot": "true"}}, True),
    ("@hermes-bot-other check this", {"root_id": "root"}, False),
])
async def test_post_reply_intent_survives_parsing_and_mention_stripping(text, options, expected):
    adapter = _adapter()
    await adapter._handle_ws_event(_post(text, **options))
    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.reply_expected is expected
    assert event.raw_message["message"] == text
    if "@hermes-bot" in text.lower():
        assert "@hermes-bot" not in event.text.lower()
    # A flat channel is an ongoing conversation, not an unaddressed new thread.
    if text == "side chatter":
        adapter._reply_mode = "off"
        await adapter._handle_ws_event(_post(text, post_id="post-2"))
        assert adapter.handle_message.await_args.args[0].reply_expected is None


@pytest.mark.asyncio
@pytest.mark.parametrize("text, options, silent", [
    ("side chatter", {}, True),
    ("status notice", {"root_id": "root", "props": {"from_bot": "true"}}, True),
    ("@alice check this", {"root_id": "root"}, True),
    ("done?", {"root_id": "root"}, False),
    ("@hermes-bot check this", {}, False),
    ("hello", {"channel_type": "D"}, False),
])
async def test_parsed_intent_controls_real_gateway_silence_fallback(monkeypatch, tmp_path, text, options, silent):
    adapter = _adapter()
    await adapter._handle_ws_event(_post(text, **options))
    event = adapter.handle_message.await_args.args[0]
    runner = _runner(monkeypatch, tmp_path)
    runner.session_store.get_model_override.return_value = None
    runner._run_agent = AsyncMock(return_value={
        "final_response": "NO_REPLY", "failed": False,
        "messages": [{"role": "user", "content": event.text}, {"role": "assistant", "content": "NO_REPLY"}],
        "tools": [], "history_offset": 0, "last_prompt_tokens": 0, "api_calls": 1,
    })
    entry = runner.session_store.get_or_create_session.return_value
    response = await runner._handle_message_with_agent(event, event.source, entry.session_key, 1)
    if silent:
        assert response == ""
    else:
        assert response and not is_intentional_silence_response(response)
