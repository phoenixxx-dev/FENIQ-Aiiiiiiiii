"""بث التقدّم اللحظي (SSE) — /jobs/{id}/stream.

ثلاثة أشياء تستحق الاختبار هنا:
  1. الأمان: بث وظيفة مستخدم آخر يجب أن يكون 404 لا بثاً فارغاً.
  2. الوظيفة المنتهية: تُغلق فوراً بحدث done — لا تُبقي الاتصال معلّقاً.
  3. الشكل: أحداث SSE صالحة، والعربية تصل كما هي لا كرموز \\uXXXX.
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


async def _user(client) -> dict:
    email = f"user_{uuid.uuid4().hex[:10]}@example.com"
    r = await client.post("/auth/register", json={"email": email, "password": "StrongPass123"})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _upload(client, auth) -> str:
    with FIXTURE.open("rb") as fh:
        r = await client.post("/datasets", headers=auth, files={"file": (FIXTURE.name, fh)})
    assert r.status_code == 202, r.text
    return r.json()["job_id"]


def _events(text: str) -> list[tuple[str, str]]:
    """يفكّ نص SSE إلى (اسم الحدث، البيانات)."""
    out = []
    for block in text.strip().split("\n\n"):
        name = data = ""
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = line[6:]
        if name:
            out.append((name, data))
    return out


class TestJobStream:
    @pytest.mark.asyncio
    async def test_other_users_job_is_404_not_an_empty_stream(self, client):
        """الأمان أولاً: لو بدأ البث ثم رفض، لصار الرفض 200 في نظر العميل."""
        job_id = await _upload(client, await _user(client))
        intruder = await _user(client)
        r = await client.get(f"/jobs/{job_id}/stream", headers=intruder)
        assert r.status_code == 404
        assert r.json()["error_code"] == "job_not_found"

    @pytest.mark.asyncio
    async def test_finished_job_closes_immediately_with_done(self, client):
        auth = await _user(client)
        job_id = await _upload(client, auth)
        async with get_sessionmaker()() as db:
            await run_processing_job(db, job_id)

        r = await client.get(f"/jobs/{job_id}/stream", headers=auth)
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")

        events = _events(r.text)
        names = [n for n, _ in events]
        assert names == ["progress", "done"], names
        payload = events[-1][1]
        assert '"status": "succeeded"' in payload
        assert '"progress": 100' in payload

    @pytest.mark.asyncio
    async def test_arabic_stage_text_arrives_readable(self, client):
        """العربية داخل الـSSE يجب أن تصل حرفاً لا \\u0627 — الواجهة تعرضها مباشرة."""
        auth = await _user(client)
        job_id = await _upload(client, auth)
        async with get_sessionmaker()() as db:
            await run_processing_job(db, job_id)
        r = await client.get(f"/jobs/{job_id}/stream", headers=auth)
        assert "جاهز" in r.text, r.text[:300]
        assert "\\u" not in r.text

    @pytest.mark.asyncio
    async def test_unauthenticated_stream_is_rejected(self, client):
        job_id = await _upload(client, await _user(client))
        r = await client.get(f"/jobs/{job_id}/stream")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_stream_sees_progress_of_a_job_running_elsewhere(self, client):
        """الاختبار الحقيقي: هل يرى البثُّ تقدّماً تكتبه معاملة أخرى؟

        هذه بالضبط الحالة التي تكسر التنفيذ الساذج: لو أعاد البث استخدام
        جلسة واحدة، لبقي يقرأ لقطة معاملته الأولى ولما تغيّر شيء أبداً.
        """
        import asyncio

        auth = await _user(client)
        job_id = await _upload(client, auth)

        async def process() -> None:
            async with get_sessionmaker()() as db:
                await run_processing_job(db, job_id)

        task = asyncio.create_task(process())
        try:
            r = await client.get(f"/jobs/{job_id}/stream", headers=auth)
            events = _events(r.text)
        finally:
            await task

        names = [n for n, _ in events]
        assert names[0] == "progress"
        assert names[-1] == "done", names
        # الشرط الذي يمنع النجاح الزائف: اللقطة الأولى **لم تكن** منتهية،
        # فالانتقال إلى succeeded رآه البث من معاملة أخرى فعلاً.
        assert '"status": "succeeded"' not in events[0][1], events[0][1]
        assert len(events) >= 2
        assert '"status": "succeeded"' in events[-1][1]
