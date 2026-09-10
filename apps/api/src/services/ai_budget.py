"""سقف الإنفاق الشهري على النموذج — الخطر رقم 2 في الخطة.

المبدأ: **السقف يوقف التصعيد لا المنتج**. عند تجاوزه تُجاب الأسئلة بالمسار
الحتمي وحده (وهو الذي يجيب الغالبية أصلاً)، ويُخبَر المستخدم بوضوح أن
الصياغة الذكية متوقفة هذا الشهر — لا رسالة خطأ ولا شاشة معطّلة.

التقدير معلن: نضرب عدد النداءات في كلفة تقديرية لكل نداء. ليس فاتورة، لكنه
يكشف الاتجاه ويوقف قبل المفاجأة — وهو أفضل بكثير من لا شيء.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db.models import Message

log = logging.getLogger("phoenix.budget")


def _month_start() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def spent_this_month(db: AsyncSession) -> float:
    res = await db.execute(
        select(func.coalesce(func.sum(Message.cost_usd), 0.0))
        .where(Message.created_at >= _month_start())
    )
    return float(res.scalar_one() or 0.0)


async def within_budget(db: AsyncSession) -> tuple[bool, float]:
    """(هل نسمح بالتصعيد؟, المصروف التقديري هذا الشهر)."""
    budget = get_settings().llm_monthly_budget_usd
    spent = await spent_this_month(db)
    if budget <= 0:
        return True, spent
    return spent < budget, spent


def cost_of(llm_calls: int) -> float:
    return round(llm_calls * get_settings().llm_cost_per_call_usd, 6)
