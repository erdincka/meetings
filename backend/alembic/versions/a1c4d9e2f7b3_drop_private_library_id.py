"""drop the vestigial private_library_id

A unique UUID was generated for every role and exposed through the API while
nothing joined on it: private-library documents are scoped by
``documents.owner_agent_id``, which holds the role id. Keeping a column that
looks like the scoping key but is not invites someone to use it as one.

Revision ID: a1c4d9e2f7b3
Revises: 0953420f1a6a
Create Date: 2026-08-23 17:35:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "a1c4d9e2f7b3"
down_revision: Union[str, None] = "0953420f1a6a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("role_agents", "private_library_id", schema="meetings")


def downgrade() -> None:
    # Restored nullable: the original values are gone, and nothing reads them.
    op.add_column(
        "role_agents",
        sa.Column("private_library_id", UUID(as_uuid=True), nullable=True),
        schema="meetings",
    )
