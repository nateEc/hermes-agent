"""Tests for shared user prompt input sanitization."""

import pytest

from hermes_cli.input_sanitize import (
    collapse_repeated_input_artifacts,
    sanitize_user_prompt_text,
    strip_leaked_bracketed_paste_wrappers,
)


class TestStripLeakedBracketedPasteWrappers:
    def test_plain_text_unchanged(self):
        assert strip_leaked_bracketed_paste_wrappers("hello world") == "hello world"



    def test_does_not_strip_non_wrapper_bracket_forms_in_normal_text(self):
        text = "literal[200~tag and literal[201~tag should stay"
        assert strip_leaked_bracketed_paste_wrappers(text) == text


class TestCollapseRepeatedInputArtifacts:
    def test_issue_62557_corruption_tail(self):
        prefix = "需要时随时叫我。"
        tail = "[e~[[e" + "~[[e" * 20
        assert collapse_repeated_input_artifacts(prefix + tail) == prefix


    def test_trailing_punctuation_preserved(self):
        assert collapse_repeated_input_artifacts("wait....") == "wait...."


class TestSanitizeUserPromptText:
    def test_combines_wrapper_strip_and_tail_collapse(self):
        prefix = "hello["
        corrupted = prefix + "~[[e" * 8
        assert sanitize_user_prompt_text(corrupted) == "hello"


@pytest.mark.parametrize("text", [
    "12:00~12:15", "08:00~09:30", "12:01~ 12:15", "1:00~2:00",
    "Schedule lunch 12:00~12:15.", "凌晨00:01~ 01:00", "12:00~", "12:01~",
])
def test_time_literals_survive_cleanup_with_and_without_real_paste_wrappers(text):
    """A minute is user data, not a degraded paste marker (#143444)."""
    assert sanitize_user_prompt_text(text) == text
    for start, end in (("\x1b[200~", "\x1b[201~"), ("^[[200~", "^[[201~"),
                       ("[200~", "[201~"), ("00~", "01~")):
        assert sanitize_user_prompt_text(start + text + end) == text
        assert sanitize_user_prompt_text("Note:" + start + text + end) == "Note:" + text
