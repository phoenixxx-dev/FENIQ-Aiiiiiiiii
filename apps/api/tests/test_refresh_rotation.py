"""تدوير توكن التجديد — الخطة §9.1.

بلا تدوير: توكن مسروق يعمل 14 يوماً كاملة ولا شيء يكشف ذلك. مع التدوير:
كل توكن يُستعمل مرة واحدة، وإعادة استعماله ترفض **وتُسجَّل** — لأن ورودها
يعني أن نسخة منه ليست بيد صاحبها.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "packages" / "data-engine"))

from apps.api.src.db.models import AuditLog  # noqa: E402
from apps.api.src.db.session import (create_all, dispose_engine,  # noqa: E402
                                     get_sessionmaker)
from apps.api.src.main import app  # noqa: E402


@pytest_asyncio.fixture
async def client():
    await dispose_engine()
    await create_all()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    await dispose_engine()


async def _register(client) -> dict:
    email = f"user_{uuid.uuid4().hex[:10]}@example.com"
    r = await client.post("/auth/register", json={"email": email, "password": "StrongPass123"})
    assert r.status_code == 201
    return r.json()


class TestRotation:
    @pytest.mark.asyncio
    async def test_refresh_returns_a_different_token_each_time(self, client):
        tokens = await _register(client)
        r = await client.post("/auth/refresh",
                              json={"refresh_token": tokens["refresh_token"]})
        assert r.status_code == 200
        assert r.json()["refresh_token"] != tokens["refresh_token"], "لم يُدوَّر التوكن"

    @pytest.mark.asyncio
    async def test_an_old_refresh_token_stops_working(self, client):
        """جوهر الميزة: التوكن المستهلَك لا يُقبل مرة ثانية."""
        tokens = await _register(client)
        first = tokens["refresh_token"]

        assert (await client.post("/auth/refresh",
                                  json={"refresh_token": first})).status_code == 200
        again = await client.post("/auth/refresh", json={"refresh_token": first})
        assert again.status_code == 401
        assert again.json()["error_code"] == "invalid_token"

    @pytest.mark.asyncio
    async def test_the_new_token_still_works(self, client):
        """الحد المقابل: التدوير يجب ألا يقطع الجلسة الشرعية."""
        tokens = await _register(client)
        r = await client.post("/auth/refresh",
                              json={"refresh_token": tokens["refresh_token"]})
        fresh = r.json()["refresh_token"]
        assert (await client.post("/auth/refresh",
                                  json={"refresh_token": fresh})).status_code == 200

    @pytest.mark.asyncio
    async def test_a_chain_of_refreshes_keeps_working(self, client):
        tokens = await _register(client)
        current = tokens["refresh_token"]
        for _ in range(5):
            r = await client.post("/auth/refresh", json={"refresh_token": current})
            assert r.status_code == 200
            current = r.json()["refresh_token"]

    @pytest.mark.asyncio
    async def test_reuse_is_recorded_in_the_audit_log(self, client):
        """إعادة الاستعمال ليست خطأ عابراً — هي مؤشّر تسريب يستحق التسجيل."""
        tokens = await _register(client)
        first = tokens["refresh_token"]
        await client.post("/auth/refresh", json={"refresh_token": first})
        await client.post("/auth/refresh", json={"refresh_token": first})

        async with get_sessionmaker()() as db:
            count = (await db.execute(
                select(func.count()).select_from(AuditLog)
                .where(AuditLog.action == "auth.refresh_reuse"))).scalar_one()
        assert count >= 1, "إعادة استعمال التوكن لم تُسجَّل"

    @pytest.mark.asyncio
    async def test_an_access_token_is_not_accepted_for_refresh(self, client):
        tokens = await _register(client)
        r = await client.post("/auth/refresh",
                              json={"refresh_token": tokens["access_token"]})
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_a_forged_token_is_refused(self, client):
        r = await client.post("/auth/refresh", json={"refresh_token": "not.a.token"})
        assert r.status_code == 401
