"""تصحيح المستخدم لدور عمود — الخطة §5.4 تجعل هذه الواجهة إجبارية.

الجوهر المُختبَر: هل يغيّر التصحيح **الأرقام** فعلاً، أم يغيّر التسمية فقط؟
لو غيّر التسمية وحدها لكان أسوأ من لا شيء: مستخدم يظن أنه صحّح، والأرقام
ما زالت محسوبة على فهم خاطئ.
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


async def _ready_dataset(client, auth) -> str:
    with FIXTURE.open("rb") as fh:
        r = await client.post("/datasets", headers=auth, files={"file": (FIXTURE.name, fh)})
    body = r.json()
    async with get_sessionmaker()() as db:
        await run_processing_job(db, body["job_id"])
    return body["dataset_id"]


async def _job_count(dataset_id: str) -> int:
    from sqlalchemy import func, select

    from apps.api.src.db.models import Job
    async with get_sessionmaker()() as db:
        return (await db.execute(select(func.count()).select_from(Job)
                                 .where(Job.dataset_id == dataset_id))).scalar_one()


class TestConcepts:
    @pytest.mark.asyncio
    async def test_concepts_come_from_the_engine_dictionary(self, client):
        """الواجهة لا تخترع خيارات: القائمة مصدرها قاموس المحرك."""
        auth = await _user(client)
        r = await client.get("/datasets/concepts", headers=auth)
        assert r.status_code == 200
        concepts = {c["concept"] for c in r.json()}
        assert {"total_amount", "quantity", "unit_price", "date"} <= concepts
        for c in r.json():
            assert c["label_ar"], "كل مفهوم يحتاج تسمية عربية للعرض"

    @pytest.mark.asyncio
    async def test_concepts_route_is_not_swallowed_by_the_id_route(self, client):
        """فخ ترتيب المسارات: /datasets/concepts كان يُقرأ كمعرّف ملف."""
        auth = await _user(client)
        r = await client.get("/datasets/concepts", headers=auth)
        assert r.status_code != 404


class TestSchemaOverride:
    @pytest.mark.asyncio
    async def test_override_changes_the_numbers_not_just_the_label(self, client):
        auth = await _user(client)
        ds_id = await _ready_dataset(client, auth)

        schema = (await client.get(f"/datasets/{ds_id}/schema", headers=auth)).json()
        qty = next(c for c in schema["columns"] if c["concept"] == "quantity")
        def kpi_values(dash: dict) -> dict:
            """قيم المؤشرات وحدها — بلا أختام وقت، وإلا نجح الاختبار مجاناً."""
            return {k["label_ar"]: k.get("value") for k in dash["kpis"]}

        before = kpi_values(
            (await client.get(f"/datasets/{ds_id}/dashboard", headers=auth)).json())

        # نُعلن أن عمود الكمية هو في الحقيقة سعر الوحدة — تغيير جذري في المعنى
        r = await client.patch(
            f"/datasets/{ds_id}/schema", headers=auth,
            json={"columns": [{"column_name": qty["column_name"], "concept": "unit_price"}]},
        )
        assert r.status_code == 202, r.text
        async with get_sessionmaker()() as db:
            await run_processing_job(db, r.json()["job_id"])

        after_schema = (await client.get(f"/datasets/{ds_id}/schema", headers=auth)).json()
        col = next(c for c in after_schema["columns"] if c["column_name"] == qty["column_name"])
        assert col["concept"] == "unit_price"
        assert col["detection_method"] == "user"
        assert col["user_overridden"] is True
        assert col["confidence"] == 1.0

        after = kpi_values(
            (await client.get(f"/datasets/{ds_id}/dashboard", headers=auth)).json())
        assert after != before, f"التصحيح لم يغيّر الأرقام: {before} == {after}"

    @pytest.mark.asyncio
    async def test_override_survives_a_later_reprocess(self, client):
        """قرار المستخدم لا يضيع: كل معالجة لاحقة تعيد تطبيقه."""
        auth = await _user(client)
        ds_id = await _ready_dataset(client, auth)
        schema = (await client.get(f"/datasets/{ds_id}/schema", headers=auth)).json()
        target = schema["columns"][0]["column_name"]

        r = await client.patch(f"/datasets/{ds_id}/schema", headers=auth,
                               json={"columns": [{"column_name": target, "concept": "customer"}]})
        async with get_sessionmaker()() as db:
            await run_processing_job(db, r.json()["job_id"])

        # معالجة أخرى بلا أي تصحيح جديد
        r2 = await client.patch(f"/datasets/{ds_id}/schema", headers=auth,
                                json={"columns": [{"column_name": target, "concept": "customer"}]})
        async with get_sessionmaker()() as db:
            await run_processing_job(db, r2.json()["job_id"])

        after = (await client.get(f"/datasets/{ds_id}/schema", headers=auth)).json()
        col = next(c for c in after["columns"] if c["column_name"] == target)
        assert col["concept"] == "customer"
        assert col["user_overridden"] is True

    @pytest.mark.asyncio
    async def test_clearing_a_concept_is_allowed(self, client):
        """«هذا العمود لا يعني شيئاً» قرار صالح ويجب أن يُحترم."""
        auth = await _user(client)
        ds_id = await _ready_dataset(client, auth)
        schema = (await client.get(f"/datasets/{ds_id}/schema", headers=auth)).json()
        target = next(c for c in schema["columns"] if c["concept"])["column_name"]

        r = await client.patch(f"/datasets/{ds_id}/schema", headers=auth,
                               json={"columns": [{"column_name": target, "concept": None}]})
        assert r.status_code == 202
        async with get_sessionmaker()() as db:
            await run_processing_job(db, r.json()["job_id"])

        after = (await client.get(f"/datasets/{ds_id}/schema", headers=auth)).json()
        col = next(c for c in after["columns"] if c["column_name"] == target)
        assert col["concept"] is None
        assert col["user_overridden"] is True

    @pytest.mark.asyncio
    async def test_unknown_concept_is_refused(self, client):
        auth = await _user(client)
        ds_id = await _ready_dataset(client, auth)
        schema = (await client.get(f"/datasets/{ds_id}/schema", headers=auth)).json()
        r = await client.patch(
            f"/datasets/{ds_id}/schema", headers=auth,
            json={"columns": [{"column_name": schema["columns"][0]["column_name"],
                               "concept": "مفهوم_مخترع"}]})
        assert r.status_code == 400
        assert r.json()["error_code"] == "unknown_concept"

    @pytest.mark.asyncio
    async def test_unknown_column_is_refused(self, client):
        auth = await _user(client)
        ds_id = await _ready_dataset(client, auth)
        r = await client.patch(f"/datasets/{ds_id}/schema", headers=auth,
                               json={"columns": [{"column_name": "عمود_غير_موجود",
                                                  "concept": "quantity"}]})
        assert r.status_code == 400
        assert r.json()["error_code"] == "unknown_column"

    @pytest.mark.asyncio
    async def test_another_user_cannot_patch_my_schema(self, client):
        auth = await _user(client)
        ds_id = await _ready_dataset(client, auth)
        intruder = await _user(client)
        r = await client.patch(f"/datasets/{ds_id}/schema", headers=intruder,
                               json={"columns": [{"column_name": "x", "concept": "quantity"}]})
        assert r.status_code == 404


class TestConcurrentReprocessRequests:
    """نقرة مزدوجة على «احفظ وأعد الحساب» — أو إعادة إرسال من شبكة متقطّعة."""

    @pytest.mark.asyncio
    async def test_two_simultaneous_patches_create_one_job_only(self, client):
        """الفحص ثم الكتابة غير آمن: طلبان يقرآن «ready» معاً.

        جُرِّب فعلياً قبل الإصلاح: نتج **وظيفتا معالجة** على الملف نفسه —
        عاملان يحذفان ويكتبان صفوف الـschema بالتناوب.
        """
        import asyncio

        from sqlalchemy import func, select

        from apps.api.src.db.models import Job

        auth = await _user(client)
        ds_id = await _ready_dataset(client, auth)
        before = await _job_count(ds_id)

        schema = (await client.get(f"/datasets/{ds_id}/schema", headers=auth)).json()
        body = {"columns": [{"column_name": schema["columns"][0]["column_name"],
                             "concept": "customer"}]}
        r1, r2 = await asyncio.gather(
            client.patch(f"/datasets/{ds_id}/schema", headers=auth, json=body),
            client.patch(f"/datasets/{ds_id}/schema", headers=auth, json=body),
        )

        assert sorted([r1.status_code, r2.status_code]) == [202, 409]
        refused = r1 if r1.status_code == 409 else r2
        assert refused.json()["error_code"] == "already_processing"
        assert await _job_count(ds_id) == before + 1, "أُنشئت أكثر من وظيفة"
        _ = (Job, func, select)

    @pytest.mark.asyncio
    async def test_a_second_request_after_the_first_finishes_is_accepted(self, client):
        """الحد المقابل: الحجز يُفرج عنه بانتهاء المعالجة."""
        auth = await _user(client)
        ds_id = await _ready_dataset(client, auth)
        schema = (await client.get(f"/datasets/{ds_id}/schema", headers=auth)).json()
        body = {"columns": [{"column_name": schema["columns"][0]["column_name"],
                             "concept": "customer"}]}

        r = await client.patch(f"/datasets/{ds_id}/schema", headers=auth, json=body)
        assert r.status_code == 202
        async with get_sessionmaker()() as db:
            await run_processing_job(db, r.json()["job_id"])

        r2 = await client.patch(f"/datasets/{ds_id}/schema", headers=auth, json=body)
        assert r2.status_code == 202
