"""add currency_override to datasets

عملة يختارها المستخدم حين لا يجد المحرك دليلاً عليها في الملف. عمودٌ مستقلّ
عمداً: تصحيحات الأعمدة خريطةٌ مفاتيحها أسماء أعمدة من ملف المستخدم، ودسّ مفتاح
محجوز فيها يصطدم يوماً بعمودٍ يحمل ذلك الاسم.

Revision ID: b3c71d9a4e02
Revises: ae6518aa2b19
Create Date: 2026-09-05
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b3c71d9a4e02"
down_revision: Union[str, Sequence[str], None] = "ae6518aa2b19"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("datasets", sa.Column("currency_override", sa.String(length=8), nullable=True))


def downgrade() -> None:
    op.drop_column("datasets", "currency_override")
