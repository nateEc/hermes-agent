"""Manual-run admission must honor real profile/fleet ESTOP files before touching a cron claim."""

import json
from functools import partial
from pathlib import Path

import pytest

from agent import estop
from cron import jobs
from tools import cronjob_tools


@pytest.fixture
def manual_job(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    home = root / "profiles" / "worker"
    home.mkdir(parents=True)
    (root / "config.yaml").write_text("{}", encoding="utf-8")
    (home / "config.yaml").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    estop._logged_components.clear()
    assert set(estop._candidate_sentinel_paths()) == {home / "ESTOP", root / "ESTOP"}
    events = []

    def observe(name, *_args, **_kwargs):
        events.append(name)

    for name in ("_hand_off_primary_routed_run", "_try_dispatch_background_run", "_forward_relay_fronted_run"):
        monkeypatch.setattr(cronjob_tools, name, partial(observe, name))

    def run_claimed(job, **_kwargs):
        events.append(("run", job))
        return {"claimed": True, "success": True}

    monkeypatch.setattr(cronjob_tools, "_run_claimed_job", run_claimed)
    monkeypatch.setattr(cronjob_tools, "_notify_provider_jobs_changed_safe", partial(observe, "notify"))
    with jobs.use_cron_store(home):
        job = jobs.create_job(prompt="Summarize local notes", schedule="every 1h", deliver="local")
        yield home, root, job, events


@pytest.mark.parametrize("action", ["run", "run_now", "trigger"])
@pytest.mark.parametrize("location", ["profile", "fleet"])
@pytest.mark.parametrize("body", ["", "not json", '{"reason": "maintenance"}'])
def test_estop_refuses_all_manual_aliases_before_any_dispatch_or_store_change(manual_job, action, location, body):
    home, root, job, events = manual_job
    sentinel = (home if location == "profile" else root) / "ESTOP"
    sentinel.write_text(body, encoding="utf-8")
    before = jobs.get_job(job["id"])
    store_before = (home / "cron" / "jobs.json").read_bytes()
    output = json.loads(cronjob_tools.cronjob(action=action, job_id=job["id"]))
    assert output["success"] is False
    assert "emergency stop" in output["error"].lower() and "hermes resume" in output["error"]
    assert str(home / "ESTOP") in output["error"]
    assert events == []
    assert jobs.get_job(job["id"]) == before
    assert (home / "cron" / "jobs.json").read_bytes() == store_before


@pytest.mark.parametrize("action", ["run", "run_now", "trigger"])
@pytest.mark.parametrize("resumed", [False, True])
def test_unpaused_or_resumed_manual_runs_still_claim_through_the_real_store(manual_job, action, resumed):
    _home, root, job, events = manual_job
    if resumed:
        estop.engage(reason="maintenance")
        (root / "ESTOP").write_text("", encoding="utf-8")
        assert estop.disengage()
    assert not estop.is_engaged()
    output = json.loads(cronjob_tools.cronjob(action=action, job_id=job["id"]))
    assert output["success"] is True and output["job"]["executed"] is True
    run = [event[1] for event in events if isinstance(event, tuple)]
    assert len(run) == 1 and run[0]["id"] == job["id"]
    stored = jobs.get_job(job["id"])
    assert stored["fire_claim"] == run[0]["fire_claim"]
    assert stored["next_run_at"] != job["next_run_at"] or stored.get("manual_run_at")
