"""فحص الصحة — الخدمة الوحيدة المفتوحة بلا مصادقة (مع auth)."""
from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text

from ..config import get_settings
from ..db.session import get_sessionmaker
from ..storage.client import get_storage

router = APIRouter(tags=["health"])

# دون هذا الحد تبدأ المشاكل قبل الامتلاء الكامل: قاعدة البيانات
# تحتاج مساحة للكتابة، والمعالجة تحتاج نسخة مؤقتة.
LOW_DISK_MB = 2048


async def _db_ok() -> bool:
    try:
        async with get_sessionmaker()() as s:
            await s.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def _redis_ok() -> bool:
    try:
        from redis.asyncio import from_url
        client = from_url(get_settings().redis_url)
        try:
            return bool(await client.ping())
        finally:
            await client.aclose()
    except Exception:
        return False


@router.get("/health")
async def health() -> dict:
    db_ok, redis_ok = await _db_ok(), await _redis_ok()
    try:
        storage_ok = get_storage().health()
    except Exception:
        storage_ok = False
    # المساحة الحرة جزء من الصحة لا معلومة جانبية: مع التخزين على قرص
    # السيرفر (ق-48) القرصُ الممتلئ يوقف كل شيء — قاعدة البيانات والسجلات
    # والمعالجة — ويبدو العطل حينها غامضاً تماماً. أن يُقال «المساحة ضاقت»
    # قبل الامتلاء يوفّر ساعة تشخيص.
    try:
        free = get_storage().free_bytes()
    except Exception:
        free = None
    free_mb = None if free is None else free // (1024 * 1024)
    disk_ok = free_mb is None or free_mb >= LOW_DISK_MB

    all_ok = db_ok and redis_ok and storage_ok
    body = {
        "status": "ok" if all_ok else "degraded",
        "db": "ok" if db_ok else "down",
        "redis": "ok" if redis_ok else "down",
        "storage": "ok" if storage_ok else "down",
        "storage_backend": "s3" if get_settings().uses_s3 else "local-dev",
    }
    if free_mb is not None:
        body["disk_free_mb"] = free_mb
        body["disk"] = "ok" if disk_ok else "low"
        if not disk_ok and all_ok:
            body["status"] = "degraded"
    return body
