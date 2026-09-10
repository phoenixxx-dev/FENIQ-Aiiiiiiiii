"""صيانة دورية: تنظيف الرفعات المهجورة وتطبيق سياسة الاحتفاظ.

مكانها هنا لا في الـAPI: كلاهما عملية طويلة على عدد غير محدود من السجلات
(قاعدة ذهبية #6). تُشغَّل من العامل بجدول، أو يدوياً:

    python3 -m apps.api.src.services.maintenance
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db.models import (CleaningRecipeRow, Conversation, Dataset,
                         DatasetProfile, DatasetSchema, InsightRow, Job,
                         Message)
from ..storage.cache import invalidate
from ..storage.client import get_storage

log = logging.getLogger("phoenix.maintenance")

# الحالتان اللتان تعنيان «سجل محجوز ولم تبدأ معالجته»
PENDING_UPLOAD = ("awaiting_upload", "uploaded")


async def _purge(db: AsyncSession, datasets: list[Dataset]) -> int:
    """يحذف الملفات وكل ما اشتُقّ منها. نفس منطق الحذف اليدوي بالضبط."""
    settings = get_settings()
    storage = get_storage()
    removed = 0

    for ds in datasets:
        for bucket, key in ((settings.s3_bucket_raw, ds.raw_key),
                            (settings.s3_bucket_processed, ds.processed_key)):
            if not key:
                continue
            try:
                storage.delete(bucket, key)
            except Exception as e:                                # noqa: BLE001
                log.warning("تعذّر حذف كائن", extra={"extra_fields": {
                    "bucket": bucket, "key": key, "error": str(e)}})
        if ds.processed_key:
            invalidate(ds.processed_key)

        for model in (DatasetSchema, DatasetProfile, CleaningRecipeRow, InsightRow):
            await db.execute(delete(model).where(model.dataset_id == ds.id))
        await db.execute(delete(Message).where(Message.conversation_id.in_(
            select(Conversation.id).where(Conversation.dataset_id == ds.id))))
        await db.execute(delete(Conversation).where(Conversation.dataset_id == ds.id))
        await db.execute(delete(Job).where(Job.dataset_id == ds.id))
        await db.execute(update(Dataset).where(Dataset.duplicate_of == ds.id)
                         .values(duplicate_of=None))
        await db.delete(ds)
        removed += 1

    if removed:
        await db.commit()
    return removed


async def purge_abandoned_uploads(db: AsyncSession, hours: int | None = None) -> int:
    """يحذف سجلات الرفع التي لم يصل محتواها.

    تنشأ كلما طلب متصفحٌ رابطَ رفع ثم أُغلق. بلا تنظيف تتراكم صفوفٌ ومفاتيح
    تخزينٍ يتيمة إلى ما لا نهاية.
    """
    hours = hours if hours is not None else get_settings().abandoned_upload_hours
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    res = await db.execute(
        select(Dataset).where(Dataset.status.in_(PENDING_UPLOAD),
                              Dataset.created_at < cutoff)
    )
    victims = list(res.scalars().all())
    removed = await _purge(db, victims)
    if removed:
        log.info("purged abandoned uploads",
                 extra={"extra_fields": {"count": removed, "older_than_hours": hours}})
    return removed


async def apply_retention(db: AsyncSession, days: int | None = None) -> int:
    """يحذف الملفات الأقدم من مدة الاحتفاظ. 0 = معطّلة (الافتراضي)."""
    days = days if days is not None else get_settings().retention_days
    if days <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    res = await db.execute(select(Dataset).where(Dataset.created_at < cutoff))
    victims = list(res.scalars().all())
    removed = await _purge(db, victims)
    if removed:
        log.info("retention purge", extra={"extra_fields": {
            "count": removed, "retention_days": days}})
    return removed


async def run_maintenance(db: AsyncSession) -> dict[str, int]:
    return {"abandoned": await purge_abandoned_uploads(db),
            "retention": await apply_retention(db)}


if __name__ == "__main__":                                        # pragma: no cover
    import asyncio

    from ..db.session import get_sessionmaker
    from ..observability import configure_logging

    async def _main() -> None:
        configure_logging()
        async with get_sessionmaker()() as db:
            print(await run_maintenance(db))

    asyncio.run(_main())
