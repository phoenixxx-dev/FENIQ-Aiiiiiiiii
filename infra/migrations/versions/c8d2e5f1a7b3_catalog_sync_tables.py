"""catalog sync tables — المرحلة ١: استقبال كتالوج FeniqSync

ميتاداتا فقط: المستودع، مفاتيح الأجهزة (بصمة لا نص)، وسجل كل مزامنة.
الأصناف والأسعار في Parquet بالتخزين — قاعدة ذهبية #2 (DECISIONS.md ق-50).

Revision ID: c8d2e5f1a7b3
Revises: b3c71d9a4e02
Create Date: 2026-09-10
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c8d2e5f1a7b3"
down_revision: Union[str, Sequence[str], None] = "b3c71d9a4e02"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "warehouses",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("external_id", sa.String(255), nullable=True),
        sa.Column("catalog_state_key", sa.String(1024), nullable=True),
        sa.Column("active_item_count", sa.Integer(), nullable=False),
        sa.Column("last_sync_at", TS, nullable=True),
        sa.Column("last_sync_id", sa.String(36), nullable=True),
        sa.Column("created_at", TS, server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_warehouses_user_id", "warehouses", ["user_id"])
    op.create_index("ix_warehouses_last_sync_at", "warehouses", ["last_sync_at"])

    op.create_table(
        "sync_tokens",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("warehouse_id", sa.String(36), sa.ForeignKey("warehouses.id"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("prefix", sa.String(16), nullable=False),
        sa.Column("label", sa.String(255), nullable=True),
        sa.Column("created_at", TS, server_default=sa.func.now(), nullable=False),
        sa.Column("last_used_at", TS, nullable=True),
        sa.Column("revoked_at", TS, nullable=True),
    )
    op.create_index("ix_sync_tokens_warehouse_id", "sync_tokens", ["warehouse_id"])
    op.create_index("ix_sync_tokens_token_hash", "sync_tokens", ["token_hash"], unique=True)

    op.create_table(
        "sync_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("warehouse_id", sa.String(36), sa.ForeignKey("warehouses.id"), nullable=False),
        sa.Column("token_id", sa.String(36), nullable=True),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("mode", sa.String(8), nullable=False),
        sa.Column("generated_at", TS, nullable=True),
        sa.Column("received_at", TS, nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("added", sa.Integer(), nullable=False),
        sa.Column("price_changed", sa.Integer(), nullable=False),
        sa.Column("removed", sa.Integer(), nullable=False),
        sa.Column("reactivated", sa.Integer(), nullable=False),
        sa.Column("active_total", sa.Integer(), nullable=False),
        sa.Column("raw_key", sa.String(1024), nullable=False),
        sa.Column("state_key", sa.String(1024), nullable=False),
        sa.Column("price_changes_key", sa.String(1024), nullable=True),
        sa.Column("dataset_id", sa.String(36), nullable=True),
        sa.Column("job_id", sa.String(36), nullable=True),
        sa.UniqueConstraint("warehouse_id", "content_sha256", name="uq_sync_runs_warehouse_sha"),
    )
    op.create_index("ix_sync_runs_warehouse_id", "sync_runs", ["warehouse_id"])
    op.create_index("ix_sync_runs_wh_received", "sync_runs", ["warehouse_id", "received_at"])


def downgrade() -> None:
    op.drop_table("sync_runs")
    op.drop_table("sync_tokens")
    op.drop_table("warehouses")
