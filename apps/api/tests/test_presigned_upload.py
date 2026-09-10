"""الرفع المباشر (presigned) — المسار الذي يمنع مرور ملف 100MB عبر الـAPI.

ما يستحق الاختبار هنا ثلاثة: العقد (رابط ثم تأكيد)، الأمان (تذكرة غير قابلة
لإعادة الاستخدام أو الاستخدام من مستخدم آخر)، والصدق (لا نبدأ معالجة لملف لم
يصل فعلاً إلى التخزين).
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


async def _ticket(client, auth, size: int | None = None) -> dict:
    r = await client.post("/datasets/upload-url", headers=auth,
                          json={"filename": FIXTURE.name,
                                "size": size if size is not None else FIXTURE.stat().st_size})
    assert r.status_code == 201, r.text
    return r.json()


async def _put(client, url: str, data: bytes):
    return await client.put(url, content=data)


class TestPresignedFlow:
    @pytest.mark.asyncio
    async def test_full_flow_ends_with_a_ready_dataset(self, client):
        auth = await _user(client)
        info = await _ticket(client, auth)
        assert info["method"] == "PUT"

        r = await _put(client, info["url"], FIXTURE.read_bytes())
        assert r.status_code == 204, r.text

        r = await client.post(f"/datasets/{info['dataset_id']}/complete", headers=auth)
        assert r.status_code == 202, r.text
        job_id = r.json()["job_id"]

        async with get_sessionmaker()() as db:
            await run_processing_job(db, job_id)

        r = await client.get(f"/datasets/{info['dataset_id']}", headers=auth)
        body = r.json()
        assert body["status"] == "ready", body
        assert body["row_count"] and body["row_count"] > 0
        assert body["file_size"] == FIXTURE.stat().st_size

    @pytest.mark.asyncio
    async def test_complete_before_upload_is_refused(self, client):
        """الصدق: لا وظيفة معالجة لملف لم يصل. الفشل هنا كان سيظهر لاحقاً وغامضاً."""
        auth = await _user(client)
        info = await _ticket(client, auth)
        r = await client.post(f"/datasets/{info['dataset_id']}/complete", headers=auth)
        assert r.status_code == 409
        assert r.json()["error_code"] == "upload_missing"

    @pytest.mark.asyncio
    async def test_ticket_cannot_be_reused_after_upload(self, client):
        """قاعدة ذهبية #3: RAW يُكتب مرة واحدة ولا يُعدَّل."""
        auth = await _user(client)
        info = await _ticket(client, auth)
        assert (await _put(client, info["url"], FIXTURE.read_bytes())).status_code == 204
        again = await _put(client, info["url"], b"different content")
        assert again.status_code == 409
        assert again.json()["error_code"] == "already_uploaded"

    @pytest.mark.asyncio
    async def test_forged_ticket_is_rejected(self, client):
        r = await _put(client, "/uploads/not-a-real-token", b"data")
        assert r.status_code == 401
        assert r.json()["error_code"] == "invalid_upload_ticket"

    @pytest.mark.asyncio
    async def test_access_token_is_not_accepted_as_an_upload_ticket(self, client):
        """توكن الدخول لا يصلح تذكرةَ رفع — لكل نوع دوره."""
        auth = await _user(client)
        access = auth["Authorization"].split(" ", 1)[1]
        r = await _put(client, f"/uploads/{access}", b"data")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_size_over_the_limit_is_refused_before_any_byte_is_sent(self, client):
        """المكسب الحقيقي: الرفض يحدث عند طلب الرابط، لا بعد رفع 100 ميغابايت."""
        auth = await _user(client)
        r = await client.post("/datasets/upload-url", headers=auth,
                              json={"filename": "big.csv", "size": 10_000 * 1024 * 1024})
        assert r.status_code == 413
        assert r.json()["error_code"] == "file_too_large"

    @pytest.mark.asyncio
    async def test_empty_body_is_refused(self, client):
        auth = await _user(client)
        info = await _ticket(client, auth)
        r = await _put(client, info["url"], b"")
        assert r.status_code == 400
        assert r.json()["error_code"] == "empty_file"

    @pytest.mark.asyncio
    async def test_reserved_dataset_is_hidden_until_content_arrives(self, client):
        """سجل محجوز بلا محتوى يجب ألا يظهر بقائمة المستخدم كصف لا يفتح."""
        auth = await _user(client)
        info = await _ticket(client, auth)
        r = await client.get("/datasets", headers=auth)
        assert [d["id"] for d in r.json()] == []

        await _put(client, info["url"], FIXTURE.read_bytes())
        await client.post(f"/datasets/{info['dataset_id']}/complete", headers=auth)
        r = await client.get("/datasets", headers=auth)
        assert [d["id"] for d in r.json()] == [info["dataset_id"]]

    @pytest.mark.asyncio
    async def test_double_complete_returns_the_same_job(self, client):
        """شبكة متقطّعة تعيد التأكيد — يجب ألا تُنشئ وظيفتين على نفس الملف."""
        auth = await _user(client)
        info = await _ticket(client, auth)
        await _put(client, info["url"], FIXTURE.read_bytes())
        first = await client.post(f"/datasets/{info['dataset_id']}/complete", headers=auth)
        second = await client.post(f"/datasets/{info['dataset_id']}/complete", headers=auth)
        assert first.json()["job_id"] == second.json()["job_id"]

    @pytest.mark.asyncio
    async def test_another_user_cannot_complete_my_upload(self, client):
        auth = await _user(client)
        info = await _ticket(client, auth)
        await _put(client, info["url"], FIXTURE.read_bytes())
        intruder = await _user(client)
        r = await client.post(f"/datasets/{info['dataset_id']}/complete", headers=intruder)
        assert r.status_code == 404


class TestDuplicateDetection:
    @pytest.mark.asyncio
    async def test_same_file_twice_is_flagged_not_blocked(self, client):
        """البصمة تُحسب في العامل الآن — يجب أن يظل كشف التكرار يعمل."""
        auth = await _user(client)
        ids = []
        for _ in range(2):
            info = await _ticket(client, auth)
            await _put(client, info["url"], FIXTURE.read_bytes())
            r = await client.post(f"/datasets/{info['dataset_id']}/complete", headers=auth)
            async with get_sessionmaker()() as db:
                await run_processing_job(db, r.json()["job_id"])
            ids.append(info["dataset_id"])

        second = (await client.get(f"/datasets/{ids[1]}", headers=auth)).json()
        assert second["status"] == "ready"          # لا نمنع الرفع
        assert second["duplicate_of"] == ids[0]     # لكن نُخبر المستخدم
