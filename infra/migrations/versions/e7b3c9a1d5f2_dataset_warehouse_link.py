"""datasets.warehouse_id — ملفات المزامنة تُطوى تحت مستودعها

Revision ID: e7b3c9a1d5f2
Revises: d4a9e1b7c2f5
Create Date: 2026-09-11
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "e7b3c9a1d5f2"
down_revision: Union[str, Sequence[str], None] = "d4a9e1b7c2f5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("datasets", sa.Column("warehouse_id", sa.String(36), nullable=True))
    op.create_index("ix_datasets_warehouse_id", "datasets", ["warehouse_id"])
    # ملفات مزامنات سابقة (قبل هذا العمود) تُنسب لمستودعها من سجل المزامنة
    op.execute("UPDATE datasets SET warehouse_id = sync_runs.warehouse_id "
               "FROM sync_runs WHERE sync_runs.dataset_id = datasets.id")


def downgrade() -> None:
    op.drop_index("ix_datasets_warehouse_id", "datasets")
    op.drop_column("datasets", "warehouse_id")
