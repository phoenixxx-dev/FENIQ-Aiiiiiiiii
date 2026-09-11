"""ترقية قاعدة البيانات عند الإقلاع — `python -m apps.api.src.db.migrate`.

لماذا لا يكفي create_all: ينشئ الجداول الناقصة فقط ولا يضيف **أعمدة** جديدة
لجدول موجود. أول عمود جديد (المرحلة ٢) كان سيُسقط السيرفر الحي.

حالة خاصة: قاعدة Render الأولى أُنشئت بـcreate_all بلا جدول alembic_version.
ترقيتها مباشرة تحاول إنشاء جداول موجودة فتفشل. نختمها أولاً بآخر مراجعة
يطابقها مخططها (BASELINE)، ثم نرقّي فوقها.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

from ..config import get_settings

ROOT = Path(__file__).resolve().parents[4]
# آخر مراجعة كان مخطط قاعدة create_all يطابقها قبل المرحلة ٢
BASELINE = "c8d2e5f1a7b3"


async def _tables() -> set[str]:
    engine = create_async_engine(get_settings().database_url)
    try:
        async with engine.connect() as conn:
            return set(await conn.run_sync(lambda c: inspect(c).get_table_names()))
    finally:
        await engine.dispose()


def main() -> None:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "infra" / "migrations"))
    tables = asyncio.run(_tables())
    if "users" in tables and "alembic_version" not in tables:
        print(f"قاعدة أُنشئت بلا هجرات — تُختم بـ{BASELINE} قبل الترقية")
        command.stamp(cfg, BASELINE)
    command.upgrade(cfg, "head")


if __name__ == "__main__":
    main()
