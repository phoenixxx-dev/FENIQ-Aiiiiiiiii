"""تيليغرام — الحدّ الشبكي الوحيد للتنبيهات. كل نداء لـapi.telegram.org يمر من هنا.

فشل الإرسال لا يُسقط أي عملية (تنبيه ضاع ≠ مزامنة ضاعت): يُسجَّل ويُرجع False.
الاختبارات تستبدل `transport` فلا تلمس الشبكة.
"""
from __future__ import annotations

import logging
from typing import Any, Protocol

import httpx

from ..config import get_settings

log = logging.getLogger("phoenix.telegram")


class Transport(Protocol):
    async def call(self, method: str, payload: dict) -> Any: ...


class HttpTransport:
    async def call(self, method: str, payload: dict) -> Any:
        token = get_settings().telegram_bot_token
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(f"https://api.telegram.org/bot{token}/{method}", json=payload)
            body = r.json()
            if not body.get("ok"):
                raise RuntimeError(body.get("description", "telegram error"))
            return body["result"]


transport: Transport = HttpTransport()


async def send(chat_id: str, text: str) -> bool:
    if not get_settings().telegram_ready:
        return False
    try:
        await transport.call("sendMessage", {"chat_id": chat_id, "text": text})
        return True
    except Exception as e:                                   # noqa: BLE001
        log.warning("تعذّر إرسال تنبيه تيليغرام: %s", e)
        return False


async def start_codes() -> dict[str, str]:
    """رموز /start الأخيرة ← chat_id. البوت لا يحتاج webhook: نسأل عند الطلب."""
    if not get_settings().telegram_ready:
        return {}
    try:
        updates = await transport.call("getUpdates", {"allowed_updates": ["message"]})
    except Exception as e:                                   # noqa: BLE001
        log.warning("تعذّر قراءة رسائل البوت: %s", e)
        return {}
    found: dict[str, str] = {}
    for u in updates or []:
        msg = u.get("message") or {}
        text = (msg.get("text") or "").strip()
        if text.startswith("/start "):
            found[text.split(" ", 1)[1].strip()] = str(msg["chat"]["id"])
    return found
