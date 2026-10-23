"""Bounded, profile-scoped worktree maintenance for the multiplexed cron ticker (#143255).

One daemon drains the captured profile contexts. Shared repositories are pruned once per interval,
but profiles with no remaining repo work still reclaim their own orphan install state.
"""

from __future__ import annotations

import contextvars
import logging
import os
import threading
import time
from typing import Callable, Iterable, Optional

logger = logging.getLogger(__name__)

_pruned_lock = threading.Lock()
_last_pruned_at: dict[str, float] = {}
_queue_lock = threading.Lock()
_pending: dict[str, tuple[contextvars.Context, Callable, float]] = {}
_worker_active = False


def enqueue_sweep(home: str, discover: Callable[[], Iterable[str]], *, interval_seconds: float) -> None:
    """Keep at most one pending request per profile without spawning per-profile waiters."""
    global _worker_active
    from agent.memory_provider import spawn_context_thread

    with _queue_lock:
        _pending[home] = (contextvars.copy_context(), discover, interval_seconds)
        if _worker_active:
            return
        _worker_active = True
        try:
            spawn_context_thread(_drain, name="cron-worktree-prune").start()
        except RuntimeError:
            _worker_active = False
            raise


def _drain() -> None:
    global _worker_active
    while True:
        with _queue_lock:
            if not _pending:
                _worker_active = False
                return
            context, discover, interval = _pending.pop(next(iter(_pending)))
        context.run(run_sweep, discover, interval_seconds=interval)


def _repo_key(repo: str) -> str:
    return os.path.normcase(os.path.realpath(str(repo)))


def due_repos(repos: Iterable[str], *, interval_seconds: float, now: Optional[float] = None) -> list[str]:
    """Claim canonical repositories before pruning so shared profile requests cannot repeat it."""
    stamp = time.monotonic() if now is None else now
    due: list[str] = []
    with _pruned_lock:
        for repo in repos:
            key = _repo_key(repo)
            last = _last_pruned_at.get(key)
            if last is not None and stamp - last < interval_seconds:
                continue
            _last_pruned_at[key] = stamp
            due.append(repo)
    return due


def run_sweep(discover: Callable[[], Iterable[str]], *, interval_seconds: float) -> None:
    """Run under the requesting profile's context; hygiene must not break scheduling."""
    try:
        repos = list(discover() or [])
        due = due_repos(repos, interval_seconds=interval_seconds)
        from hermes_cli.worktree_ops import _prune_stale_worktrees, _reclaim_orphan_install_states

        for repo in due:
            try:
                _prune_stale_worktrees(repo)
            except Exception:
                logger.debug("Cron worktree maintenance failed for %s", repo, exc_info=True)
        if repos and not due:
            _reclaim_orphan_install_states()
    except Exception:
        logger.debug("Cron worktree maintenance skipped", exc_info=True)
