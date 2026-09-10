"""حرّاس القواعد الذهبية — اختبارات تفشل عند الخرق، لا نوايا حسنة.

كل اختبار هنا يقابل قاعدة في docs/RULES.md. القاعدة التي بلا حارس تنفيذي
تُخرَق يوماً ما بلا أن ينتبه أحد.
"""
from __future__ import annotations

import ast
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
from apps.api.src.db.models import Dataset  # noqa: E402
from apps.api.src.db.session import (create_all, dispose_engine,  # noqa: E402
                                     get_sessionmaker)
from apps.api.src.main import app  # noqa: E402
from apps.api.src.services.dataset_service import run_processing_job  # noqa: E402
from apps.api.src.storage.client import get_storage  # noqa: E402

ENGINE_SRC = ROOT / "packages" / "data-engine" / "phoenix"
FIXTURE = ROOT / "packages" / "data-engine" / "fixtures" / "sales_ar_messy.xlsx"


@pytest_asyncio.fixture
async def client():
    await dispose_engine()
    await create_all()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    await dispose_engine()


class TestRule4EngineIsAPureLibrary:
    """المحرك لا يعرف الويب ولا قاعدة البيانات ولا التخزين السحابي."""

    # الفحص البنيوي (لا اعتماديات خدمة، ولا شبكة خارج llm_provider.py) يعيش
    # في `packages/data-engine/tests/test_engine.py` عمداً: شرط القاعدة نفسه
    # أن اختبارات المحرك تبقى خضراء لو حُذف apps/api كاملاً، فحارسٌ يعيش هنا
    # وحده يسقط مع أول تطبيق للشرط. ما يبقى هنا سلوكيّ لا يمكن فحصه هناك.

    def test_the_boundary_file_is_actually_the_one_being_used(self):
        """حدٌّ لا يمرّ به أحد ليس حدّاً بل زينة (قاعدة العمل #8).

        نستبدل المزوّد ونتأكّد أنّ كِلا المستهلكَين مرّا به فعلاً.
        """
        from phoenix import ai_explainer, llm_intent_resolver, llm_provider

        seen: list[dict] = []

        class Spy:
            def complete(self, prompt, *, model, json_schema=None):
                seen.append({"model": model, "schema": json_schema is not None})
                return "{}"

        original = llm_provider.default_provider
        llm_provider.default_provider = lambda api_key=None: Spy()
        try:
            llm_intent_resolver._default_gemini_call("س", "m-1", None)
            ai_explainer._default_gemini_text_call("س", "m-2", None)
        finally:
            llm_provider.default_provider = original

        assert [x["model"] for x in seen] == ["m-1", "m-2"], (
            f"أحد المستهلكَين لا يمرّ بالمزوّد: {seen}")
        assert [x["schema"] for x in seen] == [True, False], (
            "تحليل النية يجب أن يطلب مخطّط JSON، والشرح نصاً حرّاً")

    def test_engine_imports_without_any_service_running(self):
        """لو حُذف apps/api كاملاً لبقي المحرك يعمل — نتحقق بالاستيراد المعزول."""
        import subprocess
        r = subprocess.run(
            [sys.executable, "-c",
             "import phoenix.pipeline, phoenix.semantic, phoenix.charts; print('ok')"],
            capture_output=True, text=True,
            env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(ROOT / "packages" / "data-engine")},
        )
        assert r.returncode == 0, r.stderr[-500:]


