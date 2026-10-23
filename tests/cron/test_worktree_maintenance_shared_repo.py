"""Worktree hygiene is bounded per process, while each request retains its profile (#143255)."""

import os
import threading

from cron import scheduler
from cron import scheduler_worktree_maintenance as maintenance
from cron.scheduler_provider import _profile_cron_scope
from hermes_constants import get_hermes_home


def _join_workers():
    for worker in list(threading.enumerate()):
        if worker.name == "cron-worktree-prune":
            worker.join(10)
            assert not worker.is_alive(), "maintenance did not drain"


def test_shared_repos_use_one_worker_without_losing_profile_work(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    homes = [tmp_path / name for name in ("alpha", "beta", "gamma")]
    shared, private = tmp_path / "shared", tmp_path / "private"
    for repo in (shared, private):
        (repo / ".worktrees").mkdir(parents=True)
    starts, discoveries, pruned, reclaimed = [], [], [], []
    thread_type = threading.Thread

    def new_thread(*args, **kwargs):
        worker = thread_type(*args, **kwargs)
        if worker.name == "cron-worktree-prune":
            starts.append(worker)
        return worker

    def discover():
        home = get_hermes_home()
        discoveries.append(home)
        return [str(shared), str(private)] if home == homes[1] else [str(shared) + os.sep + "."]

    def prune(repo):
        pruned.append((os.path.realpath(repo), get_hermes_home()))
        if not entered.is_set():
            entered.set()
            assert release.wait(10)

    monkeypatch.setattr(scheduler, "_last_worktree_maintenance_at", None)
    monkeypatch.setattr(maintenance, "_last_pruned_at", {})
    monkeypatch.setattr(threading, "Thread", new_thread)
    monkeypatch.setattr(scheduler, "_worktree_maintenance_repos", discover)
    monkeypatch.setattr("hermes_cli.worktree_ops._prune_stale_worktrees", prune)
    monkeypatch.setattr("hermes_cli.worktree_ops._reclaim_orphan_install_states",
                        lambda: reclaimed.append(get_hermes_home()))
    try:
        with _profile_cron_scope(homes[0]):
            scheduler._maybe_run_worktree_maintenance()
        assert entered.wait(10)
        for home in homes[1:]:
            with _profile_cron_scope(home):
                scheduler._maybe_run_worktree_maintenance()
                scheduler._maybe_run_worktree_maintenance()  # same profile is still throttled
        assert len(starts) == 1, "a blocked sweep spawned a waiter thread for every profile"
    finally:
        release.set()
        _join_workers()
    assert discoveries == homes
    assert pruned == [(str(shared.resolve()), homes[0]), (str(private.resolve()), homes[1])]
    assert reclaimed == [homes[2]], "deduplicated repo work lost this profile's own cleanup"


def test_maintenance_window_and_failed_discovery_allow_later_work(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / ".worktrees").mkdir(parents=True)
    monkeypatch.setattr(maintenance, "_last_pruned_at", {})
    assert maintenance.due_repos([str(repo)], interval_seconds=10, now=100) == [str(repo)]
    assert maintenance.due_repos([str(repo) + os.sep + "."], interval_seconds=10, now=105) == []
    assert maintenance.due_repos([str(repo)], interval_seconds=10, now=110) == [str(repo)]
    monkeypatch.setattr(maintenance, "_last_pruned_at", {})
    monkeypatch.setattr(scheduler, "_last_worktree_maintenance_at", None)
    discovered, pruned = [], []
    broken, healthy = tmp_path / "broken", tmp_path / "healthy"

    def discover():
        home = get_hermes_home()
        discovered.append(home)
        if home == broken:
            raise OSError("registered repo lookup failed")
        return [str(repo)]

    monkeypatch.setattr(scheduler, "_worktree_maintenance_repos", discover)
    monkeypatch.setattr("hermes_cli.worktree_ops._prune_stale_worktrees",
                        lambda path: pruned.append((path, get_hermes_home())))
    with _profile_cron_scope(broken):
        scheduler._maybe_run_worktree_maintenance()
    _join_workers()
    with _profile_cron_scope(healthy):
        scheduler._maybe_run_worktree_maintenance()
    _join_workers()
    assert discovered == [broken, healthy]
    assert pruned == [(str(repo), healthy)]
