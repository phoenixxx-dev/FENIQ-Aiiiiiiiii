"""حذف الملف — حق صاحب البيانات.

القاعدة الذهبية #3 تمنع **المعالجة** من المساس بالملف الأصلي؛ لا تمنع
المالك من حذفه. المُختبَر هنا أن الحذف كامل فعلاً: لا سجل ولا كائن تخزين
ولا نسخة مؤقتة تبقى بعده.
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

from apps.api.src.config import get_settings  # noqa: E402
from apps.api.src.db.models import (CleaningRecipeRow, Conversation,  # noqa: E402
                                    Dataset, DatasetProfile, DatasetSchema,
                                    InsightRow, Job, Message)
from apps.api.src.db.session import (create_all, dispose_engine,  # noqa: E402
                                     get_sessionmaker)
from apps.api.src.main import app  # noqa: E402
from apps.api.src.services.dataset_service import run_processing_job  # noqa: E402
from apps.api.src.storage.client import get_storage  # noqa: E402

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
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _ready(client, auth) -> str:
    with FIXTURE.open("rb") as fh:
        r = await client.post("/datasets", headers=auth, files={"file": (FIXTURE.name, fh)})
    body = r.json()
    async with get_sessionmaker()() as db:
        await run_processing_job(db, body["job_id"])
    return body["dataset_id"]


async def _count(model, dataset_id: str) -> int:
    async with get_sessionmaker()() as db:
        res = await db.execute(select(func.count()).select_from(model)
                               .where(model.dataset_id == dataset_id))
        return res.scalar_one()


class TestDelete:
    @pytest.mark.asyncio
    async def test_delete_removes_metadata_and_stored_objects(self, client):
        auth = await _user(client)
        ds_id = await _ready(client, auth)
        await client.post(f"/datasets/{ds_id}/ask", headers=auth,
                          json={"question": "كم إجمالي المبيعات؟"})

        async with get_sessionmaker()() as db:
            ds = await db.get(Dataset, ds_id)
            raw_key, processed_key = ds.raw_key, ds.processed_key
        settings = get_settings()
        storage = get_storage()
        assert storage.exists(settings.s3_bucket_raw, raw_key)
        assert storage.exists(settings.s3_bucket_processed, processed_key)

        r = await client.delete(f"/datasets/{ds_id}", headers=auth)
        assert r.status_code == 204, r.text

        assert not storage.exists(settings.s3_bucket_raw, raw_key), "الملف الأصلي بقي"
        assert not storage.exists(settings.s3_bucket_processed, processed_key)
        for model in (DatasetSchema, DatasetProfile, CleaningRecipeRow, InsightRow, Job):
            assert await _count(model, ds_id) == 0, f"{model.__name__} لم تُحذف"
        assert await _count(Conversation, ds_id) == 0
        async with get_sessionmaker()() as db:
            assert await db.get(Dataset, ds_id) is None

    @pytest.mark.asyncio
    async def test_messages_of_the_conversation_are_gone_too(self, client):
        """رسائل المحادثة تشير للمحادثة لا للملف — يسهل نسيانها."""
        auth = await _user(client)
        ds_id = await _ready(client, auth)
        await client.post(f"/datasets/{ds_id}/ask", headers=auth,
                          json={"question": "كم إجمالي المبيعات؟"})
        async with get_sessionmaker()() as db:
            conv = (await db.execute(select(Conversation)
                                     .where(Conversation.dataset_id == ds_id))).scalars().first()
            assert conv is not None
            conv_id = conv.id

        await client.delete(f"/datasets/{ds_id}", headers=auth)

        async with get_sessionmaker()() as db:
            left = (await db.execute(select(func.count()).select_from(Message)
                                     .where(Message.conversation_id == conv_id))).scalar_one()
        assert left == 0, "رسائل المحادثة بقيت بعد حذف الملف"

    @pytest.mark.asyncio
    async def test_deleted_dataset_disappears_from_the_list(self, client):
        auth = await _user(client)
        ds_id = await _ready(client, auth)
        await client.delete(f"/datasets/{ds_id}", headers=auth)
        assert (await client.get("/datasets", headers=auth)).json() == []
        assert (await client.get(f"/datasets/{ds_id}", headers=auth)).status_code == 404

    @pytest.mark.asyncio
    async def test_another_user_cannot_delete_my_dataset(self, client):
        auth = await _user(client)
        ds_id = await _ready(client, auth)
        intruder = await _user(client)
        r = await client.delete(f"/datasets/{ds_id}", headers=intruder)
        assert r.status_code == 404
        assert (await client.get(f"/datasets/{ds_id}", headers=auth)).status_code == 200

    @pytest.mark.asyncio
    async def test_duplicate_reference_survives_deleting_the_original(self, client):
        """حذف الأصل يجب ألا يترك إشارة معلّقة تكسر صفحة الملف الثاني."""
        auth = await _user(client)
        first = await _ready(client, auth)
        second = await _ready(client, auth)
        assert (await client.get(f"/datasets/{second}", headers=auth)).json()["duplicate_of"] == first

        await client.delete(f"/datasets/{first}", headers=auth)
        body = (await client.get(f"/datasets/{second}", headers=auth)).json()
        assert body["duplicate_of"] is None
        assert body["status"] == "ready"

    @pytest.mark.asyncio
    async def test_deleting_twice_is_a_clean_404_not_a_crash(self, client):
        auth = await _user(client)
        ds_id = await _ready(client, auth)
        assert (await client.delete(f"/datasets/{ds_id}", headers=auth)).status_code == 204
        r = await client.delete(f"/datasets/{ds_id}", headers=auth)
        assert r.status_code == 404
        assert r.json()["error_code"] == "dataset_not_found"
