"""The embedding-dimension migration guard.

Changing the vector width does not merely resize the stored vectors -- it makes
them meaningless, because they were produced by a different model in a different
space. Silently truncating would leave retrieval quietly returning nonsense,
which is much worse than a failed migration.

These tests exercise the decision logic directly, since driving Alembic against
a live database belongs in the integration suite.
"""

from __future__ import annotations

import pytest


def decide(current: int | None, target: int, chunk_count: int) -> str:
    """Mirror of the migration's branching, kept in one place for testing.

    Returns 'noop', 'alter', or 'refuse'.
    """
    if current == target:
        return "noop"
    if chunk_count:
        return "refuse"
    return "alter"


class TestDimensionChange:
    def test_matching_dimension_does_nothing(self) -> None:
        assert decide(768, 768, chunk_count=1000) == "noop"

    def test_empty_table_is_resized(self) -> None:
        assert decide(2048, 768, chunk_count=0) == "alter"

    def test_populated_table_refuses(self) -> None:
        """The case that matters: existing vectors must not be silently invalidated."""
        assert decide(2048, 768, chunk_count=1) == "refuse"

    def test_growing_the_width_also_refuses_when_populated(self) -> None:
        """Widening is no safer -- the numbers still came from another model."""
        assert decide(768, 2048, chunk_count=42) == "refuse"

    def test_unsized_column_is_resized_when_empty(self) -> None:
        assert decide(None, 768, chunk_count=0) == "alter"

    @pytest.mark.parametrize("count", [0, 1, 10_000])
    def test_noop_wins_regardless_of_row_count(self, count: int) -> None:
        assert decide(1536, 1536, chunk_count=count) == "noop"
