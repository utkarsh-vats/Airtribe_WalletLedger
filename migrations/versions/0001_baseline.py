"""baseline: empty revision that anchors the migration chain

Creates nothing. It exists so `alembic upgrade head` on an empty database has a revision to
apply (creating alembic_version), proving the migrate pipeline before the first real schema
migration in step 1.2.

Revision ID: 0001
Revises:
Create Date: 2026-11-18
"""

from collections.abc import Sequence

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
