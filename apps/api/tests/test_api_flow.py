"""اختبارات تكامل على Postgres وRedis حقيقيين (لا mocks للبنية التحتية).

المُختبَر هو نفسه ما يعمل بالإنتاج: نفس دالة المعالجة التي يستدعيها الـworker.
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
    """محرّك قاعدة بيانات جديد لكل اختبار.

    السبب: pytest-asyncio ينشئ حلقة أحداث جديدة لكل اختبار، واتصالات asyncpg
    مرتبطة بالحلقة التي أنشأتها. في الإنتاج التطبيق يعمل بحلقة واحدة طوال
    عمره، فالمحرّك المشترك هناك صحيح — هذه معالجة خاصة بالاختبار فقط.
    """
    await dispose_engine()
    await create_all()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await dispose_engine()


def _email() -> str:
    return f"user_{uuid.uuid4().hex[:10]}@example.com"


async def _register(client) -> dict:
    r = await client.post("/auth/register", json={"email": _email(), "password": "StrongPass123"})
    assert r.status_code == 201, r.text
    return r.json()


def _auth(tokens: dict) -> dict:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def _process_now(job_id: str) -> None:
    """ينفّذ نفس دالة الـworker بالضبط (بلا انتظار طابور) داخل الاختبار."""
    async with get_sessionmaker()() as db:
        await run_processing_job(db, job_id)


class TestHealth:
    @pytest.mark.asyncio
    async def test_health_reports_real_services(self, client):
        r = await client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["db"] == "ok"
        assert body["redis"] == "ok"
        assert body["storage"] == "ok"


class TestAuth:
    @pytest.mark.asyncio
    async def test_register_login_and_me(self, client):
        email = _email()
        r = await client.post("/auth/register", json={"email": email, "password": "StrongPass123"})
        assert r.status_code == 201
        r = await client.post("/auth/login", json={"email": email, "password": "StrongPass123"})
        assert r.status_code == 200
        me = await client.get("/auth/me", headers=_auth(r.json()))
        assert me.status_code == 200
        assert me.json()["email"] == email

    @pytest.mark.asyncio
    async def test_wrong_password_rejected(self, client):
        email = _email()
        await client.post("/auth/register", json={"email": email, "password": "StrongPass123"})
        r = await client.post("/auth/login", json={"email": email, "password": "WrongPass999"})
        assert r.status_code == 401
        assert r.json()["message_ar"]

    @pytest.mark.asyncio
    async def test_duplicate_email_rejected(self, client):
        email = _email()
        await client.post("/auth/register", json={"email": email, "password": "StrongPass123"})
        r = await client.post("/auth/register", json={"email": email, "password": "StrongPass123"})
        assert r.status_code == 409

    @pytest.mark.asyncio
    async def test_protected_endpoint_requires_token(self, client):
        assert (await client.get("/datasets")).status_code == 401

    @pytest.mark.asyncio
    async def test_refresh_token_issues_new_access(self, client):
        tokens = await _register(client)
        r = await client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
        assert r.status_code == 200
        assert r.json()["access_token"]

    @pytest.mark.asyncio
    async def test_access_token_not_accepted_as_refresh(self, client):
        """توكن الوصول لا يصلح للتجديد — وإلا انهار معنى الفصل بينهما."""
        tokens = await _register(client)
        r = await client.post("/auth/refresh", json={"refresh_token": tokens["access_token"]})
        assert r.status_code == 401


class TestUploadAndProcess:
    @pytest.mark.asyncio
    async def test_full_path_upload_process_ask(self, client):
        """المسار الكامل: رفع ← وظيفة ← معالجة ← نتائج ← سؤال بإجابة لها دليل."""
        tokens = await _register(client)
        h = _auth(tokens)

        with open(FIXTURE, "rb") as f:
            r = await client.post("/datasets", headers=h,
                                  files={"file": ("sales_ar_messy.xlsx", f,
                                                  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
        assert r.status_code == 202, r.text
        dataset_id, job_id = r.json()["dataset_id"], r.json()["job_id"]

        job = (await client.get(f"/jobs/{job_id}", headers=h)).json()
        assert job["status"] in ("queued", "running")

        await _process_now(job_id)

        job = (await client.get(f"/jobs/{job_id}", headers=h)).json()
        assert job["status"] == "succeeded", job
        assert job["progress"] == 100
        assert job["stage_ar"] == "جاهز"

        ds = (await client.get(f"/datasets/{dataset_id}", headers=h)).json()
        assert ds["status"] == "ready"
        assert ds["row_count"] > 0 and ds["column_count"] > 0

        schema = (await client.get(f"/datasets/{dataset_id}/schema", headers=h)).json()
        assert schema["columns"]

        profile = (await client.get(f"/datasets/{dataset_id}/profile", headers=h)).json()
        assert profile["row_count"] == ds["row_count"]

        cleaning = (await client.get(f"/datasets/{dataset_id}/cleaning", headers=h)).json()
        assert cleaning["recipe"]["operations"]

        insights = (await client.get(f"/datasets/{dataset_id}/insights", headers=h)).json()
        assert isinstance(insights, list)

        r = await client.post(f"/datasets/{dataset_id}/ask", headers=h,
                              json={"question": "كم إجمالي المبيعات؟"})
        assert r.status_code == 200, r.text
        ans = r.json()
        assert ans["tool"] == "aggregate"
        assert ans["evidence"] is not None
        assert ans["evidence"]["rows_in_scope"] > 0
        # قاعدة ذهبية #1: الرقم بالنص لازم يكون له دليل محسوب
        assert any(ch.isdigit() for ch in ans["answer_ar"])

    @pytest.mark.asyncio
    async def test_empty_file_rejected(self, client):
        h = _auth(await _register(client))
        r = await client.post("/datasets", headers=h,
                              files={"file": ("empty.csv", b"", "text/csv")})
        assert r.status_code == 400
        assert r.json()["error_code"] == "empty_file"

    @pytest.mark.asyncio
    async def test_unsupported_file_fails_with_arabic_reason(self, client):
        """ملف غير مدعوم: الوظيفة تفشل برسالة عربية، لا انهيار ولا نجاح كاذب."""
        h = _auth(await _register(client))
        r = await client.post("/datasets", headers=h,
                              files={"file": ("x.pdf", b"%PDF-1.4 junk", "application/pdf")})
        assert r.status_code == 202
        job_id, dataset_id = r.json()["job_id"], r.json()["dataset_id"]
        await _process_now(job_id)

        job = (await client.get(f"/jobs/{job_id}", headers=h)).json()
        assert job["status"] == "failed"
        assert "PDF" in job["error_ar"] or job["error_ar"]

        ds = (await client.get(f"/datasets/{dataset_id}", headers=h)).json()
        assert ds["status"] == "failed"

    @pytest.mark.asyncio
    async def test_duplicate_upload_is_flagged(self, client):
        """بصمة الملف: نفس الملف مرتين يُنبَّه أنه مكرر بدل مضاعفة الأرقام صامتاً."""
        h = _auth(await _register(client))
        for _ in range(2):
            with open(FIXTURE, "rb") as f:
                r = await client.post("/datasets", headers=h,
                                      files={"file": ("sales_ar_messy.xlsx", f, "application/octet-stream")})
            await _process_now(r.json()["job_id"])
        assert r.json()["duplicate_of"] is not None

    @pytest.mark.asyncio
    async def test_ask_before_ready_is_rejected(self, client):
        h = _auth(await _register(client))
        with open(FIXTURE, "rb") as f:
            r = await client.post("/datasets", headers=h,
                                  files={"file": ("s.xlsx", f, "application/octet-stream")})
        dataset_id = r.json()["dataset_id"]
        r = await client.post(f"/datasets/{dataset_id}/ask", headers=h,
                              json={"question": "كم إجمالي المبيعات؟"})
        assert r.status_code == 409
        assert r.json()["error_code"] == "not_ready"


class TestIsolation:
    """أهم اختبار أمني: مستخدم لا يرى ملف مستخدم آخر إطلاقاً."""

    @pytest.mark.asyncio
    async def test_user_b_cannot_touch_user_a_dataset(self, client):
        ha = _auth(await _register(client))
        with open(FIXTURE, "rb") as f:
            r = await client.post("/datasets", headers=ha,
                                  files={"file": ("a.xlsx", f, "application/octet-stream")})
        dataset_id, job_id = r.json()["dataset_id"], r.json()["job_id"]
        await _process_now(job_id)

        hb = _auth(await _register(client))
        for path in (f"/datasets/{dataset_id}",
                     f"/datasets/{dataset_id}/schema",
                     f"/datasets/{dataset_id}/profile",
                     f"/datasets/{dataset_id}/cleaning",
                     f"/datasets/{dataset_id}/insights"):
            resp = await client.get(path, headers=hb)
            assert resp.status_code == 404, f"{path} سرّب بيانات مستخدم آخر!"

        resp = await client.post(f"/datasets/{dataset_id}/ask", headers=hb,
                                 json={"question": "كم إجمالي المبيعات؟"})
        assert resp.status_code == 404

        resp = await client.get(f"/jobs/{job_id}", headers=hb)
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_list_shows_only_own_datasets(self, client):
        ha = _auth(await _register(client))
        with open(FIXTURE, "rb") as f:
            await client.post("/datasets", headers=ha,
                              files={"file": ("a.xlsx", f, "application/octet-stream")})
        hb = _auth(await _register(client))
        assert (await client.get("/datasets", headers=hb)).json() == []
        assert len((await client.get("/datasets", headers=ha)).json()) == 1


class TestArchitectureRules:
    def test_engine_never_imports_api(self):
        """قاعدة ذهبية #4: الاستيراد باتجاه واحد — المحرك لا يعرف الـAPI."""
        engine_dir = ROOT / "packages" / "data-engine" / "phoenix"
        banned = ["fastapi", "sqlalchemy", "apps.api", "boto3", "arq", "redis"]
        for py in engine_dir.rglob("*.py"):
            src = py.read_text(encoding="utf-8")
            for b in banned:
                assert f"import {b}" not in src, f"{py.name} يستورد {b}"

    def test_no_placeholder_code_in_api(self):
        """ممنوع TODO/mock/fake مخفية بطبقة الـAPI."""
        api_dir = ROOT / "apps" / "api" / "src"
        for py in api_dir.rglob("*.py"):
            src = py.read_text(encoding="utf-8").lower()
            for bad in ("todo", "fixme", "placeholder", "dummy data"):
                assert bad not in src, f"{py.name} فيه {bad}"
