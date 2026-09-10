"""تصليب المصادقة: لا تجميد لحلقة الأحداث، ولا تسريب عبر الزمن.

المصدر: اختبار الحِمل. وسيط زمن التسجيل كان 3.7 ثانية مع 30 مستخدماً
متزامناً لأن Argon2 كان يُنفَّذ داخل حلقة الأحداث فيجمّد كل الطلبات.
"""
from __future__ import annotations

import asyncio
import sys
import time
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "packages" / "data-engine"))

from apps.api.src.db.session import create_all, dispose_engine  # noqa: E402
from apps.api.src.main import app  # noqa: E402
from apps.api.src.security import auth as auth_mod  # noqa: E402


@pytest_asyncio.fixture
async def client():
    await dispose_engine()
    await create_all()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    await dispose_engine()


def _email() -> str:
    return f"user_{uuid.uuid4().hex[:10]}@example.com"


class TestEventLoopIsNotBlocked:
    @pytest.mark.asyncio
    async def test_other_requests_proceed_while_a_password_is_hashed(self, client):
        """المقياس: كم طلباً آخر ينجح **أثناء** تنفيذ التجزئة؟

        نعدّ بدل أن نقيس زمناً: القياس الزمني حسّاس لضجيج الآلة، أما العدّ
        فالفرق فيه هائل — حلقة حرّة تخدم عشرات الطلبات خلال 175ms، وحلقة
        مجمّدة لا تخدم شيئاً تقريباً.
        """
        # إحماء: أول طلب في العملية يدفع كلفة تهيئة لا علاقة لها بالتجزئة
        for _ in range(3):
            await client.get("/health")

        served = 0
        register = asyncio.create_task(client.post(
            "/auth/register", json={"email": _email(), "password": "StrongPass123"}))
        # نمنح المهمة فرصة البدء فعلياً قبل أن نبدأ العدّ
        await asyncio.sleep(0)
        while not register.done():
            await client.get("/health")
            served += 1
        r = await register

        assert r.status_code == 201
        # على هذه الآلة: ~30 طلباً عند التنفيذ في مجمّع خيوط، و0–1 عند
        # التنفيذ داخل الحلقة. عتبة 5 بعيدة عن الحالتين معاً.
        assert served >= 5, (
            f"الحلقة تجمّدت أثناء التجزئة — لم يُخدَم سوى {served} طلب")


    @pytest.mark.asyncio
    async def test_hashing_runs_in_a_bounded_pool_not_the_default_one(self):
        """المجمّع الافتراضي يصل إلى 32 خيطاً — 30 تجزئة عليه تُبطئ الجميع."""
        workers = auth_mod._HASH_POOL._max_workers
        assert 2 <= workers <= 8, f"حجم مجمّع التجزئة غير معقول: {workers}"


class TestLoginDoesNotLeakWhichEmailsExist:
    @pytest.mark.asyncio
    async def test_unknown_email_still_performs_a_verification(self, client, monkeypatch):
        """الرد الفوري على بريد غير موجود يكشف — بالزمن — أي بريد مسجّل."""
        calls: list[str] = []
        real = auth_mod.verify_password

        def counting(raw: str, hashed: str) -> bool:
            calls.append(hashed)
            return real(raw, hashed)

        monkeypatch.setattr(auth_mod, "verify_password", counting)
        r = await client.post("/auth/login",
                              json={"email": _email(), "password": "StrongPass123"})
        assert r.status_code == 401
        assert len(calls) == 1, "لم تُنفَّذ أي تجزئة لبريد غير موجود — تسريب زمني"

    @pytest.mark.asyncio
    async def test_same_message_for_wrong_email_and_wrong_password(self, client):
        email = _email()
        await client.post("/auth/register", json={"email": email, "password": "StrongPass123"})
        a = await client.post("/auth/login", json={"email": email, "password": "WrongPass123"})
        b = await client.post("/auth/login", json={"email": _email(), "password": "WrongPass123"})
        assert a.status_code == b.status_code == 401
        assert a.json()["error_code"] == b.json()["error_code"] == "bad_credentials"
        assert a.json()["message_ar"] == b.json()["message_ar"]
