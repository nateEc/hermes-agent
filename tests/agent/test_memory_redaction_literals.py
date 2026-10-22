"""The learned-literal mask is a scan, not a regex compiled out of the literals (#143111).

The first egress scrub learns the values it removed and the same hand-off masks any bare echo of them.
That second pass used to escape the literals into one alternation and ``re.compile`` it, so a value the
size of a transcript's credential cost a parser node per byte. These rows pin the replacement: nothing
proportional to a literal is compiled, and the masked text is the one the alternation produced.
"""
import re

import pytest

from agent import memory_manager
from agent.memory_manager import _redact_for_provider

# Generated, not a credential: 4,096 characters of an opaque value, the shape of the report's 64 KiB case.
_SECRET = "aB09xyZ7" * 512


class _CompileRecorder:
    """Stands in for ``re`` inside ``agent.memory_manager`` and records what it compiles."""

    def __init__(self):
        self.patterns = []

    def compile(self, pattern, *args, **kwargs):
        self.patterns.append(str(pattern))
        return re.compile(pattern, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(re, name)


def _alternation_mask(text, literals):
    """The second pass as it was before this change, kept as the reference for the equivalence rows."""
    ordered = sorted(literals, key=len, reverse=True)
    pattern = re.compile("|".join(re.escape(literal) for literal in ordered))
    return pattern.sub("[redacted]", text) if pattern.search(text) else text


def test_a_long_learned_literal_is_masked_without_compiling_an_alternation(monkeypatch):
    recorder = _CompileRecorder()
    monkeypatch.setattr(memory_manager, "re", recorder)
    tool = "Authorization: Bearer " + _SECRET
    payload = {"tool": tool, "echo": ["echo " + _SECRET]}

    out = _redact_for_provider(payload)

    assert _SECRET not in repr(out)  # the bare echo of a value the scrub removed is still masked
    assert payload["echo"][0] == "echo " + _SECRET  # the caller keeps its own copy
    assert not recorder.patterns, "compiled patterns: %r" % [p[:80] for p in recorder.patterns]


@pytest.mark.parametrize("text,literals", [
    ("xxabcdefyy", ["abcd", "abcdef"]),          # the longer literal wins at the same position
    ("abcdef", ["abcdef", "abcd", "cdef"]),      # and only that one is consumed
    ("ab-ab-ab", ["ab"]),                        # every occurrence, not just the first
    ("tail-secret", ["secret"]),                 # a match that ends the text
    ("secretsecret", ["secret"]),                # adjacent matches
    ("abcXXbc", ["bc", "abc"]),                  # leftmost wins over a longer one further right
    ("naïve-ñ", ["ïve", "ñ"]),                   # non-ASCII literals
    ("nothing here", ["absent"]),                # no match at all
])
def test_masking_matches_the_alternation_it_replaced(text, literals):
    ordered = sorted(literals, key=len, reverse=True)
    assert memory_manager._mask_learned_literals(text, ordered) == _alternation_mask(text, literals)


def test_a_text_without_a_learned_literal_comes_back_as_itself():
    text = "nothing to mask here"
    assert memory_manager._mask_learned_literals(text, ["absent", ""]) is text  # identity, and an empty literal cannot loop
