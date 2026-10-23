"""Named-provider image routing uses the turn's catalog identity (#143347)."""

from copy import deepcopy
import json
from pathlib import Path

import pytest
import hermes_yaml as yaml

from agent import models_dev
from agent.auxiliary_client import scoped_runtime_main
from agent.image_routing import decide_image_input_mode


@pytest.fixture
def catalog_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    # Disk config disagrees with the turn snapshot: a probe must not reload it.
    (home / "config.yaml").write_text(yaml.safe_dump({
        "providers": {"fixture-fast": {"catalog_provider": "deepseek"}},
    }))
    cache = models_dev._get_cache_path()
    cache.parent.mkdir(parents=True, exist_ok=True)
    catalog = {}
    for vendor, modalities in (("openrouter", ["text", "image"]), ("deepseek", ["text"])):
        catalog[vendor] = {"id": vendor, "models": {
            model: {"id": model, "modalities": {"input": modalities}}
            for model in ("fixture/seeing", "fixture/other")
        }}
    cache.write_text(json.dumps(catalog))
    monkeypatch.setattr(models_dev, "_models_dev_cache", {})
    monkeypatch.setattr(models_dev, "_models_dev_cache_time", 0)
    monkeypatch.setattr(models_dev, "_models_dev_retry_after", 0)

    def no_network(**kwargs):
        pytest.fail("Fresh local catalog must not require network access")

    monkeypatch.setattr(models_dev, "_fetch_models_dev_from_network", no_network)
    return {
        "model": {"provider": "fixture-fast", "default": "fixture/seeing",
                  "base_url": "https://fixture.invalid/v1"},
        "providers": {"fixture-fast": {"catalog_provider": "openrouter"}},
    }


@pytest.mark.parametrize("provider,identity,legacy", [
    ("custom", "explicit", False),
    ("custom", "runtime", False),
    ("fixture-fast", "none", False),
    ("custom:fixture-fast", "none", False),
    ("custom", "runtime", True),
])
def test_named_catalog_routes_images_from_the_turn_config(catalog_home, provider, identity, legacy):
    cfg = catalog_home
    if legacy:
        cfg["custom_providers"] = [{"name": "fixture-fast", "catalog_provider": "openrouter"}]
        cfg.pop("providers")
    original = deepcopy(cfg)
    runtime = {"provider": "custom", "model": "fixture/seeing", "requested_provider": "fixture-fast"}
    extra = {"requested_provider": "fixture-fast"} if identity == "explicit" else {}
    with scoped_runtime_main(runtime if identity == "runtime" else {}):
        assert decide_image_input_mode(provider, "fixture/seeing", cfg, **extra) == "native"
    assert cfg == original


def test_runtime_identity_is_exact_scoped_and_cannot_override_a_known_verdict(catalog_home):
    cfg = catalog_home
    runtime = {"provider": "custom", "model": "fixture/seeing", "requested_provider": "fixture-fast"}
    with scoped_runtime_main(runtime):
        assert decide_image_input_mode("custom", "fixture/seeing", cfg) == "native"
        assert decide_image_input_mode("custom", "fixture/other", cfg) == "text"
        assert decide_image_input_mode("fixture-unrelated", "fixture/seeing", cfg) == "text"
        with scoped_runtime_main({**runtime, "requested_provider": "fixture-text"}):
            nested = deepcopy(cfg)
            nested["providers"]["fixture-text"] = {"catalog_provider": "deepseek"}
            assert decide_image_input_mode("custom", "fixture/seeing", nested) == "text"
        assert decide_image_input_mode("custom", "fixture/seeing", cfg) == "native"
        overridden = deepcopy(cfg)
        overridden["model_overrides"] = {"custom": {"fixture/seeing": {"supports_vision": False}}}
        assert decide_image_input_mode("custom", "fixture/seeing", overridden) == "text"
        overridden["model"]["supports_vision"] = True
        assert decide_image_input_mode("custom", "fixture/seeing", overridden) == "native"
