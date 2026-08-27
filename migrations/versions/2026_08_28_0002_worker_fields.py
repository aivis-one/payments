"""worker bookkeeping fields on invoices

Separate revision on purpose. TOR section 4 kept these three columns out of the
first migration because the worker that reads and writes them did not exist
yet, and a column nobody touches is a promise the schema cannot keep.

``next_check_at`` is nullable with no default, and no backfill is performed.
NULL is the column's permanent meaning -- "never looked at" -- because the API
route that moves an invoice into ``awaiting_confirmations`` does not set it and
is not meant to. Backfilling a timestamp here would make the migrated rows look
like they had been checked, and would not help the rows created tomorrow.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.add_column(
        "invoices",
        sa.Column(
            "confirmations_seen",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "invoices",
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "invoices",
        sa.Column("next_check_at", sa.DateTime(timezone=True), nullable=True),
    )

    # The worker's only selection: invoices awaiting confirmations that are due.
    # Partial, because every other status is irrelevant to it and indexing them
    # would grow the index with rows it will never look at.
    op.create_index(
        "ix_invoices_due_for_check",
        "invoices",
        ["next_check_at"],
        postgresql_where=sa.text("status = 'awaiting_confirmations'"),
    )


def downgrade() -> None:
    op.drop_index("ix_invoices_due_for_check", table_name="invoices")
    op.drop_column("invoices", "next_check_at")
    op.drop_column("invoices", "last_checked_at")
    op.drop_column("invoices", "confirmations_seen")
