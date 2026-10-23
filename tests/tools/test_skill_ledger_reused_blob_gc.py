"""Recapturing an old orphan renews the same GC grace as storing fresh content."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import threading
import time

import pytest

from hermes_cli.config import atomic_config_write
from tools import skill_ledger as ledger
from tools import skill_manager_tool
from tools.registry import registry


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "skills").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for name in ("demo", "other"):
        root = home / "skills" / name
        root.mkdir()
        (root / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Use when testing safe skill edits.\n---\n# Test\nOriginal body.\n",
            encoding="utf-8",
        )
    return home


def _call(name, action, *, batch=False, **args):
    op = {"name": name, "action": action, **args}
    return json.loads(registry.dispatch("skill_manage", {"operations": [op]} if batch else op))


def _age(blob):
    past = time.time() - 2 * ledger._BLOB_GC_GRACE_SECS
    os.utime(blob, (past, past))


@pytest.mark.parametrize("batch", [False, True], ids=["flat", "batch"])
@pytest.mark.parametrize("automatic", [False, True], ids=["explicit-gc", "automatic-maintenance"])
def test_edit_reusing_old_orphan_keeps_a_working_rollback(home, monkeypatch, batch, automatic):
    skill = home / "skills" / "demo" / "SKILL.md"
    original = skill.read_bytes()
    # A supporting-file edit captures SKILL.md, but delta storage does not reference it.
    assert _call("demo", "write_file", file_path="references/helper.txt", file_content="helper")["success"]
    digest = hashlib.sha256(original).hexdigest()
    blob = ledger.blobs_dir() / digest
    assert blob.is_file()
    assert all(i["sha256"] != digest for row in ledger.list_entries() for i in row["before"] + row["after"])
    _age(blob)
    garbage = ledger.blobs_dir() / ledger._store_blob(b"truly unused old content")
    _age(garbage)
    captured, finish_edit = threading.Event(), threading.Event()
    handler = skill_manager_tool._ACTION_HANDLERS["patch"]

    def pause_after_capture(args):
        captured.set()
        assert finish_edit.wait(15), "collector did not release the edit"
        return handler(args)

    monkeypatch.setitem(skill_manager_tool._ACTION_HANDLERS, "patch", pause_after_capture)
    with ThreadPoolExecutor(max_workers=1) as pool:
        edit = pool.submit(_call, "demo", "patch", batch=batch,
                           old_string="Original body.", new_string="Updated body.")
        try:
            assert captured.wait(15), "edit never reached its captured-before boundary"
            if automatic:
                atomic_config_write(home / "config.yaml", {"skills": {"ledger_max_bytes": 1}})
                assert _call("other", "write_file", file_path="references/other.txt", file_content="other")["success"]
                atomic_config_write(home / "config.yaml", {"skills": {"ledger_max_bytes": 0}})
            else:
                ledger.gc_blobs()
            assert not garbage.exists(), "the collector must still remove actual old garbage"
        finally:
            finish_edit.set()
        result = edit.result(timeout=15)
    assert result["success"], result
    entry = next(row for row in ledger.list_entries(skill="demo") if row["action"] == "patch")
    ok, message = ledger.rollback_entry(entry["id"])
    assert ok, message
    assert skill.read_bytes() == original


def test_capture_and_gc_cannot_disagree_after_collector_observed_old_mtime(home, monkeypatch):
    """Force the stat/touch/unlink race: touching without the ledger lock is not sufficient."""
    data = b"old unreferenced content reused by a new capture"
    digest = ledger._store_blob(data)
    blob = ledger.blobs_dir() / digest
    _age(blob)
    ledger.append_entry("create", "seed")
    observed, capture_boundary = threading.Event(), threading.Event()
    real_unlink, real_lock = Path.unlink, ledger._ledger_lock

    def paused_unlink(path, *args, **kwargs):
        if threading.current_thread().name == "collector" and path == blob:
            observed.set()  # GC has already stat'ed it and committed to deleting the old orphan.
            assert capture_boundary.wait(15), "capture did not reach its storage boundary"
        return real_unlink(path, *args, **kwargs)

    @contextmanager
    def capture_lock():
        if threading.current_thread().name == "capture":
            capture_boundary.set()
        with real_lock():
            yield

    def collect():
        threading.current_thread().name = "collector"
        return ledger.gc_blobs()

    def capture():
        threading.current_thread().name = "capture"
        try:
            return ledger._store_blob(data)
        finally:
            capture_boundary.set()  # unfixed _store_blob has no lock acquisition to signal.

    monkeypatch.setattr(Path, "unlink", paused_unlink)
    monkeypatch.setattr(ledger, "_ledger_lock", capture_lock)
    with ThreadPoolExecutor(max_workers=2) as pool:
        collector = pool.submit(collect)
        try:
            assert observed.wait(15), "collector never observed the old blob"
            recapture = pool.submit(capture)
            assert recapture.result(timeout=15) == digest
            collector.result(timeout=15)
        finally:
            capture_boundary.set()
    assert ledger.read_blob(digest) == data
