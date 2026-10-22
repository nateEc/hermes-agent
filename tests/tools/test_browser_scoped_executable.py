"""Tests for profile-scoped AGENT_BROWSER_EXECUTABLE_PATH resolution in routed profiles (#143170)."""

import os

from agent.secret_scope import (
    get_secret,
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


def test_scoped_browser_executable_path_honored_in_routed_profile(tmp_path, monkeypatch):
    """Reproduction for #143170: A secondary profile's AGENT_BROWSER_EXECUTABLE_PATH
    must be honored in _build_browser_env, chromium_executable, and _agent_browser_command_env."""
    renderer = str(tmp_path / "profile-owned-chrome")
    monkeypatch.delenv("AGENT_BROWSER_EXECUTABLE_PATH", raising=False)

    m_token = set_multiplex_context(True)
    s_token = set_secret_scope(
        {"AGENT_BROWSER_EXECUTABLE_PATH": renderer},
        profile_home=str(tmp_path / "secondary-profile"),
    )
    try:
        assert get_secret("AGENT_BROWSER_EXECUTABLE_PATH") == renderer
        assert _build_browser_env().get("AGENT_BROWSER_EXECUTABLE_PATH") == renderer
        assert chromium_executable() == renderer

        socket_dir = str(tmp_path / "probe-socket")
        cmd_env = _agent_browser_command_env(socket_dir)
        assert cmd_env.get("AGENT_BROWSER_EXECUTABLE_PATH") == renderer

        base_env = _base_subprocess_env()
        assert base_env.get("AGENT_BROWSER_EXECUTABLE_PATH") == renderer
    finally:
        reset_secret_scope(s_token)
        reset_multiplex_context(m_token)


def test_conflicting_launch_and_secondary_profile_browser_executable(tmp_path, monkeypatch):
    """When the gateway process has a launch-profile AGENT_BROWSER_EXECUTABLE_PATH,
    a secondary profile with its own override must resolve its own value."""
    launch_chrome = str(tmp_path / "launch-chrome")
    secondary_chrome = str(tmp_path / "secondary-chrome")
    monkeypatch.setenv("AGENT_BROWSER_EXECUTABLE_PATH", launch_chrome)

    m_token = set_multiplex_context(True)
    s_token = set_secret_scope(
        {"AGENT_BROWSER_EXECUTABLE_PATH": secondary_chrome},
        profile_home=str(tmp_path / "secondary-profile"),
    )
    try:
        assert _build_browser_env().get("AGENT_BROWSER_EXECUTABLE_PATH") == secondary_chrome
        assert chromium_executable() == secondary_chrome

        socket_dir = str(tmp_path / "probe-socket")
        cmd_env = _agent_browser_command_env(socket_dir)
        assert cmd_env.get("AGENT_BROWSER_EXECUTABLE_PATH") == secondary_chrome
    finally:
        reset_secret_scope(s_token)
        reset_multiplex_context(m_token)


def test_missing_scoped_override_does_not_inherit_launch_environ(tmp_path, monkeypatch):
    """When the secondary profile has NO override, it must NOT inherit the launch profile's
    AGENT_BROWSER_EXECUTABLE_PATH from os.environ. It must fall back to PM Chromium or None."""
    launch_chrome = str(tmp_path / "launch-chrome")
    monkeypatch.setenv("AGENT_BROWSER_EXECUTABLE_PATH", launch_chrome)

    m_token = set_multiplex_context(True)
    s_token = set_secret_scope(
        {},
        profile_home=str(tmp_path / "secondary-profile"),
    )
    try:
        # _build_browser_env must not contain launch_chrome
        assert _build_browser_env().get("AGENT_BROWSER_EXECUTABLE_PATH") is None

        # chromium_executable must never return launch_chrome
        resolved = chromium_executable()
        assert resolved != launch_chrome

        # _agent_browser_command_env must never contain launch_chrome
        socket_dir = str(tmp_path / "probe-socket")
        cmd_env = _agent_browser_command_env(socket_dir)
        assert cmd_env.get("AGENT_BROWSER_EXECUTABLE_PATH") != launch_chrome
    finally:
        reset_secret_scope(s_token)
        reset_multiplex_context(m_token)


def test_single_profile_unrouted_honors_environ(tmp_path, monkeypatch):
    """In single-profile mode (unrouted), os.environ override is honored."""
    single_chrome = str(tmp_path / "single-chrome")
    monkeypatch.setenv("AGENT_BROWSER_EXECUTABLE_PATH", single_chrome)

    set_multiplex_context(False)
    set_secret_scope(None)

    assert _build_browser_env().get("AGENT_BROWSER_EXECUTABLE_PATH") == single_chrome
    assert chromium_executable() == single_chrome

    socket_dir = str(tmp_path / "probe-socket")
    cmd_env = _agent_browser_command_env(socket_dir)
    assert cmd_env.get("AGENT_BROWSER_EXECUTABLE_PATH") == single_chrome


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
