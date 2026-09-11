"""كشف المستودعات المنقطعة عن المزامنة وتنبيه أصحابها (المرحلة ٢).

تنبيه واحد لكل انقطاع: `stale_alerted_at` يُختم عند الإرسال ويُمسح عند عودة
المزامنة (في نقطة الاستقبال) مع رسالة «عادت». المستودع الذي لم يرسل أبداً لا
يُعدّ منقطعاً — لا نعرف متى «يجب» أن يبدأ.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db.models import User, Warehouse
from . import telegram


def _hours(delta: timedelta) -> int:
    return int(delta.total_seconds() // 3600)


async def check_stale(db: AsyncSession, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    limit = timedelta(hours=get_settings().stale_after_hours)
    res = await db.execute(
        select(Warehouse, User).join(User, User.id == Warehouse.user_id)
        .where(Warehouse.last_sync_at.is_not(None),
               Warehouse.last_sync_at < now - limit,
               Warehouse.stale_alerted_at.is_(None)))
    stale, alerted = 0, 0
    for wh, user in res.all():
        stale += 1
        if not user.telegram_chat_id:
            continue          # لا قناة — الواجهة تُظهر التأخّر على أي حال
        text = (f"⚠️ فينيق: المستودع «{wh.name}» لم يرسل مزامنة منذ "
                f"{_hours(now - wh.last_sync_at)} ساعة. تأكّد أن جهاز المستودع شغّال "
                "ومتصل بالإنترنت.")
        if await telegram.send(user.telegram_chat_id, text):
            wh.stale_alerted_at = now
            alerted += 1
    await db.commit()
    return {"stale": stale, "alerted": alerted}


async def notify_recovered(chat_id: str | None, name: str) -> None:
    if chat_id:
        await telegram.send(chat_id, f"✅ فينيق: عادت المزامنة من المستودع «{name}».")
