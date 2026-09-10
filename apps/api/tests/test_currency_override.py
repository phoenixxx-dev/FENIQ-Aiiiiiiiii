"""العملة التي يختارها المستخدم (ق-49).

المحرك لا يخترع عملة: بلا دليل في الملف تُعرض المبالغ بلا رمز. وهذا صادق لكنه
ناقص — فصاحب الملف يعرف عملته ولا وسيلة لديه ليقولها. هذه الواجهة هي المخرج،
وشرطها أن تكون **تصحيحاً حقيقياً**: يُعاد الحساب، ويظهر الرمز في الأرقام
الفعلية، لا في حقلٍ مخزَّن لا يقرؤه أحد.

وتُخزَّن في عمود مستقلّ لا داخل خريطة تصحيحات الأعمدة: مفاتيح تلك الخريطة
أسماء أعمدة تأتي من ملف المستخدم، وأي مفتاح محجوز فيها يصطدم يوماً بعمود يحمل
ذلك الاسم.
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

FIXTURE = (ROOT / "packages" / "data-engine" / "fixtures" / "real"
           / "feniqsync_catalog.json")


@pytest_asyncio.fixture
async def client():
    await dispose_engine()
    await create_all()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    await dispose_engine()


async def _auth(client) -> dict:
    email = f"cur_{uuid.uuid4().hex[:10]}@example.com"
    r = await client.post("/auth/register",
                          json={"email": email, "password": "StrongPass123"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _ready(client, auth) -> str:
    with FIXTURE.open("rb") as fh:
        r = await client.post("/datasets", headers=auth, files={"file": (FIXTURE.name, fh)})
    body = r.json()
    async with get_sessionmaker()() as db:
        await run_processing_job(db, body["job_id"])
    return body["dataset_id"]


class TestTheEngineOffersItsOwnList:
    @pytest.mark.asyncio
    async def test_currencies_come_from_the_engine(self, client):
        r = await client.get("/datasets/currencies")
        assert r.status_code == 200
        codes = {c["code"] for c in r.json()}
        assert {"SYP", "USD"} <= codes, codes
        syp = next(c for c in r.json() if c["code"] == "SYP")
        assert syp["symbol_ar"] == "ل.س"


class TestSettingTheCurrencyChangesTheNumbers:
    @pytest.mark.asyncio
    async def test_before_and_after(self, client):
        auth = await _auth(client)
        ds = await _ready(client, auth)

        before = (await client.get(f"/datasets/{ds}/dashboard", headers=auth)).json()
        money = [k["formatted_ar"] for k in before["kpis"] if k["unit"] == "currency"]
        assert money, f"لا مؤشّر مالي: {before['kpis']}"
        assert all("ل.س" not in m for m in money), f"رمز قبل أن يُطلب: {money}"

        r = await client.patch(f"/datasets/{ds}/schema", headers=auth,
                               json={"currency": "SYP", "set_currency": True})
        assert r.status_code == 202, r.text
        async with get_sessionmaker()() as db:
            await run_processing_job(db, r.json()["job_id"])

        after = (await client.get(f"/datasets/{ds}/dashboard", headers=auth)).json()
        money = [k["formatted_ar"] for k in after["kpis"] if k["unit"] == "currency"]
        assert money and all("ل.س" in m for m in money), f"لم يظهر الرمز: {money}"

    @pytest.mark.asyncio
    async def test_the_schema_says_the_source_was_the_user(self, client):
        auth = await _auth(client)
        ds = await _ready(client, auth)
        r = await client.patch(f"/datasets/{ds}/schema", headers=auth,
                               json={"currency": "USD", "set_currency": True})
        async with get_sessionmaker()() as db:
            await run_processing_job(db, r.json()["job_id"])
        schema = (await client.get(f"/datasets/{ds}/schema", headers=auth)).json()
        assert schema["currency"]["code"] == "USD"
        assert schema["currency"]["source"] == "user", (
            "لا فرق ظاهر بين ما عرفناه من الملف وما قاله المستخدم")

    @pytest.mark.asyncio
    async def test_clearing_it_returns_to_inference(self, client):
        auth = await _auth(client)
        ds = await _ready(client, auth)
        r = await client.patch(f"/datasets/{ds}/schema", headers=auth,
                               json={"currency": "SYP", "set_currency": True})
        async with get_sessionmaker()() as db:
            await run_processing_job(db, r.json()["job_id"])
        r = await client.patch(f"/datasets/{ds}/schema", headers=auth,
                               json={"currency": None, "set_currency": True})
        async with get_sessionmaker()() as db:
            await run_processing_job(db, r.json()["job_id"])
        schema = (await client.get(f"/datasets/{ds}/schema", headers=auth)).json()
        assert schema["currency"]["code"] is None, "الاختيار لا يُمحى"


class TestBadInputIsRefusedClearly:
    @pytest.mark.asyncio
    async def test_an_unknown_currency_is_rejected(self, client):
        auth = await _auth(client)
        ds = await _ready(client, auth)
        r = await client.patch(f"/datasets/{ds}/schema", headers=auth,
                               json={"currency": "XYZ", "set_currency": True})
        assert r.status_code == 400
        body = r.json()
        assert body["error_code"] == "unknown_currency"
        assert "XYZ" in body["message_ar"]

    @pytest.mark.asyncio
    async def test_an_empty_patch_is_rejected(self, client):
        auth = await _auth(client)
        ds = await _ready(client, auth)
        r = await client.patch(f"/datasets/{ds}/schema", headers=auth, json={"columns": []})
        assert r.status_code == 400
        assert r.json()["error_code"] == "empty_patch"

    @pytest.mark.asyncio
    async def test_a_column_named_like_the_setting_cannot_hijack_it(self, client):
        """سبب العمود المستقلّ: لو خُزّنت العملة كمفتاح داخل تصحيحات الأعمدة،
        لكان عمودٌ بذلك الاسم قادراً على تغييرها."""
        auth = await _auth(client)
        ds = await _ready(client, auth)
        r = await client.patch(f"/datasets/{ds}/schema", headers=auth,
                               json={"columns": [{"column_name": "currency",
                                                  "concept": "quantity"}]})
        assert r.status_code == 400, "عمود غير موجود يجب أن يُرفض"
        assert r.json()["error_code"] == "unknown_column"
