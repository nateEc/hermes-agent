"""Memory-provider handoffs retain masking semantics without parser-sized temporary allocation."""

import re
import tracemalloc

import pytest

from agent.memory_manager import _redact_for_provider
from agent.redact import redact_for_egress


@pytest.mark.parametrize("local_redaction", [False, True])
def test_large_credential_handoff_masks_echoes_with_bounded_temporary_allocation(monkeypatch, local_redaction):
    monkeypatch.setattr("agent.redact._REDACT_ENABLED", local_redaction)
    secret = "aB09xyZ7" * 8192
    payload = {"tool": "Authorization: Bearer " + secret, "echo": ["echo " + secret]}
    assert redact_for_egress(secret) == secret
    redact_for_egress("Authorization: Bearer synthetic-warmup-token-12345")
    re.purge()
    tracing = tracemalloc.is_tracing()
    if not tracing:
        tracemalloc.start()
    before = tracemalloc.get_traced_memory()[0]
    tracemalloc.reset_peak()
    try:
        output = _redact_for_provider(payload)
        extra_peak = tracemalloc.get_traced_memory()[1] - before
    finally:
        if not tracing:
            tracemalloc.stop()
    assert secret not in repr(output)
    assert payload["echo"][0] == "echo " + secret
    # Generous relative budget: linear string work fits; a regex parser node per byte does not.
    assert extra_peak < len(secret) * 16, "literal masking allocated a parser-sized temporary tree"


@pytest.mark.parametrize("text,literals", [
    ("xxabcdefghijklmnopqrstuvXYZyy", ["abcdefghijklmnopqrstuv", "abcdefghijklmnopqrstuvXYZ"]),
    ("abcdefghijklmnopqrstuvXYZ", ["abcdefghijklmnopqrstuvXYZ", "abcdefghijklmnopqrstuv", "cdefghijklmnopqrstuvXYZ"]),
    ("abcdefghijklmnopqrstuvXXcdefghijklmnopqrstuv", ["cdefghijklmnopqrstuv", "abcdefghijklmnopqrstuv"]),
    ("abcdefghijklmnopqrstuv" * 3, ["abcdefghijklmnopqrstuv"]),
    ("nothing to mask", ["abcdefghijklmnopqrstuv"]),
    ("abc.def+ghi/jkl_mno-pqr", ["abc.def+ghi/jkl_mno-pqr", "def+ghi/jkl_mno-pqr"]),
    ("left abcdefghijklmnopqrstuv right", ["abcdefghijklmnopqrstuv", "abcdefghijklmnopqrstuv"]),
    (("primarySecretToken123456 " * 2048), ["primarySecretToken123456"] +
     [f"missingSecretToken{index:04d}" for index in range(256)]),
])
def test_handoff_preserves_literal_overlap_input_ownership_and_media_passthrough(text, literals):
    sources = tuple("Authorization: Bearer " + value for value in literals)
    marker = "data:image/png;base64," + literals[0]
    unchanged = {"label": "ordinary clean text", "count": 7}
    echo = {"nested": ([text], marker), "stable": unchanged}
    output = _redact_for_provider(sources, echo)
    scrubbed = redact_for_egress(text)
    pattern = re.compile("|".join(re.escape(value) for value in sorted(set(literals), key=len, reverse=True)))
    assert output[1]["nested"][0][0] == pattern.sub("[redacted]", scrubbed)
    assert output[1]["nested"][1] is marker
    assert output[1]["stable"] is unchanged
    assert echo["nested"][0][0] == text
    if scrubbed == text and not pattern.search(text):
        assert output[1] is echo
    assert _redact_for_provider(text) is text  # no learned credential state crosses handoffs
