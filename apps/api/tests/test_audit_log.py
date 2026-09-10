"""سجل التدقيق — ادّعاء في STATUS يستحق التحقق.

سجل تدقيق لا يُكتب فعلاً أسوأ من غيابه: عند التحقيق في حادثة تظنّ أن لديك
أثراً وليس لديك. هذا الملف يتحقق أن كل عملية حساسة تترك أثرها فعلاً، وأن
الأثر يحمل ما يلزم للتحقيق (من، ماذا، متى، من أي عنوان).
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "packages" / "data-engine"))

from apps.api.src.db.models import AuditLog  # noqa: E402
from apps.api.src.db.session import (create_all, dispose_engine,  # noqa: E402
                                     get_sessionmaker)
from apps.api.src.main import app  # noqa: E402
from apps.api.src.services.dataset_service import run_processing_job  # noqa: E402

FIXTURE = ROOT / "packages" / "data-engine" / "fixtures" / "sales_ar_messy.xlsx"


@pytest_asyncio.fixture
async def client():
    await dispose_engine()
    await create_all()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    await dispose_engine()


async def _actions_for(user_id: str) -> list[str]:
    async with get_sessionmaker()() as db:
        rows = (await db.execute(
            select(AuditLog).where(AuditLog.user_id == user_id)
            .order_by(AuditLog.created_at))).scalars().all()
        return [r.action for r in rows]


class TestSensitiveActionsLeaveATrace:
    @pytest.mark.asyncio
    async def test_the_whole_lifecycle_is_recorded(self, client):
        email = f"a_{uuid.uuid4().hex[:10]}@example.com"
        r = await client.post("/auth/register",
                              json={"email": email, "password": "StrongPass123"})
        auth = {"Authorization": f"Bearer {r.json()['access_token']}"}
        user_id = (await client.get("/auth/me", headers=auth)).json()["id"]

        await client.post("/auth/login", json={"email": email, "password": "StrongPass123"})
        await client.post("/auth/login", json={"email": email, "password": "WrongPass999"})

        with FIXTURE.open("rb") as fh:
            r = await client.post("/datasets", headers=auth,
                                  files={"file": (FIXTURE.name, fh)})
        ds_id = r.json()["dataset_id"]
        async with get_sessionmaker()() as db:
            await run_processing_job(db, r.json()["job_id"])
        await client.delete(f"/datasets/{ds_id}", headers=auth)

        actions = await _actions_for(user_id)
        for expected in ("auth.register", "auth.login", "auth.login_failed",
                         "dataset.upload", "dataset.delete"):
            assert expected in actions, f"لم يُسجَّل: {expected} — الموجود {actions}"

    @pytest.mark.asyncio
    async def test_an_entry_carries_what_an_investigation_needs(self, client):
        """سطر بلا وقت أو عنوان أو مورد لا يُجيب عن سؤال تحقيق واحد."""
        email = f"a_{uuid.uuid4().hex[:10]}@example.com"
        r = await client.post("/auth/register",
                              json={"email": email, "password": "StrongPass123"})
        auth = {"Authorization": f"Bearer {r.json()['access_token']}"}
        user_id = (await client.get("/auth/me", headers=auth)).json()["id"]

        async with get_sessionmaker()() as db:
            entry = (await db.execute(
                select(AuditLog).where(AuditLog.user_id == user_id))).scalars().first()

        assert entry is not None
        assert entry.action
        assert entry.created_at is not None
        assert entry.ip, "بلا عنوان IP لا يمكن ربط الحدث بمصدره"
        assert entry.resource, "بلا مورد لا نعرف على ماذا وقع الفعل"

    @pytest.mark.asyncio
    async def test_a_failed_login_is_recorded_even_for_an_unknown_email(self, client):
        """محاولات على بريد غير مسجَّل هي بالضبط ما يكشف الهجوم."""
        async with get_sessionmaker()() as db:
            before = len((await db.execute(
                select(AuditLog).where(AuditLog.action == "auth.login_failed")
            )).scalars().all())

        await client.post("/auth/login",
                          json={"email": f"ghost_{uuid.uuid4().hex[:8]}@example.com",
                                "password": "WhateverPass1"})

        async with get_sessionmaker()() as db:
            after = len((await db.execute(
                select(AuditLog).where(AuditLog.action == "auth.login_failed")
            )).scalars().all())
        assert after == before + 1
