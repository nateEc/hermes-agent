"""`hermes cron run` must refuse while the global emergency stop is engaged (#142820).

``tick()`` returns 0 before it even scans due jobs, so no *scheduled* fire starts
under `hermes pause`. The manual path was the hole: ``cronjob(action="run")`` —
which is exactly what ``hermes cron run`` calls — claimed the job and fired it
immediately, with no ``agent.estop`` check anywhere in the chain. A manual run
during an emergency stop starts an agent and spends tokens on an operator who
just asked for new work to hold.

In-flight work is never touched: only a run that has not been claimed yet is
refused, and the job is left exactly as it was.
"""

import json
from unittest.mock import patch

import pytest

from agent import estop
from tools.cronjob_tools import cronjob

_JOB = {"id": "job-estop-1", "name": "nightly", "prompt": "hi",
        "schedule": {"kind": "cron", "expr": "0 9 * * *"}}


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """Pin both candidate sentinel paths (profile home + fleet root) at tmp_path."""
    monkeypatch.setattr(estop, "sentinel_path", lambda: tmp_path / "ESTOP")
    monkeypatch.setattr(estop, "_canonical_root", lambda: tmp_path)
    estop._logged_components.clear()
    return tmp_path


def _manual_run(action="run"):
    """Drive the tool's run action with the claim + fire bodies under mocks."""
    claimed = {**_JOB, "fire_claim": {"by": "manual-owner"}}
    with patch("tools.cronjob_tools.resolve_job_ref", return_value=dict(_JOB)), \
         patch("tools.cronjob_tools.claim_job_for_fire", return_value=claimed) as m_claim, \
         patch("cron.scheduler.run_one_job", return_value=True) as m_run, \
         patch("tools.cronjob_tools.get_job", return_value=dict(_JOB)):
        out = json.loads(cronjob(action=action, job_id="job-estop-1"))
    return out, m_claim, m_run


def test_manual_run_refused_while_engaged(hermes_home):
    estop.engage(reason="deploy window")

    out, m_claim, m_run = _manual_run()

    assert out["success"] is False
    assert "emergency stop" in out["error"].lower()
    # The operator needs the way out, not just the refusal.
    assert "hermes resume" in out["error"]
    assert str(hermes_home / "ESTOP") in out["error"]
    m_claim.assert_not_called()
    m_run.assert_not_called()


@pytest.mark.parametrize("action", ["run", "run_now", "trigger"])
def test_every_run_alias_is_refused(action, hermes_home):
    """`run`, `run_now` and `trigger` are one handler: none may leak a fire."""
    estop.engage()

    out, m_claim, m_run = _manual_run(action=action)

    assert out["success"] is False
    m_claim.assert_not_called()
    m_run.assert_not_called()


def test_corrupt_sentinel_still_refuses(hermes_home):
    """A hand-touched/empty ESTOP file is engaged too (fail safe, as in tick())."""
    (hermes_home / "ESTOP").write_text("not json", encoding="utf-8")

    out, m_claim, _ = _manual_run()

    assert out["success"] is False
    m_claim.assert_not_called()


def test_manual_run_proceeds_after_resume(hermes_home):
    """Control: with the sentinel gone the claim and fire happen exactly as before."""
    estop.engage()
    estop.disengage()

    out, m_claim, m_run = _manual_run()

    assert out["success"] is True
    assert out["job"]["executed"] is True
    m_claim.assert_called_once_with("job-estop-1", manual=True, return_job=True)
    m_run.assert_called_once()
