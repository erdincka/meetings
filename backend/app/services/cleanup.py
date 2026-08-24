"""What terminating a meeting does to its record.

``cleanup_rules`` was persisted and editable in the settings UI while nothing
read it, so terminating always kept everything regardless of what the operator
chose. This module is what makes that setting mean something.
"""

from __future__ import annotations

from typing import Final

import structlog

from app.models.meetings import Meeting

logger = structlog.get_logger(__name__)

KEEP_HISTORY: Final = "terminate_keeps_history"
PURGE_TRANSCRIPT: Final = "terminate_purges_transcript"

CLEANUP_RULES: Final = (KEEP_HISTORY, PURGE_TRANSCRIPT)


def apply_cleanup_rules(meeting: Meeting, rule: str) -> None:
    """Apply the operator's cleanup rule to a meeting being terminated.

    ``KEEP_HISTORY`` leaves the transcript intact -- the default, and what an
    audit trail wants. ``PURGE_TRANSCRIPT`` drops the conversation but keeps the
    meeting row, so the fact that it happened survives while its contents do
    not. An unrecognised rule keeps history: discarding a transcript because a
    setting was misspelled is not a recoverable mistake.
    """
    if rule == PURGE_TRANSCRIPT:
        meeting.meeting_log = []
        meeting.final_summary = None
        logger.info("meeting_transcript_purged", meeting_id=str(meeting.id), rule=rule)
        return

    if rule != KEEP_HISTORY:
        logger.warning("unknown_cleanup_rule_keeping_history", rule=rule)
