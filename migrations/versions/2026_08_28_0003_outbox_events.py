"""outbox_events

The table that makes an outgoing event unloseable: it is written by the same
transaction that moves the invoice into a published status, so "the invoice is
confirmed but nobody was told" is not a state this service can be in.

``next_attempt_at`` is NOT NULL and carries the publication time, unlike
``invoices.next_check_at``, which is nullable and means "never looked at".
There is no equivalent meaning here -- a published event is due immediately --
and a nullable column would need every selection to spell out that NULL is due
now, which is precisely the trap the worker's migration documents.

No backfill: before this revision there were no events, and inventing rows for
transitions that happened while nothing was listening would send the product a
history it has no way to act on.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.create_table(
        "outbox_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "invoice_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("invoices.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("invoice_status", sa.String(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "delivery_state",
            sa.String(),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("claims", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    # The product deduplicates on (invoice_id, status), so the same pair is the
    # identity of an event here. Unique, because two writers racing on the same
    # transition is reachable -- the API does not lock the invoice row -- and
    # closing that in the schema costs one index instead of a branch that would
    # have to be right in five places.
    op.create_index(
        "uq_outbox_events_invoice_status",
        "outbox_events",
        ["invoice_id", "invoice_status"],
        unique=True,
    )

    # The delivery loop's only selection. Partial: delivered and failed rows are
    # never looked at again and would grow the index for nothing.
    op.create_index(
        "ix_outbox_events_due",
        "outbox_events",
        ["next_attempt_at"],
        postgresql_where=sa.text("delivery_state = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_events_due", table_name="outbox_events")
    op.drop_index("uq_outbox_events_invoice_status", table_name="outbox_events")
    op.drop_table("outbox_events")
