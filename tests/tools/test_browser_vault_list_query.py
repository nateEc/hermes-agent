"""Registry-level site filtering with a real encrypted local vault (#143214)."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.vault_store import VaultItemMeta, VaultStore
from tools import browser_vault_tool
from tools.registry import registry


@pytest.fixture
def query_vault(tmp_path, monkeypatch):
    from hermes_cli.config import atomic_config_write

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    atomic_config_write(home / "config.yaml", {"vault": {
        "onepassword": {"enabled": False}, "bitwarden": {"enabled": False},
    }})
    return home, VaultStore()


def _login(store, host, label="Personal login", identifier="user@example.com"):
    return store.add_item(kind="login", label=label, origin=f"https://{host}", secret={
        "identifier_type": "email", "identifier": identifier, "password": "fixture-vault-password",
    })


@pytest.mark.parametrize("query,indices", [
    (" GitHub ", [0]), ("PERSONAL", [0]), ("archive-user", [1]),
    ("ALT.EXAMPLE", [2]), ("missing-site", []), ("", [0, 1, 2]),
])
def test_query_reaches_real_metadata_backends_without_exposing_secrets_or_rewriting_the_vault(query_vault, monkeypatch, query, indices):
    from agent.vault_backends.local import LocalLoginBackend

    home, store = query_vault
    first = _login(store, "github.com")
    second = _login(store, "shop.example.org", label="Archive", identifier="archive-user@example.org")
    external = VaultItemMeta(id="op:fixture", kind="login", label="Workspace", origin="https://login.example",
                             created_at="", allowed_origins=("https://login.example", "https://alt.example"))
    # External-manager I/O is a metadata-only stand-in; the local backend and encrypted store are real.
    manager = SimpleNamespace(name="fixture", needs_unlock=False, list_items=lambda: [external])
    monkeypatch.setattr("agent.vault_backends.enabled_backends", lambda: [LocalLoginBackend(), manager])
    before = {p: p.read_bytes() for p in (home / "vault").glob("vault.*")}
    assert before

    result = json.loads(registry.dispatch("browser_vault", {"action": "list", "query": query}))

    handles = [first.id, second.id, external.id]
    assert result["success"]
    assert [item["handle"] for item in result["items"]] == [handles[i] for i in indices]
    assert "fixture-vault-password" not in json.dumps(result)
    if not indices:
        assert "missing-site" in result["hint"]
    assert {p: p.read_bytes() for p in (home / "vault").glob("vault.*")} == before


def test_large_unfiltered_vault_requires_a_query_but_small_and_filtered_lists_remain_usable(query_vault):
    home, store = query_vault
    cap = getattr(browser_vault_tool, "_LIST_UNFILTERED_MAX", 50)
    for index in range(cap):
        _login(store, f"site-{index}.example", identifier=f"account-{index}@example.org")
    small = json.loads(registry.dispatch("browser_vault", {"action": "list"}))
    assert len(small["items"]) == cap
    _login(store, "target.example", identifier="private-unrelated@example.org")
    before = {p: p.read_bytes() for p in (home / "vault").glob("vault.*")}
    assert before

    large = json.loads(registry.dispatch("browser_vault", {"action": "list", "query": "  "}))
    assert large["items"] == [] and large["total"] == cap + 1
    assert "query" in large["hint"]
    assert "private-unrelated@example.org" not in json.dumps(large)
    filtered = json.loads(registry.dispatch("browser_vault", {"action": "list", "query": "TARGET.EXAMPLE"}))
    assert [item["origin"] for item in filtered["items"]] == ["https://target.example"]
    assert {p: p.read_bytes() for p in (home / "vault").glob("vault.*")} == before
