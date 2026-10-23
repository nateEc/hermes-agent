"""A browser renderer belongs to its profile; PM composition must not replace it (#143170)."""

import os

import pytest

from agent.secret_scope import (
    reset_multiplex_context,
    reset_secret_scope,
    set_multiplex_context,
    set_secret_scope,
)
from hermes_cli.browser_runtime import chromium_executable
from tools.browser_tool import _build_browser_env
from tools.browser_tool_session import _agent_browser_command_env
from tools.browser_use_cli import _base_subprocess_env

pytest_plugins = ["tests.tools.test_browser_pm"]


@pytest.mark.parametrize(("multiplex", "scoped", "launch"), [
    (True, True, False),
    (True, True, True),
    (True, False, True),
    (False, False, True),
])
def test_renderer_selection_preserves_profile_and_single_process_precedence(
        browser_store, tmp_path, monkeypatch, multiplex, scoped, launch):
    _, _, publish = browser_store
    managed = str(publish("chromium"))
    renderer = str(tmp_path / "profile-owned-chrome")
    process_renderer = str(tmp_path / "launch-chrome")
    if launch:
        monkeypatch.setenv("AGENT_BROWSER_EXECUTABLE_PATH", process_renderer)
    else:
        monkeypatch.delenv("AGENT_BROWSER_EXECUTABLE_PATH", raising=False)
    scope = {"AGENT_BROWSER_EXECUTABLE_PATH": renderer} if scoped else {}
    multiplex_token = set_multiplex_context(multiplex)
    scope_token = set_secret_scope(scope if multiplex else None,
                                   profile_home=str(tmp_path / "secondary") if multiplex else None)
    override = renderer if scoped else (None if multiplex else process_renderer)
    try:
        assert _build_browser_env().get("AGENT_BROWSER_EXECUTABLE_PATH") == override
        assert _base_subprocess_env().get("AGENT_BROWSER_EXECUTABLE_PATH") == override
        assert chromium_executable() == (override or managed)
        assert chromium_executable(allow_override=False) == managed
        assert _agent_browser_command_env(str(tmp_path / "socket"))["AGENT_BROWSER_EXECUTABLE_PATH"] == (
            override or managed)
    finally:
        reset_secret_scope(scope_token)
        reset_multiplex_context(multiplex_token)


def test_profile_files_round_trip_a_b_a_after_pm_composition(browser_store, tmp_path, monkeypatch):
    """The actual gateway binder reads each home's .env; PM must not restamp its renderer."""
    from gateway.run import _profile_runtime_scope

    _, _, publish = browser_store
    managed = str(publish("chromium"))
    launch = str(tmp_path / "launch-renderer")
    monkeypatch.setenv("AGENT_BROWSER_EXECUTABLE_PATH", launch)
    homes = {}
    for name in ("a", "b"):
        home = tmp_path / name
        home.mkdir()
        (home / "config.yaml").write_text("model:\n  default: fixture\n", encoding="utf-8")
        renderer = str(home / "owned-renderer")
        (home / ".env").write_text(f"AGENT_BROWSER_EXECUTABLE_PATH={renderer}\n", encoding="utf-8")
        homes[name] = (home, renderer)

    multiplex = set_multiplex_context(True)
    try:
        for name in ("a", "b", "a"):
            home, renderer = homes[name]
            with _profile_runtime_scope(home):
                assert chromium_executable() == renderer
                assert _build_browser_env()["AGENT_BROWSER_EXECUTABLE_PATH"] == renderer
                assert _base_subprocess_env()["AGENT_BROWSER_EXECUTABLE_PATH"] == renderer
                assert _agent_browser_command_env(str(home / "socket"))["AGENT_BROWSER_EXECUTABLE_PATH"] == renderer
            assert os.environ["AGENT_BROWSER_EXECUTABLE_PATH"] == launch

        home, _ = homes["b"]
        (home / ".env").write_text("", encoding="utf-8")
        with _profile_runtime_scope(home):
            assert "AGENT_BROWSER_EXECUTABLE_PATH" not in _build_browser_env()
            assert "AGENT_BROWSER_EXECUTABLE_PATH" not in _base_subprocess_env()
            assert _agent_browser_command_env(str(home / "socket"))["AGENT_BROWSER_EXECUTABLE_PATH"] == managed
    finally:
        reset_multiplex_context(multiplex)
