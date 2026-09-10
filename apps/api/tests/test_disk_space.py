"""حارس مساحة القرص — لازمة قرار «التخزين على السيرفر نفسه» (ق-48).

مع تخزين سحابي لا سقف عملي للمساحة. أمّا على قرص السيرفر فالمساحة مورد
محدود، وامتلاؤها **لا يُعطّل الرفع وحده**: قاعدة البيانات تتوقف عن الكتابة،
والسجلات تتوقف، والمعالجة تفشل — ويبدو العطل غامضاً بلا سبب ظاهر.

القاعدة: نرفض قبل أن نمتلئ، برسالة تقول للمستخدم ماذا يفعل.
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

from apps.api.src.db.session import create_all, dispose_engine  # noqa: E402
from apps.api.src.main import app  # noqa: E402
from apps.api.src.routers import uploads  # noqa: E402
from apps.api.src.storage.client import get_storage  # noqa: E402


@pytest_asyncio.fixture
async def auth():
    await dispose_engine()
    await create_all()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        email = f"disk_{uuid.uuid4().hex[:10]}@example.com"
        r = await c.post("/auth/register",
                         json={"email": email, "password": "StrongPass123"})
        yield c, {"Authorization": f"Bearer {r.json()['access_token']}"}
    await dispose_engine()


class TestFreeSpaceIsReadable:
    def test_local_storage_reports_a_real_number(self):
        free = get_storage().free_bytes()
        assert free is None or free > 0, "المساحة الحرة رقم غير معقول"


class TestUploadIsRefusedBeforeTheDiskFills:
    @pytest.mark.asyncio
    async def test_a_full_disk_refuses_with_an_actionable_message(self, auth, monkeypatch):
        client, headers = auth
        # قرص شبه ممتلئ: 100 ميغابايت متبقية فقط
        monkeypatch.setattr(uploads, "ensure_disk_space",
                            uploads.ensure_disk_space)   # يبقى الأصل
        monkeypatch.setattr(type(get_storage()), "free_bytes",
                            lambda self: 100 * 1024 * 1024, raising=False)

        r = await client.post("/datasets/upload-url", headers=headers,
                              json={"filename": "كبير.xlsx", "size": 50 * 1024 * 1024})
        assert r.status_code == 507, r.text
        body = r.json()
        assert body["error_code"] == "disk_full"
        msg = body["message_ar"]
        # الرسالة يجب أن ترشد لا أن تخبر فقط
        assert "مساحة" in msg and ("احذف" in msg or "تواصل" in msg), msg
        assert not any(w in msg for w in ("disk", "bytes", "storage")), (
            f"مصطلح داخلي في رسالة المستخدم: {msg}")

    @pytest.mark.asyncio
    async def test_a_healthy_disk_accepts_the_same_file(self, auth, monkeypatch):
        """الحدّ المقابل: الحارس لا يرفض ما دامت المساحة كافية."""
        client, headers = auth
        monkeypatch.setattr(type(get_storage()), "free_bytes",
                            lambda self: 20 * 1024 * 1024 * 1024, raising=False)
        r = await client.post("/datasets/upload-url", headers=headers,
                              json={"filename": "عادي.xlsx", "size": 50 * 1024 * 1024})
        assert r.status_code == 201, r.text

    @pytest.mark.asyncio
    async def test_unknown_free_space_does_not_block_uploads(self, auth, monkeypatch):
        """تخزين سحابي (free_bytes = None) يجب ألا يُمنع بحجة المساحة."""
        client, headers = auth
        monkeypatch.setattr(type(get_storage()), "free_bytes",
                            lambda self: None, raising=False)
        r = await client.post("/datasets/upload-url", headers=headers,
                              json={"filename": "سحابي.xlsx", "size": 10 * 1024 * 1024})
        assert r.status_code == 201, r.text


class TestHealthShowsTheDisk:
    @pytest.mark.asyncio
    async def test_health_reports_free_space(self, auth):
        client, _ = auth
        body = (await client.get("/health")).json()
        assert "disk_free_mb" in body, "الصحة لا تذكر المساحة — العطل سيبدو غامضاً"
        assert body["disk"] in ("ok", "low")

    @pytest.mark.asyncio
    async def test_low_disk_degrades_the_status(self, auth, monkeypatch):
        client, _ = auth
        monkeypatch.setattr(type(get_storage()), "free_bytes",
                            lambda self: 200 * 1024 * 1024, raising=False)
        body = (await client.get("/health")).json()
        assert body["disk"] == "low"
        assert body["status"] == "degraded", (
            "المساحة ضاقت والحالة ما زالت ok — لا فائدة من فحص لا ينذر")
