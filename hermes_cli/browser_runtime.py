"""Read-only full-Chromium selection shared by browser launchers."""

from __future__ import annotations

import pm


def chromium_override() -> str | None:
    """This profile's explicit renderer; managed/system fallback remains the caller's policy."""
    from agent.secret_scope import current_secret_scope, get_secret, serves_routed_profile

    if serves_routed_profile():
        override = (current_secret_scope() or {}).get("AGENT_BROWSER_EXECUTABLE_PATH")
    else:
        override = get_secret("AGENT_BROWSER_EXECUTABLE_PATH")
    return override.strip() or None if override else None


def chromium_executable(*, allow_override: bool = True) -> str | None:
    """Prefer an explicit override unless the caller needs PM's own Chromium."""
    override = chromium_override() if allow_override else None
    if override:
        return override
    installed = pm.installed_package("chromium")
    return str(installed.binary) if installed and installed.binary is not None else None
