"""Model-output interpretation.

These paths decide who speaks next and what reaches the transcript. Before
Phase 1 none of them had a single test.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.orchestration.recovery import (
    FINISH,
    recover_speaker_id,
    split_thought,
    strip_speaker_prefix,
)


@dataclass
class FakeAttendee:
    display_name: str
    title: str


CEO = "11111111-1111-1111-1111-111111111111"
CFO = "22222222-2222-2222-2222-222222222222"
GC = "33333333-3333-3333-3333-333333333333"

ATTENDEES = {
    CEO: FakeAttendee("Chief Executive Officer", "CEO"),
    CFO: FakeAttendee("Finance Director", "CFO"),
    GC: FakeAttendee("General Counsel", "GC"),
}
VALID = set(ATTENDEES)


class TestRecoverSpeakerId:
    def test_exact_id_passes_through(self) -> None:
        assert recover_speaker_id(CEO, VALID, ATTENDEES) == CEO

    def test_surrounding_whitespace_tolerated(self) -> None:
        assert recover_speaker_id(f"  {CFO}\n", VALID, ATTENDEES) == CFO

    def test_finish_is_honoured_case_insensitively(self) -> None:
        assert recover_speaker_id("finish", VALID, ATTENDEES) == FINISH
        assert recover_speaker_id("FINISH", VALID, ATTENDEES) == FINISH

    def test_display_name_is_recovered(self) -> None:
        assert recover_speaker_id("General Counsel", VALID, ATTENDEES) == GC

    def test_title_is_recovered(self) -> None:
        assert recover_speaker_id("CFO", VALID, ATTENDEES) == CFO

    def test_partial_name_is_recovered_when_unambiguous(self) -> None:
        assert recover_speaker_id("Finance", VALID, ATTENDEES) == CFO

    @pytest.mark.parametrize("raw", [None, "", "   "])
    def test_empty_output_finishes(self, raw: str | None) -> None:
        assert recover_speaker_id(raw, VALID, ATTENDEES) == FINISH

    @pytest.mark.parametrize("raw", ["a", "e", "of"])
    def test_too_short_to_be_meaningful_finishes(self, raw: str) -> None:
        """Regression: a 1-2 character reply used to substring-match the first
        attendee containing that letter and hand them the turn at random."""
        assert recover_speaker_id(raw, VALID, ATTENDEES) == FINISH

    def test_ambiguous_match_finishes_rather_than_guessing(self) -> None:
        """ "Chief" matches nothing uniquely here once two chiefs exist."""
        attendees = {
            CEO: FakeAttendee("Chief Executive Officer", "CEO"),
            GC: FakeAttendee("Chief Legal Officer", "CLO"),
        }
        assert recover_speaker_id("Chief", set(attendees), attendees) == FINISH

    def test_unrecoverable_name_finishes(self) -> None:
        assert recover_speaker_id("Head of Wombats", VALID, ATTENDEES) == FINISH


class TestSplitThought:
    def test_thought_tag_is_extracted_and_removed(self) -> None:
        public, thought = split_thought("<thought>plotting</thought>Hello team.")
        assert public == "Hello team."
        assert thought == "plotting"

    def test_thinking_tag_variant_is_handled(self) -> None:
        public, thought = split_thought("<thinking>hmm</thinking>Agreed.")
        assert public == "Agreed."
        assert thought == "hmm"

    def test_multiline_thought(self) -> None:
        public, thought = split_thought("<thought>line one\nline two</thought>Done.")
        assert public == "Done."
        assert thought == "line one\nline two"

    def test_multiple_thoughts_are_joined(self) -> None:
        public, thought = split_thought("<thought>a</thought>Mid.<thought>b</thought>End.")
        assert public == "Mid.End."
        assert thought == "a\n\nb"

    def test_no_thought_returns_content_unchanged(self) -> None:
        assert split_thought("Just talking.") == ("Just talking.", "")

    @pytest.mark.parametrize("raw", [None, ""])
    def test_empty_input(self, raw: str | None) -> None:
        assert split_thought(raw) == ("", "")

    def test_reasoning_never_leaks_into_public_text(self) -> None:
        secret = "the CFO is hiding the numbers"
        public, thought = split_thought(f"<thought>{secret}</thought>Let us proceed.")
        assert secret not in public
        assert secret in thought


class TestStripSpeakerPrefix:
    def test_hallucinated_prefix_removed(self) -> None:
        assert strip_speaker_prefix("[Jane Doe - CFO] Revenue is up.") == "Revenue is up."

    def test_only_leading_prefix_removed(self) -> None:
        assert strip_speaker_prefix("[A] see [B] later") == "see [B] later"

    def test_untagged_content_untouched(self) -> None:
        assert strip_speaker_prefix("Revenue is up.") == "Revenue is up."
