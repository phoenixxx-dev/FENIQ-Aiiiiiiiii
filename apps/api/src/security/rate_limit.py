"""تحديد المحاولات عبر Redis.

لماذا Redis وليس عدّاداً في الذاكرة (docs/DECISIONS.md ق-6): الـAPI مصمَّم
ليعمل بأكثر من نسخة. عدّاد داخل العملية ينتج حداً وهمياً — نسختان تعنيان ضعف
الحد المسموح فعلياً، والحماية تصير على الورق فقط.

مبدأ التصميم: **الفشل لا يمنع الخدمة**. لو تعطّل Redis نسمح بالطلب ونسجّل
تحذيراً — تعطيل تسجيل الدخول لكل المستخدمين أسوأ من فقدان الحد مؤقتاً.
"""
from __future__ import annotations

import logging
import time

from fastapi import HTTPException, Request, status

from ..config import get_settings

log = logging.getLogger("phoenix.ratelimit")


async def _hit(key: str, limit: int, window_seconds: int = 60) -> tuple[bool, int]:
    """يزيد العدّاد ويُرجع (مسموح؟, الثواني حتى إعادة الضبط)."""
    try:
        from redis.asyncio import from_url
        client = from_url(get_settings().redis_url)
        try:
            bucket = int(time.time()) // window_seconds
            redis_key = f"rl:{key}:{bucket}"
            count = await client.incr(redis_key)
            if count == 1:
                await client.expire(redis_key, window_seconds)
            reset_in = window_seconds - (int(time.time()) % window_seconds)
            return count <= limit, reset_in
        finally:
            await client.aclose()
    except Exception as e:                                   # noqa: BLE001
        log.warning("تعذّر فحص حد المحاولات (%s) — سُمح بالطلب: %s", key, e)
        return True, 0


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def enforce(request: Request, scope: str, limit: int) -> None:
    allowed, reset_in = await _hit(f"{scope}:{client_ip(request)}", limit)
    if not allowed:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"error_code": "rate_limited",
                    "message_ar": f"محاولات كثيرة خلال وقت قصير. جرّب بعد {reset_in} ثانية."},
            headers={"Retry-After": str(reset_in)},
        )


async def limit_login(request: Request) -> None:
    """الدخول والتسجيل: حد ضيّق — هذا هو المسار المستهدَف بتخمين كلمات المرور."""
    await enforce(request, "login", get_settings().rate_limit_login_per_minute)


async def limit_general(request: Request) -> None:
    """الحد العام لكل المسارات المحمية.

    ⚠️ كانت هذه الدالة **معرَّفة ولا تُستعمل** في أي مسار: الإعداد موجود،
    والاختبار موجود لأخيها، والحماية غير مطبَّقة إطلاقاً. حقلٌ يبدو أن أحداً
    يستعمله ولا أحد يستعمله — نفس نمط `cost_usd` (ق-28).
    """
    await enforce(request, "general", get_settings().rate_limit_general_per_minute)


async def limit_ask(request: Request) -> None:
    """«اسأل فينيق» — أغلى مسار في المنتج.

    كل سؤال يفتح DuckDB ويقرأ Parquet، وقد يستدعي نموذجاً بتكلفة مالية.
    حدُّه أضيق من الحد العام عمداً (الخطة §9.4).
    """
    await enforce(request, "ask", get_settings().rate_limit_ask_per_minute)
