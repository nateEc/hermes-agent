"""Desktop UIDs must not corrupt Matrix reply relations on text or local-media sends (#143137)."""

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.matrix.adapter import MatrixAdapter


async def _send_case(tmp_path, kind, reply_to, thread_id):
    adapter = MatrixAdapter(PlatformConfig(enabled=True, extra={
        "homeserver": "https://matrix.example.org", "user_id": "@hermes:example.org",
        "e2ee_mode": "off", "max_message_length": 100,
    }))
    adapter._client = SimpleNamespace(
        send_message_event=AsyncMock(return_value="$sent_event"),
        upload_media=AsyncMock(return_value="mxc://example.org/synthetic"), crypto=None,
    )
    metadata = {"thread_id": thread_id, "ordinary": "unchanged"} if thread_id else {"ordinary": "unchanged"}
    before = copy.deepcopy(metadata)
    if kind == "text":
        result = await adapter.send("!room:example.org", "hello Matrix " * 40, reply_to, metadata)
    else:
        suffix = {"image": ".png", "document": ".txt", "audio": ".wav", "video": ".webm"}[kind]
        file = tmp_path / ("attachment" + suffix)
        file.write_bytes(b"synthetic media contents")
        send = {"image": adapter.send_image_file, "document": adapter.send_document,
                "audio": adapter.send_voice, "video": adapter.send_video}[kind]
        options = {"is_voice": False} if kind == "audio" else {}
        result = await send("!room:example.org", str(file), caption="hello Matrix", reply_to=reply_to,
                            metadata=metadata, **options)
        assert file.read_bytes() == b"synthetic media contents"
        adapter._client.upload_media.assert_awaited_once()
    assert result.success and result.message_id == "$sent_event"
    assert metadata == before
    calls = adapter._client.send_message_event.await_args_list
    assert len(calls) > 1 if kind == "text" else len(calls) == 1
    payloads = [call.args[2] for call in calls]
    assert all(payload["body"] for payload in payloads)
    if kind != "text":
        assert payloads[0]["body"] == "hello Matrix"
        assert payloads[0]["url"] == "mxc://example.org/synthetic"
    return payloads


@pytest.mark.parametrize("kind", ["text", "image", "document", "audio", "video"])
@pytest.mark.parametrize("thread_id", [None, "$thread_root:example.org"])
@pytest.mark.parametrize("reply_to", ["a51ff6d975da4faa9d447277d9fc439a", "@not-an-event:example.org", "", None])
def test_non_event_targets_drop_only_the_reply_decoration(tmp_path, kind, thread_id, reply_to):
    payloads = asyncio.run(_send_case(tmp_path, kind, reply_to, thread_id))
    for payload in payloads:
        if thread_id:
            relation = payload["m.relates_to"]
            assert relation == {"rel_type": "m.thread", "event_id": thread_id, "is_falling_back": True,
                                "m.in_reply_to": {"event_id": thread_id}}
        else:
            assert "m.relates_to" not in payload


@pytest.mark.parametrize("kind", ["text", "image", "document", "audio", "video"])
@pytest.mark.parametrize("thread_id", [None, "$thread_root:example.org"])
@pytest.mark.parametrize("reply_to", ["$legacy:example.org", "$opaque/plus+event", "$url_safe-event_id"])
def test_valid_opaque_event_targets_are_preserved_on_every_send_path(tmp_path, kind, thread_id, reply_to):
    payloads = asyncio.run(_send_case(tmp_path, kind, reply_to, thread_id))
    for payload in payloads:
        relation = payload["m.relates_to"]
        assert relation["m.in_reply_to"]["event_id"] == reply_to
        if thread_id:
            assert relation["event_id"] == thread_id and relation["rel_type"] == "m.thread"
        else:
            assert set(relation) == {"m.in_reply_to"}
