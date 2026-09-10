"""يثبت أن تحديد المحاولات يعمل فعلاً — لا يكفي أنه مكتوب بالكود.

يخفض الحد إلى 3 ويتأكد أن الطلب الرابع يُرفض بـ429 برسالة عربية.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "packages" / "data-engine"))

from apps.api.src.config import get_settings  # noqa: E402
from apps.api.src.db.session import create_all, dispose_engine  # noqa: E402
from apps.api.src.main import app  # noqa: E402

LIMIT = 3


@pytest_asyncio.fixture
async def strict_client(monkeypatch):
    """حد منخفض + مفتاح Redis نظيف، حتى لا تتسرب عدّادات من اختبارات أخرى."""
    settings = get_settings()
    monkeypatch.setattr(settings, "rate_limit_login_per_minute", LIMIT, raising=False)

    from redis.asyncio import from_url
    client = from_url(settings.redis_url)
    keys = [k async for k in client.scan_iter(match="rl:login:*")]
    if keys:
        await client.delete(*keys)
    await client.aclose()

    await dispose_engine()
    await create_all()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    await dispose_engine()


@pytest.mark.asyncio
async def test_login_attempts_are_limited(strict_client):
    body = {"email": f"rl_{uuid.uuid4().hex[:8]}@example.com", "password": "WrongPass123"}

    codes = []
    for _ in range(LIMIT + 2):
        r = await strict_client.post("/auth/login", json=body)
        codes.append(r.status_code)

    assert 429 in codes, f"الحد لم يُطبَّق إطلاقاً: {codes}"
    assert codes[-1] == 429, f"آخر محاولة كان يجب أن تُرفض: {codes}"

    last = await strict_client.post("/auth/login", json=body)
    assert last.json()["error_code"] == "rate_limited"
    assert last.json()["message_ar"]
    assert "Retry-After" in last.headers


@pytest.mark.asyncio
async def test_rate_limit_message_is_arabic_and_actionable(strict_client):
    body = {"email": f"rl_{uuid.uuid4().hex[:8]}@example.com", "password": "WrongPass123"}
    for _ in range(LIMIT + 1):
        r = await strict_client.post("/auth/login", json=body)
    msg = r.json()["message_ar"]
    assert "ثانية" in msg, f"الرسالة لا تقول للمستخدم متى يعيد المحاولة: {msg}"


# ---------------------------------------------------------------------------
# الحد العام وحد السؤال
#
# كانا معرَّفين ولا يُطبَّقان على أي مسار: الإعداد موجود والدالة موجودة
# والحماية غائبة. هذه الاختبارات تُثبت التطبيق نفسه لا وجود الكود.

@pytest_asyncio.fixture
async def strict_general(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "rate_limit_general_per_minute", LIMIT, raising=False)
    monkeypatch.setattr(settings, "rate_limit_ask_per_minute", LIMIT, raising=False)

    from redis.asyncio import from_url
    client = from_url(settings.redis_url)
    for pattern in ("rl:general:*", "rl:ask:*"):
        keys = [k async for k in client.scan_iter(match=pattern)]
        if keys:
            await client.delete(*keys)
    await client.aclose()

    await dispose_engine()
    await create_all()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    await dispose_engine()


@pytest.mark.asyncio
async def test_listing_datasets_is_limited(strict_general):
    """أي مسار يخصّ بيانات المستخدم محكوم بالحد العام."""
    r = await strict_general.post(
        "/auth/register",
        json={"email": f"g_{uuid.uuid4().hex[:8]}@example.com", "password": "StrongPass123"})
    auth = {"Authorization": f"Bearer {r.json()['access_token']}"}

    codes = [(await strict_general.get("/datasets", headers=auth)).status_code
             for _ in range(LIMIT + 2)]
    assert 429 in codes, f"الحد العام غير مطبَّق: {codes}"


@pytest.mark.asyncio
async def test_health_is_not_rate_limited(strict_general):
    """المراقبة تنادي /health كل ثوانٍ — حدُّها يعني إسكات المراقبة."""
    codes = [(await strict_general.get("/health")).status_code
             for _ in range(LIMIT + 3)]
    assert 429 not in codes, f"فحص الصحة محدود: {codes}"
