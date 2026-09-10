"""سجل التدقيق — من فعل ماذا ومتى.

قاعدة: التسجيل لا يُسقط العملية أبداً. لو فشلت الكتابة نُسجّل تحذيراً ونكمل؛
إسقاط رفع ملف ناجح لأن سطر تدقيق لم يُكتب سلوك خاطئ.
"""
from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import AuditLog

log = logging.getLogger("phoenix.audit")


async def record(db: AsyncSession, *, user_id: str | None, action: str,
                 resource: str | None = None, ip: str | None = None) -> None:
    try:
        db.add(AuditLog(user_id=user_id, action=action, resource=resource, ip=ip))
        await db.flush()
    except Exception as e:                                   # noqa: BLE001
        log.warning("تعذّر كتابة سجل التدقيق (%s): %s", action, e)
