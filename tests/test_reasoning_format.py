"""Tests for reasoning_format.py.

The heuristic itself has no Rocket.Chat/Hermes/model awareness - the pattern
and label are always supplied by the caller (see ROCKETCHAT_REASONING_LEAK_*
env vars in adapter.py). These tests exercise that contract directly with
explicit patterns/labels, so they stay valid no matter which harness or model
backend eventually calls this module in production. Uses the standard-library
``unittest`` (no pytest/requirements.txt needed), runnable from anywhere:

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import sys
import unittest

# The tested modules load as bare modules (not as a package).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reasoning_format import DEFAULT_REASONING_LEAD_PATTERN, wrap_leaked_reasoning

LABEL = "💭 _thinking_"


class WrapLeakedReasoningTests(unittest.TestCase):
    def test_wraps_leaked_preamble_separated_by_a_blank_line(self):
        content = "Based on the project history, this looks like a duplicate.\n\nYes, this is a duplicate."
        result = wrap_leaked_reasoning(
            content, pattern=DEFAULT_REASONING_LEAD_PATTERN, label=LABEL, mode="wrap"
        )
        self.assertTrue(result.startswith(f"> {LABEL}"))
        self.assertIn("> Based on the project history, this looks like a duplicate.", result)
        self.assertTrue(result.endswith("Yes, this is a duplicate."))

    def test_falls_back_to_single_newline_when_no_blank_line_present(self):
        content = "Let me check the project list.\nThe available projects: A, B, C."
        result = wrap_leaked_reasoning(
            content, pattern=DEFAULT_REASONING_LEAD_PATTERN, label=LABEL, mode="wrap"
        )
        self.assertTrue(result.startswith(f"> {LABEL}"))
        self.assertIn("> Let me check the project list.", result)
        self.assertTrue(result.endswith("The available projects: A, B, C."))

    def test_matches_hungarian_lead_in_phrases_too(self):
        content = "The user wants a status list.\n\nHere is the list: open, closed."
        result = wrap_leaked_reasoning(
            content, pattern=DEFAULT_REASONING_LEAD_PATTERN, label=LABEL, mode="wrap"
        )
        self.assertTrue(result.startswith(f"> {LABEL}"))

    def test_leaves_non_matching_content_completely_untouched(self):
        content = "Hi! How can I help today?"
        result = wrap_leaked_reasoning(
            content, pattern=DEFAULT_REASONING_LEAD_PATTERN, label=LABEL, mode="wrap"
        )
        self.assertEqual(result, content)

    def test_single_paragraph_content_is_never_wrapped(self):
        # Matches the lead-in pattern, but there is no second paragraph to
        # treat as "the real answer" - wrapping would just eat the whole reply.
        content = "Let me think about this for a second."
        result = wrap_leaked_reasoning(
            content, pattern=DEFAULT_REASONING_LEAD_PATTERN, label=LABEL, mode="wrap"
        )
        self.assertEqual(result, content)

    def test_documented_false_positive_a_normal_based_on_answer_gets_wrapped(self):
        # This is the known, documented risk called out in the module's own
        # docstring: an ordinary answer that happens to start with "Based
        # on ..." is indistinguishable from a leaked reasoning preamble with
        # the default pattern. Asserting the current (imperfect) behavior
        # here is a deliberate regression guard, not an endorsement - if this
        # test starts failing, the pattern's precision has changed and the
        # callers' docs/comments about this risk need to be revisited too.
        content = "Based on the current sprint data, three items are overdue.\n\nDetails below."
        result = wrap_leaked_reasoning(
            content, pattern=DEFAULT_REASONING_LEAD_PATTERN, label=LABEL, mode="wrap"
        )
        self.assertTrue(result.startswith(f"> {LABEL}"))

    def test_consumes_a_second_matching_paragraph_too(self):
        # The leak spanned two paragraphs, the second a short "about to answer"
        # meta-remark that matches the pattern directly; a single-paragraph wrap left it
        # leaking as plain text before the real answer.
        content = (
            "The user wants a short answer.\n\n"
            "I should summarise this in one or two sentences.\n\n"
            "This is the real answer."
        )
        result = wrap_leaked_reasoning(
            content, pattern=DEFAULT_REASONING_LEAD_PATTERN, label=LABEL, mode="wrap"
        )
        self.assertTrue(result.startswith(f"> {LABEL}"))
        self.assertIn("> I should summarise this in one or two sentences.", result)
        self.assertTrue(result.endswith("This is the real answer."))
        self.assertNotIn("\n\nI should", result)

    def test_bridges_a_single_non_matching_paragraph_between_two_matching_ones(self):
        # A plain transcript-recap paragraph (matches nothing) sandwiched between two
        # reasoning paragraphs that DO match: it should still be absorbed into the same
        # quote, since the paragraph right after it confirms the reasoning continues.
        content = (
            "The user is asking for a brief summary.\n\n"
            "Looking at the transcript, the key points were: 1. First "
            "point. 2. Second point.\n\n"
            "I should provide a brief summary in Hungarian.\n\n"
            "This is the real answer."
        )
        result = wrap_leaked_reasoning(
            content, pattern=DEFAULT_REASONING_LEAD_PATTERN, label=LABEL, mode="wrap"
        )
        self.assertTrue(result.startswith(f"> {LABEL}"))
        self.assertIn("> Looking at the transcript", result)
        self.assertIn("> I should provide a brief summary in Hungarian.", result)
        self.assertTrue(result.endswith("This is the real answer."))

    def test_never_swallows_the_final_paragraph_even_if_it_would_match(self):
        # The real answer happening to start with a phrase that resembles
        # the lead pattern must never be eaten - there must always be a
        # "rest" left over.
        content = "The user is asking a question.\n\nThe message is the final answer text."
        result = wrap_leaked_reasoning(
            content, pattern=DEFAULT_REASONING_LEAD_PATTERN, label=LABEL, mode="wrap"
        )
        self.assertTrue(result.endswith("The message is the final answer text."))

    def test_two_consecutive_non_matching_paragraphs_stop_consumption(self):
        # The bridge tolerance is for exactly ONE gap paragraph - two in a
        # row correctly signal "we have reached the real, multi-paragraph
        # answer", not more leaked reasoning.
        content = (
            "The user is asking a question.\n\n"
            "This is the first answer paragraph.\n\n"
            "This is the second answer paragraph."
        )
        result = wrap_leaked_reasoning(
            content, pattern=DEFAULT_REASONING_LEAD_PATTERN, label=LABEL, mode="wrap"
        )
        self.assertTrue(result.startswith(f"> {LABEL}"))
        self.assertTrue(
            result.endswith(
                "This is the first answer paragraph.\n\nThis is the second answer paragraph."
            )
        )

    def test_custom_pattern_and_label_are_honored_over_the_default(self):
        # Proves the heuristic is genuinely backend-agnostic: a caller wired
        # up for a different model's leak style supplies its own pattern and
        # label, and the module must use exactly that, not the default.
        content = "MODEL_SCRATCHPAD: computing answer.\n\nDone."
        result = wrap_leaked_reasoning(
            content, pattern=r"^MODEL_SCRATCHPAD", label="🛠️ _scratchpad_", mode="wrap"
        )
        self.assertTrue(result.startswith("> 🛠️ _scratchpad_"))
        self.assertIn("> MODEL_SCRATCHPAD: computing answer.", result)

        # And the same content is left untouched under the default pattern,
        # since it doesn't match any of the default lead-in phrases.
        untouched = wrap_leaked_reasoning(
            content, pattern=DEFAULT_REASONING_LEAD_PATTERN, label=LABEL, mode="wrap"
        )
        self.assertEqual(untouched, content)


class DropModeTests(unittest.TestCase):
    """mode="drop" (this deployment's default) removes the detected leaked
    reasoning entirely, keeping only the real answer - same detection /
    consumption boundary as wrap mode, just a different render."""

    def _drop(self, content, pattern=DEFAULT_REASONING_LEAD_PATTERN, label=LABEL):
        return wrap_leaked_reasoning(content, pattern=pattern, label=label, mode="drop")

    def test_drops_the_preamble_keeps_only_the_answer(self):
        content = "Based on the project history, this looks like a duplicate.\n\nYes, this is a duplicate."
        self.assertEqual(self._drop(content), "Yes, this is a duplicate.")

    def test_drops_a_multi_paragraph_leak(self):
        content = (
            "The user wants a short answer.\n\n"
            "I should summarise this in one or two sentences.\n\n"
            "This is the real answer."
        )
        self.assertEqual(self._drop(content), "This is the real answer.")
        self.assertNotIn("thinking", self._drop(content))
        self.assertNotIn("user", self._drop(content))

    def test_no_match_is_untouched(self):
        content = "Hi! How can I help you?"
        self.assertEqual(self._drop(content), content)

    def test_entirely_reasoning_no_answer_is_left_untouched(self):
        # There is no "rest" to keep - dropping would post nothing, so the
        # guard returns the content unchanged (same as wrap mode).
        content = "Let me think about this for a second."
        self.assertEqual(self._drop(content), content)

    def test_default_mode_is_drop(self):
        content = "Based on the data, X.\n\nThe answer: X."
        self.assertEqual(wrap_leaked_reasoning(
            content, pattern=DEFAULT_REASONING_LEAD_PATTERN, label=LABEL), "The answer: X.")


if __name__ == "__main__":
    unittest.main()
