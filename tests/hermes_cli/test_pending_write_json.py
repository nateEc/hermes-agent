"""Full, read-only pending-write review through the real command and disk store (#143181)."""

import json
from pathlib import Path

import pytest

from hermes_cli.write_approval_commands import handle_pending_subcommand
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tools import write_approval as wa


@pytest.fixture
def review_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "api_server")
    token = set_hermes_home_override(home)
    try:
        yield home
    finally:
        reset_hermes_home_override(token)


@pytest.mark.parametrize("origin", ["foreground", "background_review"])
def test_cli_json_preserves_the_full_staged_patch_without_applying_it(review_home, capsys, origin):
    from hermes_cli.cli_commands_mixin import CLICommandsMixin
    from hermes_cli.config import atomic_config_write
    from tools.memory_tool import MemoryStore, memory_tool
    from tools.skill_provenance import reset_current_write_origin, set_current_write_origin

    atomic_config_write(review_home / "config.yaml", {"memory": {"write_approval": True}})
    store = MemoryStore()
    store.load_from_disk()
    old = "User prefers morning meetings."
    new = "User prefers afternoon meetings with a written agenda. " * 8 + "完整结尾：需要事先确认时区。"
    assert store.add("memory", old)["success"]
    before = (review_home / "memories" / "MEMORY.md").read_bytes()
    token = set_current_write_origin(origin)
    try:
        result = json.loads(memory_tool("patch", "memory", new, old_text=old, store=store))
    finally:
        reset_current_write_origin(token)
    assert result["staged"]
    [record] = wa.list_pending(wa.MEMORY)
    assert new not in record["summary"]

    cli = CLICommandsMixin.__new__(CLICommandsMixin)
    cli.agent = None
    capsys.readouterr()
    cli._handle_memory_command("/memory pending --json")
    output = json.loads(capsys.readouterr().out)

    assert output == {"count": 1, "records": [record]}
    assert output["records"][0]["payload"] == {
        "action": "patch", "target": "memory", "content": new, "old_text": old, "matched_entry": old,
    }
    assert (review_home / "memories" / "MEMORY.md").read_bytes() == before
    assert wa.get_pending(wa.MEMORY, record["id"]) == record


@pytest.mark.parametrize("subsystem", [wa.MEMORY, wa.SKILLS])
def test_json_review_keeps_nested_records_profile_local_and_leaves_the_default_list_alone(review_home, subsystem):
    payload = {"action": "batch", "target": "memory", "operations": [
        {"action": "patch", "old_text": "before", "content": "完整替换文本\n" * 80, "matched_entry": "before"},
        {"action": "add", "content": "[full] details"},
    ]}
    if subsystem == wa.SKILLS:
        payload = {"action": "batch", "name": "example", "operations": [
            {"action": "write_file", "file_path": "references/review.md", "file_content": "完整替换文本\n" * 80},
            {"action": "edit", "old_string": "before", "new_string": "[full] details"},
        ]}
    records = [wa.stage_write(subsystem, payload, summary=origin, origin=origin)
               for origin in ("foreground", "background_review")]
    pending_files = {p: p.read_bytes() for p in (review_home / "pending" / subsystem).glob("*.json")}
    text = handle_pending_subcommand(subsystem, ["pending"])
    assert "foreground" in text and "background_review" in text

    token = set_hermes_home_override(review_home.parent / "other-home")
    try:
        assert json.loads(handle_pending_subcommand(subsystem, ["pending", "--json"])) == {"count": 0, "records": []}
        wa.stage_write(subsystem, {"action": "add", "content": "other profile"}, summary="other", origin="foreground")
    finally:
        reset_hermes_home_override(token)

    assert json.loads(handle_pending_subcommand(subsystem, ["pending", "--json"])) == {"count": 2, "records": records}
    assert handle_pending_subcommand(subsystem, ["pending"]) == text
    assert {p: p.read_bytes() for p in (review_home / "pending" / subsystem).glob("*.json")} == pending_files
