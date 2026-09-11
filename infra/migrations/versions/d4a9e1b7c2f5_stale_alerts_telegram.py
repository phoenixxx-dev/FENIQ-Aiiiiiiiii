"""stale-sync alerts via Telegram — المرحلة ٢

Revision ID: d4a9e1b7c2f5
Revises: c8d2e5f1a7b3
Create Date: 2026-09-11
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "d4a9e1b7c2f5"
down_revision: Union[str, Sequence[str], None] = "c8d2e5f1a7b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("telegram_chat_id", sa.String(32), nullable=True))
    op.add_column("users", sa.Column("telegram_link_code", sa.String(32), nullable=True))
    op.create_index("ix_users_telegram_link_code", "users", ["telegram_link_code"])
    op.add_column("warehouses", sa.Column("stale_alerted_at", sa.DateTime(timezone=True),
                                          nullable=True))


def downgrade() -> None:
    op.drop_column("warehouses", "stale_alerted_at")
    op.drop_index("ix_users_telegram_link_code", "users")
    op.drop_column("users", "telegram_link_code")
    op.drop_column("users", "telegram_chat_id")