class TestRule2PostgresHoldsMetadataOnly:
    @pytest.mark.asyncio
    async def test_rows_come_from_storage_not_the_database(self, client):
        """الإثبات السلوكي: أزل الملف المعالَج من التخزين ⇒ يتعذّر عرض
        الصفوف. لو كانت البيانات في Postgres لظلّت تُعرض."""
        email = f"user_{uuid.uuid4().hex[:10]}@example.com"
        r = await client.post("/auth/register",
                              json={"email": email, "password": "StrongPass123"})
        auth = {"Authorization": f"Bearer {r.json()['access_token']}"}
        with FIXTURE.open("rb") as fh:
            r = await client.post("/datasets", headers=auth,
                                  files={"file": (FIXTURE.name, fh)})
        ds_id = r.json()["dataset_id"]
        async with get_sessionmaker()() as db:
            await run_processing_job(db, r.json()["job_id"])

        assert (await client.get(f"/datasets/{ds_id}/rows", headers=auth)).json()["rows"]

        async with get_sessionmaker()() as db:
            processed_key = (await db.get(Dataset, ds_id)).processed_key
        from apps.api.src.storage.cache import invalidate
        get_storage().delete(get_settings().s3_bucket_processed, processed_key)
        invalidate(processed_key)

        r = await client.get(f"/datasets/{ds_id}/rows", headers=auth)
        assert r.status_code >= 400, (
            "الصفوف ما زالت تُعرض بعد حذف الملف من التخزين — "
            "أي أن بيانات المستخدم مخزَّنة في قاعدة البيانات (خرق للقاعدة #2)")
        # وبنفس الوقت: الفشل يجب أن يبقى برسالة عربية بالشكل الموحّد،
        # لا استثناءً خاماً (عيب حقيقي كشفه هذا الاختبار)
        assert r.json()["error_code"] == "storage_object_missing"
        assert r.json()["message_ar"]


class TestRule6NoLongEndpoints:
    @pytest.mark.asyncio
    async def test_upload_returns_immediately_with_a_job(self, client):
        import time
        email = f"user_{uuid.uuid4().hex[:10]}@example.com"
        r = await client.post("/auth/register",
                              json={"email": email, "password": "StrongPass123"})
        auth = {"Authorization": f"Bearer {r.json()['access_token']}"}

        t0 = time.perf_counter()
        with FIXTURE.open("rb") as fh:
            r = await client.post("/datasets", headers=auth,
                                  files={"file": (FIXTURE.name, fh)})
        elapsed = time.perf_counter() - t0

        assert r.status_code == 202
        assert r.json()["job_id"], "الرفع لا يُرجع وظيفة — المعالجة تمت داخل الطلب؟"
        assert elapsed < 3.0, f"الرفع استغرق {elapsed:.1f}s — أطول من الحد"

    @pytest.mark.asyncio
    async def test_reprocessing_decisions_return_a_job_not_a_result(self, client):
        """التصحيح وإعادة التنظيف عمليتان طويلتان — يجب أن تمرّا بالطابور."""
        email = f"user_{uuid.uuid4().hex[:10]}@example.com"
        r = await client.post("/auth/register",
                              json={"email": email, "password": "StrongPass123"})
        auth = {"Authorization": f"Bearer {r.json()['access_token']}"}
        with FIXTURE.open("rb") as fh:
            r = await client.post("/datasets", headers=auth,
                                  files={"file": (FIXTURE.name, fh)})
        ds_id, job_id = r.json()["dataset_id"], r.json()["job_id"]
        async with get_sessionmaker()() as db:
            await run_processing_job(db, job_id)

        schema = (await client.get(f"/datasets/{ds_id}/schema", headers=auth)).json()
        col = schema["columns"][0]["column_name"]
        r = await client.patch(f"/datasets/{ds_id}/schema", headers=auth,
                               json={"columns": [{"column_name": col, "concept": "customer"}]})
        assert r.status_code == 202 and r.json()["job_id"]
        # الملف الآن قيد المعالجة — ننهيها قبل القرار التالي، تماماً كما
        # يفعل المستخدم الذي ينتظر انتهاء إعادة الحساب
        async with get_sessionmaker()() as db:
            await run_processing_job(db, r.json()["job_id"])

        r = await client.patch(f"/datasets/{ds_id}/cleaning", headers=auth,
                               json={"disabled": []})
        assert r.status_code == 202 and r.json()["job_id"]
