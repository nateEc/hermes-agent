"""Independent pairing grants must survive absent DM role metadata (#143409)."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.secret_scope import reset_multiplex_context, set_multiplex_context
from gateway.pairing import PairingStore
from gateway.run import _profile_runtime_scope
from plugins.platforms.discord.adapter import (
    ClarifyChoiceView, ExecApprovalView, ModelPickerView, SlashConfirmView, UpdatePromptView,
)
from plugins.platforms.discord.adapter_component_auth import _component_check_auth


@pytest.fixture
def profile_homes(tmp_path, monkeypatch, request):
    home = tmp_path / ".hermes"
    secondary = home / "profiles" / "secondary"
    for target in (home, secondary):
        target.mkdir(parents=True, exist_ok=True)
        (target / "config.yaml").write_text("{}")
        (target / ".env").write_text(
            "DISCORD_ALLOW_ALL_USERS=false\nGATEWAY_ALLOW_ALL_USERS=false\n"
            "GATEWAY_ALLOWED_USERS=\nDISCORD_ALLOWED_USERS=\n"
        )
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    token = set_multiplex_context(True)
    request.addfinalizer(lambda: reset_multiplex_context(token))
    return home, secondary


def _approve(store):
    code = store.generate_code("discord", "11111", "Fixture user")
    assert code is not None
    assert store.approve_code("discord", code)["user_id"] == "11111"


@pytest.mark.parametrize("paired", [False, True])
@pytest.mark.parametrize("shape", ["missing", "none", "invalid", "empty", "other", "matching"])
def test_pairing_and_role_grants_are_independent_and_unknown_users_stay_denied(profile_homes, shape, paired):
    with _profile_runtime_scope(profile_homes[0]):
        store = PairingStore()
        if paired:
            _approve(store)
        roles = {"none": None, "invalid": 42, "empty": [],
                 "other": [SimpleNamespace(id=7)], "matching": [SimpleNamespace(id=42)]}
        user = SimpleNamespace(id=11111)
        if shape != "missing":
            user.roles = roles[shape]
        interaction = SimpleNamespace(user=user)
        assert _component_check_auth(interaction, set(), {42}) is (paired or shape == "matching")


@pytest.mark.asyncio
async def test_component_views_follow_profile_pairing_revocation_and_admin_gates(profile_homes):
    home, secondary = profile_homes
    interaction = SimpleNamespace(user=SimpleNamespace(id=11111))  # DM User: no roles attribute.

    async def selected(*_):
        return ""

    with _profile_runtime_scope(home):
        store = PairingStore()
        _approve(store)
        policy = {"allowed_user_ids": set(), "allowed_role_ids": {42}}
        views = [
            ClarifyChoiceView(choices=["yes"], clarify_id="c", **policy),
            ExecApprovalView(session_key="s", **policy),
            SlashConfirmView(session_key="s", confirm_id="c", **policy),
            UpdatePromptView(session_key="s", **policy),
            ModelPickerView(providers=[], current_model="m", current_provider="p", session_key="s",
                            on_model_selected=selected, **policy),
        ]
        for view in views:
            assert view._check_auth(interaction) is True
        admin = ExecApprovalView(session_key="s", require_admin=True, admin_user_ids={"22222"}, **policy)
        assert admin._check_auth(interaction) is False
        assert _component_check_auth(SimpleNamespace(user=None), set(), {42}) is False

        with _profile_runtime_scope(secondary):
            assert PairingStore().list_approved("discord") == []
            for view in views:
                assert view._check_auth(interaction) is False
        for view in views:
            assert view._check_auth(interaction) is True

        # A separately constructed store mirrors a CLI revoke, not this caller's cached instance.
        assert PairingStore().revoke("discord", "11111") is True
        assert not store.is_approved("discord", "11111")
        for view in views:
            assert view._check_auth(interaction) is False
        role_holder = SimpleNamespace(user=SimpleNamespace(id=22222, roles=[SimpleNamespace(id=42)]))
        assert _component_check_auth(role_holder, set(), {42}) is True
