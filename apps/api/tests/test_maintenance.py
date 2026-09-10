"""الصيانة الدورية: الرفعات المهجورة وسياسة الاحتفاظ.

الخطر الحقيقي هنا ليس أن تفشل الصيانة، بل أن تحذف أكثر مما يجب. لذلك أكثر
الاختبارات أدناه تتحقق مما **يبقى**، لا مما يُحذف.
"""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "packages" / "data-engine"))

from apps.api.src.config import get_settings  # noqa: E402
from apps.api.src.db.models import Dataset  # noqa: E402
from apps.api.src.db.session import (create_all, dispose_engine,  # noqa: E402
                                     get_sessionmaker)
from apps.api.src.main import app  # noqa: E402
from apps.api.src.services.dataset_service import run_processing_job  # noqa: E402
from apps.api.src.services.maintenance import (apply_retention,  # noqa: E402
                                               purge_abandoned_uploads)
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


async def _reserved(client, auth) -> str:
    """سجل رفع محجوز بلا محتوى."""
    r = await client.post("/datasets/upload-url", headers=auth,
                          json={"filename": "x.csv", "size": 100})
    return r.json()["dataset_id"]


async def _ready(client, auth) -> str:
    with FIXTURE.open("rb") as fh:
        r = await client.post("/datasets", headers=auth, files={"file": (FIXTURE.name, fh)})
    body = r.json()
    async with get_sessionmaker()() as db:
        await run_processing_job(db, body["job_id"])
    return body["dataset_id"]


async def _age(dataset_id: str, hours: float) -> None:
    """يُقدّم عمر السجل بدل انتظار الزمن الحقيقي."""
    async with get_sessionmaker()() as db:
        await db.execute(update(Dataset).where(Dataset.id == dataset_id)
                         .values(created_at=datetime.now(timezone.utc)
                                 - timedelta(hours=hours)))
        await db.commit()


async def _exists(dataset_id: str) -> bool:
    async with get_sessionmaker()() as db:
        return await db.get(Dataset, dataset_id) is not None


class TestAbandonedUploads:
    @pytest.mark.asyncio
    async def test_old_reserved_record_is_purged(self, client):
        auth = await _user(client)
        ds_id = await _reserved(client, auth)
        await _age(ds_id, 48)

        async with get_sessionmaker()() as db:
            await purge_abandoned_uploads(db, hours=24)
        assert not await _exists(ds_id)

    @pytest.mark.asyncio
    async def test_recent_reserved_record_is_left_alone(self, client):
        """المستخدم قد يكون يرفع الآن — الحذف المتعجّل يُفقده ملفه."""
        auth = await _user(client)
        ds_id = await _reserved(client, auth)
        async with get_sessionmaker()() as db:
            await purge_abandoned_uploads(db, hours=24)
        assert await _exists(ds_id)

    @pytest.mark.asyncio
    async def test_a_ready_dataset_is_never_touched_however_old(self, client):
        """أهم اختبار هنا: ملف قديم ومعالَج ليس «رفعاً مهجوراً»."""
        auth = await _user(client)
        ds_id = await _ready(client, auth)
        await _age(ds_id, 24 * 365)

        async with get_sessionmaker()() as db:
            await purge_abandoned_uploads(db, hours=24)
        assert await _exists(ds_id)

    @pytest.mark.asyncio
    async def test_purge_removes_the_stored_object_too(self, client):
        auth = await _user(client)
        info = (await client.post("/datasets/upload-url", headers=auth,
                                  json={"filename": "x.csv",
                                        "size": 5})).json()
        await client.put(info["url"], content=b"a,b\n1,2\n")
        ds_id = info["dataset_id"]
        async with get_sessionmaker()() as db:
            key = (await db.get(Dataset, ds_id)).raw_key
        settings = get_settings()
        assert get_storage().exists(settings.s3_bucket_raw, key)

        await _age(ds_id, 48)
        async with get_sessionmaker()() as db:
            await purge_abandoned_uploads(db, hours=24)

        assert not get_storage().exists(settings.s3_bucket_raw, key), "الكائن بقي يتيماً"


class TestRetention:
    @pytest.mark.asyncio
    async def test_disabled_by_default_nothing_is_deleted(self, client):
        """الافتراضي 0 = بلا حذف تلقائي. حذف بيانات المستخدم بالسهو غير مقبول."""
        assert get_settings().retention_days == 0
        auth = await _user(client)
        ds_id = await _ready(client, auth)
        await _age(ds_id, 24 * 3650)
        async with get_sessionmaker()() as db:
            assert await apply_retention(db) == 0, "الاحتفاظ معطّل ومع ذلك حُذف شيء"
        assert await _exists(ds_id)

    @pytest.mark.asyncio
    async def test_when_enabled_only_older_than_the_window_is_removed(self, client):
        auth = await _user(client)
        old = await _ready(client, auth)
        fresh = await _ready(client, auth)
        await _age(old, 24 * 40)

        # نتحقق من الهوية لا من العدد: قاعدة الاختبار مشتركة بين الاختبارات
        # وقد تحوي سجلات قديمة من غيرها، فالعدّ يقيس ما لا نقصده.
        async with get_sessionmaker()() as db:
            await apply_retention(db, days=30)
        assert not await _exists(old)
        assert await _exists(fresh), "حُذف ملف داخل مدة الاحتفاظ"
