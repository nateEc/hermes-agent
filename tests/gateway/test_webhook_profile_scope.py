"""Authenticated webhook scripts run with their owner's profile, not ambient state (#143233)."""

import hashlib
import hmac
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from agent import secret_scope as secrets
from gateway.config import GatewayConfig, PlatformConfig
from gateway.platforms.webhook import WebhookAdapter
from gateway.run import _profile_runtime_scope
from hermes_constants import get_hermes_home
from tools import env_passthrough
from tui_gateway import launch_profile_policy


@pytest.fixture
def profile_homes(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    worker = home / "profiles" / "worker"
    for path, value in ((home, "from-default"), (worker, "from-worker")):
        (path / "scripts").mkdir(parents=True)
        (path / "config.yaml").write_text("{}\n")
        (path / ".env").write_text(f"NOTION_TOKEN={value}\n")
        (path / "scripts" / "probe.py").write_text(
            "import json, os, sys\nfrom pathlib import Path\n"
            "payload = json.load(sys.stdin)\n"
            "Path(__file__).with_suffix('.receipt.json').write_text(json.dumps({\n"
            "  'credential_matches': os.environ.get('NOTION_TOKEN') == payload['expected']}))\n"
            "print('[SILENT]')\n")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("NOTION_TOKEN", "from-process")
    monkeypatch.setattr(launch_profile_policy, "_snapshot", None)
    launch_profile_policy.capture_launch_env()
    monkeypatch.setattr(env_passthrough, "_allowed_env_vars", set())
    env_passthrough.register_env_passthrough(["NOTION_TOKEN"])
    return home, worker


@pytest.mark.asyncio
@pytest.mark.parametrize(("multiplex", "path", "env_only"), [
    (False, "/webhooks/default", False),
    (True, "/webhooks/default", False),
    (True, "/p/default/webhooks/default", False),
    (True, "/p/worker/webhooks/worker", False),
    (True, "/webhooks/default", True),
])
async def test_signed_delivery_executes_the_real_script_with_its_own_credential(
        profile_homes, multiplex, path, env_only):
    home, worker = profile_homes
    if env_only:
        (home / ".env").write_text("")
    signing_key = "signed-webhook-fixture"
    routes = {"default": {"secret": signing_key, "script": "probe.py"},
              "worker": {"secret": signing_key, "script": "probe.py", "profile": "worker"}}
    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={"routes": routes, "host": "127.0.0.1"}))
    adapter.gateway_runner = SimpleNamespace(config=GatewayConfig(multiplex_profiles=multiplex))
    app = web.Application()
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    app.router.add_post("/p/{profile}/webhooks/{route_name}", adapter._handle_webhook)
    owner = worker if "/worker/" in path else home
    expected = "from-process" if env_only or not multiplex else (
        "from-worker" if owner == worker else "from-default")
    body = json.dumps({"expected": expected}).encode()
    signature = "sha256=" + hmac.new(signing_key.encode(), body, hashlib.sha256).hexdigest()
    token = secrets.set_multiplex_context(multiplex)
    try:
        async with TestClient(TestServer(app)) as client:
            response = await client.post(path, data=body, headers={"X-Hub-Signature-256": signature})
            assert response.status == 200
            assert (await response.json())["reason"] == "script"  # intentional veto AFTER the side effect
    finally:
        secrets.reset_multiplex_context(token)
    receipt = owner / "scripts" / "probe.receipt.json"
    assert receipt.is_file(), "the authenticated script was silently skipped before spawning"
    assert json.loads(receipt.read_text()) == {"credential_matches": True}
    other = home if owner == worker else worker
    assert not (other / "scripts" / "probe.receipt.json").exists()


def test_bare_scope_ignores_a_foreign_ambient_profile_and_restores_it(profile_homes):
    home, worker = profile_homes
    token = secrets.set_multiplex_context(True)
    try:
        with _profile_runtime_scope(worker):
            with WebhookAdapter._profile_scope(None):
                assert get_hermes_home() == home
                assert secrets.get_secret("NOTION_TOKEN") == "from-default"
            assert get_hermes_home() == worker
            assert secrets.get_secret("NOTION_TOKEN") == "from-worker"
    finally:
        secrets.reset_multiplex_context(token)
