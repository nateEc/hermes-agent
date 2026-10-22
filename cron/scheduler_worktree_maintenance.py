"""Process-wide coordination for the cron tick's worktree prune (``cron.scheduler`` runs in here).

One process ticks every served profile (``cron/AGENTS.md``), so the per-profile throttle in
``cron.scheduler`` legitimately lets each profile decide it is due — but all of those sweeps walk the
SAME repos: the Hermes checkout is in ``_worktree_maintenance_repos()`` for every profile, and a
workdir repo can be shared by several profiles' jobs. N profiles therefore used to fork ``git``
against one repo at the same moment, which starved a 338-profile gateway's event loop until the
liveness watchdog exited 75 (#143255).

Two process-wide guards fix that without dropping a profile's own repos:

- **One sweep at a time.** The tick is sequential, so the sweeps it dispatches are not; serialising
  them keeps the number of concurrent ``git`` children at one regardless of profile count. A waiting
  sweep holds no resources, so the wait is capped rather than unbounded.
- **One prune per repo per interval.** Pruning is repo-scoped (same trees, same pruner), so the
  second profile to reach a shared repo within the interval has nothing to do. Profile-scoped work
  still runs: every profile's own repos are pruned, and ``_reclaim_orphan_install_states`` follows
  whichever repos that sweep prunes.

Cost of the second guard: a profile whose ONLY repo was just pruned by another profile skips its own
``_reclaim_orphan_install_states`` pass for that interval; the next sweep picks it up.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable, Iterable, Optional

logger = logging.getLogger(__name__)

# Bounded so a wedged sweep cannot park a profile's tick forever; a normal sweep is one git pass over
# a handful of repos.
SWEEP_WAIT_SECONDS = 120.0

_sweep_slot = threading.Semaphore(1)
_pruned_lock = threading.Lock()
_last_pruned_at: dict[str, float] = {}  # canonical repo path -> monotonic stamp


def _repo_key(repo: str) -> str:
    """Dedupe on the path ``git`` resolves to, so ``/x/repo`` and ``/x/repo/.`` are one repo."""
    return os.path.normcase(os.path.realpath(str(repo)))


def claim_sweep_slot(wait_seconds: Optional[float] = None) -> bool:
    """Take the process-wide prune slot, waiting for the sweep ahead of us (bounded)."""
    return _sweep_slot.acquire(timeout=SWEEP_WAIT_SECONDS if wait_seconds is None else wait_seconds)


def release_sweep_slot() -> None:
    _sweep_slot.release()


def due_repos(repos: Iterable[str], *, interval_seconds: float, now: Optional[float] = None) -> list[str]:
    """Claim the repos this process has not pruned within ``interval_seconds``.

    The claim is taken before the prune runs, so a sweep that is still working on a repo keeps its
    claim if a later sweep looks at the same one.
    """
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


def run_sweep(discover: Callable[[], Iterable[str]], *, interval_seconds: float,
              wait_seconds: Optional[float] = None) -> None:
    """Prune the repos ``discover()`` reports that are due, from the cron tick's daemon thread.

    ``discover`` stays in the caller so a per-profile repo lookup (and anything a test patches) is
    read where it is defined. Errors never propagate: GC is hygiene, not scheduling.
    """
    if not claim_sweep_slot(wait_seconds):
        logger.debug("Cron worktree maintenance skipped: another sweep is still running")
        return
    try:
        repos = due_repos(discover() or [], interval_seconds=interval_seconds)
        if not repos:
            return
        from hermes_cli.worktree_ops import _prune_stale_worktrees

        for repo in repos:
            try:
                _prune_stale_worktrees(repo)
            except Exception:
                logger.debug("Cron worktree maintenance failed for %s", repo, exc_info=True)
    except Exception:
        logger.debug("Cron worktree maintenance skipped", exc_info=True)
    finally:
        release_sweep_slot()


def reset_state() -> None:
    """Drop the per-repo stamps (tests; a fresh process starts empty anyway)."""
    with _pruned_lock:
        _last_pruned_at.clear()
