"""«ماذا تغيّر في بياناتي؟» + رفض عملية تنظيف.

التنظيف اقتراح لا حكم. المُختبَر: هل يُعيد الرفضُ الحسابَ فعلاً، أم يخفي
سطراً من السجل ويترك البيانات كما هي؟
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
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _ready(client, auth) -> str:
    with FIXTURE.open("rb") as fh:
        r = await client.post("/datasets", headers=auth, files={"file": (FIXTURE.name, fh)})
    body = r.json()
    async with get_sessionmaker()() as db:
        await run_processing_job(db, body["job_id"])
    return body["dataset_id"]


def _effective(changelog: dict) -> dict[str, int]:
    return {r["operation_type"] + "|" + ",".join(r["columns_affected"]): r["cells_changed"]
            for r in changelog["results"]}


class TestCleaningView:
    @pytest.mark.asyncio
    async def test_changelog_shows_real_before_after_examples(self, client):
        """الشاشة بلا أمثلة ملموسة لا تبني ثقة — نتحقق أن الأمثلة حقيقية."""
        auth = await _user(client)
        ds_id = await _ready(client, auth)
        body = (await client.get(f"/datasets/{ds_id}/cleaning", headers=auth)).json()

        assert body["disabled"] == []
        results = body["changelog"]["results"]
        assert results, "سجل التغييرات فارغ"
        with_examples = [r for r in results if r["examples"]]
        assert with_examples, "لا يوجد ولا مثال قبل/بعد"
        ex = with_examples[0]["examples"][0]
        assert {"row", "column", "before", "after"} <= set(ex)
        assert ex["before"] != ex["after"]

    @pytest.mark.asyncio
    async def test_every_operation_has_a_stable_key(self, client):
        auth = await _user(client)
        ds_id = await _ready(client, auth)
        body = (await client.get(f"/datasets/{ds_id}/cleaning", headers=auth)).json()
        keys = [op["key"] for op in body["recipe"]["operations"]]
        assert all(keys)
        assert len(keys) == len(set(keys)), "مفاتيح مكرّرة — التعطيل سيصيب عمليتين"


class TestCleaningOverride:
    @pytest.mark.asyncio
    async def test_disabling_an_operation_actually_changes_the_data(self, client):
        auth = await _user(client)
        ds_id = await _ready(client, auth)
        before = (await client.get(f"/datasets/{ds_id}/cleaning", headers=auth)).json()

        # نختار عملية غيّرت شيئاً فعلاً — تعطيل عملية بلا أثر لا يُثبت شيئاً
        active = {r["operation_id"] for r in before["changelog"]["results"]
                  if r["cells_changed"] or r["rows_affected"]}
        target = next(op for op in before["recipe"]["operations"] if op["id"] in active)

        r = await client.patch(f"/datasets/{ds_id}/cleaning", headers=auth,
                               json={"disabled": [target["key"]]})
        assert r.status_code == 202, r.text
        async with get_sessionmaker()() as db:
            await run_processing_job(db, r.json()["job_id"])

        after = (await client.get(f"/datasets/{ds_id}/cleaning", headers=auth)).json()
        assert after["disabled"] == [target["key"]]
        assert _effective(after["changelog"]) != _effective(before["changelog"]), \
            "التعطيل لم يغيّر ما نُفِّذ فعلاً"
        off = next(op for op in after["recipe"]["operations"] if op["key"] == target["key"])
        assert off["enabled"] is False

    @pytest.mark.asyncio
    async def test_re_enabling_restores_the_original_result(self, client):
        """القرار قابل للتراجع: إعادة التفعيل تعيد النتيجة الأولى بالضبط."""
        auth = await _user(client)
        ds_id = await _ready(client, auth)
        original = _effective(
            (await client.get(f"/datasets/{ds_id}/cleaning", headers=auth)).json()["changelog"])
        recipe = (await client.get(f"/datasets/{ds_id}/cleaning", headers=auth)).json()["recipe"]
        key = recipe["operations"][0]["key"]

        r = await client.patch(f"/datasets/{ds_id}/cleaning", headers=auth,
                               json={"disabled": [key]})
        async with get_sessionmaker()() as db:
            await run_processing_job(db, r.json()["job_id"])

        r = await client.patch(f"/datasets/{ds_id}/cleaning", headers=auth, json={"disabled": []})
        async with get_sessionmaker()() as db:
            await run_processing_job(db, r.json()["job_id"])

        restored = _effective(
            (await client.get(f"/datasets/{ds_id}/cleaning", headers=auth)).json()["changelog"])
        assert restored == original

    @pytest.mark.asyncio
    async def test_unknown_operation_key_is_refused(self, client):
        auth = await _user(client)
        ds_id = await _ready(client, auth)
        r = await client.patch(f"/datasets/{ds_id}/cleaning", headers=auth,
                               json={"disabled": ["عملية:غير_موجودة"]})
        assert r.status_code == 400
        assert r.json()["error_code"] == "unknown_operation"

    @pytest.mark.asyncio
    async def test_another_user_cannot_change_my_cleaning(self, client):
        auth = await _user(client)
        ds_id = await _ready(client, auth)
        intruder = await _user(client)
        r = await client.patch(f"/datasets/{ds_id}/cleaning", headers=intruder,
                               json={"disabled": []})
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_raw_file_is_never_touched_by_any_of_this(self, client):
        """قاعدة ذهبية #3: مهما عطّل المستخدم، الملف الأصلي كما هو."""
        from apps.api.src.config import get_settings
        from apps.api.src.storage.client import get_storage

        auth = await _user(client)
        ds_id = await _ready(client, auth)
        meta = (await client.get(f"/datasets/{ds_id}", headers=auth)).json()

        async with get_sessionmaker()() as db:
            from apps.api.src.db.models import Dataset
            raw_key = (await db.get(Dataset, ds_id)).raw_key
        size_before = get_storage().size(get_settings().s3_bucket_raw, raw_key)

        recipe = (await client.get(f"/datasets/{ds_id}/cleaning", headers=auth)).json()["recipe"]
        r = await client.patch(f"/datasets/{ds_id}/cleaning", headers=auth,
                               json={"disabled": [recipe["operations"][0]["key"]]})
        async with get_sessionmaker()() as db:
            await run_processing_job(db, r.json()["job_id"])

        assert get_storage().size(get_settings().s3_bucket_raw, raw_key) == size_before
        assert meta["file_size"] == size_before
