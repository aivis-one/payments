"""invoices and invoice_txid_attempts

Revision ID: 0001
Revises:
Create Date: 2026-08-26

Worker fields (confirmations_seen, last_checked_at, next_check_at) are not
here: they belong to the confirmations worker and arrive with it.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "invoices",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("product_ref", sa.String(), nullable=False),
        sa.Column("network", sa.String(), nullable=False),
        sa.Column("address", sa.String(), nullable=False),
        sa.Column("invoice_amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("credited_amount_cents", sa.BigInteger(), nullable=True),
        sa.Column("underpaid", sa.Boolean(), nullable=True),
        sa.Column("active_txid", sa.String(), nullable=True),
        sa.Column("attempts_used", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("slot_frozen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "invoice_txid_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("invoice_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("network", sa.String(), nullable=False),
        sa.Column("txid", sa.String(), nullable=False),
        sa.Column("result_code", sa.String(), nullable=False),
        sa.Column("from_address", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["invoice_id"], ["invoices.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )

    # Partial unique index: only a matched attempt holds a TXID. Two rejected
    # attempts carrying the same (network, txid) are both allowed; a second
    # matched one is not.
    op.create_index(
        "uq_txid_attempts_network_txid",
        "invoice_txid_attempts",
        ["network", "txid"],
        unique=True,
        postgresql_where=sa.text("result_code = 'matched'"),
    )


def downgrade() -> None:
    op.drop_index("uq_txid_attempts_network_txid", table_name="invoice_txid_attempts")
    op.drop_table("invoice_txid_attempts")
    op.drop_table("invoices")
