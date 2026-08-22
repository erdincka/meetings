"""align embedding dimension with configured model

Revision ID: 286db52aad07
Revises: 3488bce17480
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.config import settings

revision: str = "286db52aad07"
down_revision: str | None = "3488bce17480"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = settings.DB_SCHEMA


def _current_dimension(conn: sa.Connection) -> int | None:
    """Declared width of the embedding column, or None if unsized."""
    # Joined against pg_class/pg_namespace rather than casting a bind parameter
    # to regclass: SQLAlchemy's text() parses ":table::regclass" as a parameter
    # followed by a stray cast and the statement fails to compile.
    value = conn.execute(
        sa.text(
            "SELECT a.atttypmod "
            "FROM pg_attribute a "
            "JOIN pg_class c ON c.oid = a.attrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = :schema AND c.relname = 'document_chunks' "
            "AND a.attname = 'embedding'"
        ),
        {"schema": SCHEMA},
    ).scalar()
    return None if value in (None, -1) else int(value)


def upgrade() -> None:
    """Resize the vector column to match EMBEDDING_DIM, refusing to destroy data.

    The width must match the embedding model exactly. Switching models -- say
    from a 2048-wide model to nomic-embed-text at 768 -- makes every stored
    vector meaningless rather than merely mis-sized, because the numbers came
    from a different space entirely.

    Silently truncating them would leave retrieval quietly returning nonsense,
    which is considerably worse than a failed migration. So this refuses unless
    the table is empty.
    """
    conn = op.get_bind()
    target = settings.EMBEDDING_DIM
    current = _current_dimension(conn)

    if current == target:
        return

    chunks = conn.execute(sa.text(f"SELECT count(*) FROM {SCHEMA}.document_chunks")).scalar_one()

    if chunks:
        raise RuntimeError(
            f"Refusing to change the embedding dimension from {current} to {target} "
            f"while {chunks} chunks exist: those vectors were produced by a different "
            "model and cannot be reinterpreted at a new width. Re-embed the corpus, "
            "or delete the documents, then re-run this migration."
        )

    op.execute(
        f"ALTER TABLE {SCHEMA}.document_chunks ALTER COLUMN embedding TYPE vector({target})"
    )


def downgrade() -> None:
    # No meaningful inverse: the previous width is not recorded, and the stored
    # vectors would be equally invalid going back.
    pass
