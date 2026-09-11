"""تنبيهات انقطاع المزامنة (المرحلة ٢): ربط تيليغرام + نقطة الفحص الدوري.

    GET    /v1/alerts/telegram          حالة الربط
    POST   /v1/alerts/telegram/link     رابط t.me بالرمز المؤقت
    POST   /v1/alerts/telegram/verify   «ضغطت Start» ← نقرأ رسائل البوت ونربط
    DELETE /v1/alerts/telegram          فكّ الربط
    POST   /v1/internal/check-stale     للمنبّه الخارجي فقط (ترويسة X-Cron-Secret)

لماذا منبّه خارجي: السيرفر المجاني ينام بعد ١٥ دقيقة بلا طلبات. مستودع توقّف عن
الإرسال = لا طلبات = سيرفر نائم = لا أحد يفحص. التنبيه كان سيغيب تحديداً حين
يلزم. GitHub Actions يوقظه كل ساعة (.github/workflows/stale-check.yml).
"""
from __future__ import annotations

import hmac
import secrets
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from ..config import get_settings
from ..deps import CurrentUser, SessionDep
from ..services import telegram
from ..services.stale import check_stale

router = APIRouter(prefix="/v1", tags=["alerts"])


class TelegramStatus(BaseModel):
    configured: bool
    linked: bool


class TelegramLink(BaseModel):
    url: str


class StaleCheckOut(BaseModel):
    stale: int
    alerted: int


def _not_configured() -> HTTPException:
    return HTTPException(503, detail={"error_code": "telegram_not_configured",
                                      "message_ar": "تنبيهات تيليغرام غير مفعّلة على الخادم بعد."})


@router.get("/alerts/telegram", response_model=TelegramStatus)
async def telegram_status(user: CurrentUser) -> TelegramStatus:
    return TelegramStatus(configured=get_settings().telegram_ready,
                          linked=bool(user.telegram_chat_id))


@router.post("/alerts/telegram/link", response_model=TelegramLink)
async def telegram_link(user: CurrentUser, db: SessionDep) -> TelegramLink:
    s = get_settings()
    if not s.telegram_ready:
        raise _not_configured()
    user.telegram_link_code = secrets.token_urlsafe(12)
    await db.commit()
    return TelegramLink(url=f"https://t.me/{s.telegram_bot_username}?start={user.telegram_link_code}")


@router.post("/alerts/telegram/verify", response_model=TelegramStatus)
async def telegram_verify(user: CurrentUser, db: SessionDep) -> TelegramStatus:
    if not get_settings().telegram_ready:
        raise _not_configured()
    if user.telegram_link_code:
        chat = (await telegram.start_codes()).get(user.telegram_link_code)
        if chat:
            user.telegram_chat_id, user.telegram_link_code = chat, None
            await db.commit()
            await telegram.send(chat, "✅ تم ربط تنبيهات فينيق. رح يوصلك تنبيه إذا "
                                      "وقف أي مستودع عن المزامنة.")
    return TelegramStatus(configured=True, linked=bool(user.telegram_chat_id))


@router.delete("/alerts/telegram", status_code=204)
async def telegram_unlink(user: CurrentUser, db: SessionDep) -> None:
    user.telegram_chat_id = user.telegram_link_code = None
    await db.commit()


@router.post("/internal/check-stale", response_model=StaleCheckOut)
async def internal_check_stale(db: SessionDep,
                               x_cron_secret: Annotated[str | None, Header()] = None):
    expected = get_settings().cron_secret
    # فارغ = معطّل. compare_digest: لا تسريب للسر عبر زمن المقارنة.
    if not expected or not x_cron_secret or not hmac.compare_digest(x_cron_secret, expected):
        raise HTTPException(404, detail={"error_code": "not_found", "message_ar": "هذا المسار غير متاح في هذا الإعداد."})
    return StaleCheckOut(**await check_stale(db))
