"""تدوير توكن التجديد — الخطة §9.1: «تدوير الـrefresh عند كل استخدام».

المشكلة بلا تدوير: توكن تجديد مسروق يبقى صالحاً 14 يوماً كاملة، ولا شيء
يكشف استعماله. مع التدوير، كل توكن يُستهلك مرة واحدة؛ ومحاولة إعادة استعماله
تُرفض — وهي في الوقت نفسه **إشارة** إلى أن نسخة منه بيد شخص آخر.

التخزين: Redis، بمهلة تساوي عمر التوكن المتبقّي فقط — فلا تنمو القائمة أبداً.

سياسة الفشل: لو تعطّل Redis نسمح بالتجديد ونسجّل خطأً. المقايضة صريحة:
تعطيل التجديد يُخرج كل المستخدمين عند أول انقطاع، بينما فقدان الحماية
مؤقتاً يُعيدنا إلى ما كان قبل هذه الميزة لا أسوأ.
"""
from __future__ import annotations

import logging

from ..config import get_settings

log = logging.getLogger("phoenix.refresh")

_PREFIX = "refresh:used:"


async def _client():
    from redis.asyncio import from_url
    return from_url(get_settings().redis_url)


async def is_used(jti: str) -> bool:
    """هل استُهلك هذا التوكن من قبل؟"""
    try:
        client = await _client()
        try:
            return bool(await client.exists(f"{_PREFIX}{jti}"))
        finally:
            await client.aclose()
    except Exception as e:                                        # noqa: BLE001
        log.error("تعذّر فحص تدوير التوكن — سُمح بالتجديد بلا حماية إعادة الاستعمال: %s", e)
        return False


async def mark_used(jti: str, ttl_seconds: int) -> None:
    """يسجّل التوكن مستهلَكاً حتى نهاية عمره الأصلي."""
    if ttl_seconds <= 0:
        return
    try:
        client = await _client()
        try:
            await client.set(f"{_PREFIX}{jti}", "1", ex=ttl_seconds)
        finally:
            await client.aclose()
    except Exception as e:                                        # noqa: BLE001
        log.error("تعذّر تسجيل استهلاك التوكن: %s", e)
