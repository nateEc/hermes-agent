"""Token delimiters must not disable redaction, including profile-export and plugin consumers."""

import json

import pytest

from agent import redact
from hermes_cli.profiles import _scrub_export_secrets


@pytest.fixture(autouse=True)
def clean_registry():
    redact._reset_plugin_redaction_patterns()
    yield
    redact._reset_plugin_redaction_patterns()


@pytest.mark.parametrize("token,tail", [
    ("ghp_" + "A" * 40, "_SUFFIX"),
    ("ghp_" + "A" * 40, "-backup"),
    ("hf_" + "C" * 20, "_KEY"),
    ("npm_" + "D" * 36, "-item"),
    ("xoxb-" + "E" * 24, "_SUFFIX"),
    ("AKIA" + "F" * 16, "_TAIL"),
    ("rk_live_" + "G" * 24, "_limited"),
    ("gsk_" + "H" * 56, "-prod"),
    ("github_pat_" + "I" * 36, "-env"),
    ("glpat-" + "Z" * 24, ".json"),
])
@pytest.mark.parametrize("leading", ["", "/tmp/backup-"])
def test_delimited_vendor_tokens_are_masked_in_text_and_staged_exports(tmp_path, token, tail, leading):
    text = leading + token + tail
    for options in ({"force": True}, {"force": True, "code_file": True}, {"force": True, "file_read": True}):
        output = redact.redact_sensitive_text(text, **options)
        assert token not in output
        assert output.endswith(tail)
    assert token not in redact.redact_for_egress(text)
    original = json.dumps({"entry": text, "label": "ordinary text", "count": 7})
    staged = tmp_path / "state.json"
    staged.write_text(original, encoding="utf-8")
    _scrub_export_secrets(tmp_path)
    exported = json.loads(staged.read_text(encoding="utf-8"))
    assert token not in exported["entry"]
    assert exported["entry"].endswith(tail)
    assert exported["label"] == "ordinary text" and exported["count"] == 7
    for benign in ("x" + token, "_" + token, "ghp_tooshort_x"):
        assert redact.redact_sensitive_text(benign, force=True) == benign


@pytest.mark.parametrize("pattern,token,tail", [
    (r"zzkey-[A-Za-z0-9]{10,}", "zzkey-" + "K" * 16, "_RETRY"),
    (r"zzkey-[A-Z\]]{10,}", "zzkey-" + "A]" * 8, "-RETRY"),
    (r"zzkey-\w{10,}", "zzkey-" + "K" * 16, "-RETRY"),
    (r"zzkey-\d{16}", "zzkey-" + "4" * 16, "_RETRY"),
    (r"zzkey-(?:[A-Z]{16}|[0-9]{16})", "zzkey-" + "4" * 16, "_RETRY"),
    (r"zzkey-[A-Z.]{16}", "zzkey-" + "A" * 16, ".json"),
    (r"zzkey-[A-Z/]{16}", "zzkey-" + "A" * 16, "/path"),
])
def test_registered_formats_share_delimiter_and_identifier_contracts(pattern, token, tail):
    assert redact.register_redaction_patterns([pattern], source="test") == 1
    for leading in ("", "backup-"):
        output = redact.redact_sensitive_text(leading + token + tail, force=True)
        assert token not in output
        assert output.endswith(tail)
    for leading in ("x", "_"):
        text = leading + token
        assert redact.redact_sensitive_text(text, force=True) == text
