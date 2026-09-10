"""البحث العربي في الجدول — عيب كان يجعله بلا فائدة تقريباً.

الخطأ: تطبيع نص البحث وحده («شركة» ← «شركه») ومقارنته بقيم غير مطبَّعة. أي
كلمة فيها ة أو أ أو ى لا تُطابق شيئاً — ومعظم الأسماء العربية فيها واحدة.
النتيجة: المستخدم يبحث عن عميل يراه أمامه في الجدول ويحصل على صفر.
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


@pytest_asyncio.fixture
async def ready(client):
    email = f"user_{uuid.uuid4().hex[:10]}@example.com"
    r = await client.post("/auth/register", json={"email": email, "password": "StrongPass123"})
    auth = {"Authorization": f"Bearer {r.json()['access_token']}"}
    with FIXTURE.open("rb") as fh:
        r = await client.post("/datasets", headers=auth, files={"file": (FIXTURE.name, fh)})
    body = r.json()
    async with get_sessionmaker()() as db:
        await run_processing_job(db, body["job_id"])

    # نتحقق من الجاهزية داخل التجهيز نفسه: بدونه يظهر الفشل لاحقاً كـ
    # KeyError غامض على مفتاح "rows" بدل سبب مفهوم.
    meta = await client.get(f"/datasets/{body['dataset_id']}", headers=auth)
    assert meta.json()["status"] == "ready", f"التجهيز لم يكتمل: {meta.text}"
    return client, auth, body["dataset_id"]


async def _search(client, auth, ds_id, q: str) -> int:
    r = await client.get(f"/datasets/{ds_id}/rows", headers=auth, params={"search": q})
    assert r.status_code == 200, r.text
    return r.json()["total_rows"]


class TestSearchFindsWhatTheUserSees:
    @pytest.mark.asyncio
    async def test_searching_a_value_shown_in_the_table_finds_it(self, ready):
        """أبسط اختبار ممكن — وكان يفشل: ننسخ قيمة من الجدول ونبحث عنها."""
        client, auth, ds_id = ready
        rows = (await client.get(f"/datasets/{ds_id}/rows", headers=auth,
                                 params={"page_size": 100})).json()
        values = {str(r.get("العميل")) for r in rows["rows"] if r.get("العميل")}
        # نختار قيمة تحتوي تاءً مربوطة — أصل العيب
        target = next(v for v in sorted(values) if "ة" in v)

        found = await _search(client, auth, ds_id, target)
        assert found > 0, f"البحث عن «{target}» — وهي قيمة ظاهرة في الجدول — أعطى صفراً"

    @pytest.mark.asyncio
    async def test_spelling_variants_find_the_same_rows(self, ready):
        """«شركة» و«شركه» يجب أن تعطيا النتيجة نفسها — هذا جوهر التطبيع."""
        client, auth, ds_id = ready
        with_taa = await _search(client, auth, ds_id, "شركة")
        with_haa = await _search(client, auth, ds_id, "شركه")
        assert with_taa > 0
        assert with_taa == with_haa

    @pytest.mark.asyncio
    async def test_diacritics_and_tatweel_do_not_break_search(self, ready):
        client, auth, ds_id = ready
        plain = await _search(client, auth, ds_id, "شركة")
        assert await _search(client, auth, ds_id, "شــركة") == plain
        assert await _search(client, auth, ds_id, "شَرِكَة") == plain

    @pytest.mark.asyncio
    async def test_arabic_indic_digits_match_western_ones(self, ready):
        client, auth, ds_id = ready
        assert await _search(client, auth, ds_id, "١") == await _search(
            client, auth, ds_id, "1")

    @pytest.mark.asyncio
    async def test_a_term_that_is_really_absent_returns_nothing(self, ready):
        """الحد المقابل: تطبيع فضفاض يطابق كل شيء يساوي بحثاً معطّلاً."""
        client, auth, ds_id = ready
        assert await _search(client, auth, ds_id, "زغرتا الكورة عكار") == 0

    @pytest.mark.asyncio
    async def test_search_narrows_the_result_set(self, ready):
        client, auth, ds_id = ready
        total = (await client.get(f"/datasets/{ds_id}/rows", headers=auth)).json()["total_rows"]
        assert 0 < await _search(client, auth, ds_id, "شركة") < total


class TestSorting:
    """الفرز يجري على الملف كلّه في الخادم — لا على الصفحة المعروضة."""

    @pytest.mark.asyncio
    async def test_sorting_orders_the_whole_file_not_the_page(self, ready):
        client, auth, ds_id = ready
        col = "الاجمالي"

        desc = (await client.get(f"/datasets/{ds_id}/rows", headers=auth,
                                 params={"sort_by": col, "descending": True,
                                         "page_size": 5})).json()["rows"]
        asc = (await client.get(f"/datasets/{ds_id}/rows", headers=auth,
                                params={"sort_by": col, "descending": False,
                                        "page_size": 5})).json()["rows"]

        top = [r[col] for r in desc if r[col] is not None]
        bottom = [r[col] for r in asc if r[col] is not None]
        assert top == sorted(top, reverse=True)
        assert bottom == sorted(bottom)
        assert top[0] > bottom[0], "الاتجاهان يعطيان النتيجة نفسها"

    @pytest.mark.asyncio
    async def test_sorting_is_stable_across_pages(self, ready):
        """أعلى قيمة في الصفحة الثانية يجب ألا تتجاوز أدنى قيمة في الأولى."""
        client, auth, ds_id = ready
        col = "الاجمالي"
        p1 = (await client.get(f"/datasets/{ds_id}/rows", headers=auth,
                               params={"sort_by": col, "descending": True,
                                       "page_size": 10, "page": 1})).json()["rows"]
        p2 = (await client.get(f"/datasets/{ds_id}/rows", headers=auth,
                               params={"sort_by": col, "descending": True,
                                       "page_size": 10, "page": 2})).json()["rows"]
        last_of_first = min(r[col] for r in p1 if r[col] is not None)
        first_of_second = max(r[col] for r in p2 if r[col] is not None)
        assert first_of_second <= last_of_first

    @pytest.mark.asyncio
    async def test_sorting_an_unknown_column_is_refused_clearly(self, ready):
        client, auth, ds_id = ready
        r = await client.get(f"/datasets/{ds_id}/rows", headers=auth,
                             params={"sort_by": "عمود_غير_موجود"})
        assert r.status_code == 400
        assert r.json()["error_code"] == "unknown_column"

    @pytest.mark.asyncio
    async def test_search_and_sort_work_together(self, ready):
        client, auth, ds_id = ready
        r = await client.get(f"/datasets/{ds_id}/rows", headers=auth,
                             params={"search": "شركة", "sort_by": "الاجمالي",
                                     "descending": True, "page_size": 5})
        assert r.status_code == 200
        body = r.json()
        assert 0 < body["total_rows"]
        vals = [x["الاجمالي"] for x in body["rows"] if x["الاجمالي"] is not None]
        assert vals == sorted(vals, reverse=True)


class TestOutlierFlagsAreDataNotColumns:
    """أعمدة الوسم بيانات للواجهة، لا أعمدة تُعرض للمستخدم."""

    @pytest.mark.asyncio
    async def test_response_maps_each_flag_to_its_source_column(self, ready):
        client, auth, ds_id = ready
        body = (await client.get(f"/datasets/{ds_id}/rows", headers=auth)).json()
        flags = body["outlier_flags"]
        assert flags, "لا توجد خريطة وسم رغم وجود أعمدة موسومة"
        for source, flag in flags.items():
            assert source in body["columns"], f"وسم بلا عمود أصلي: {flag}"
            assert flag in body["columns"]
            assert flag != source

    @pytest.mark.asyncio
    async def test_flag_columns_are_still_exported(self, ready):
        """الإخفاء في العرض فقط — التصدير يحمل الوسم كاملاً."""
        client, auth, ds_id = ready
        r = await client.get(f"/datasets/{ds_id}/export", headers=auth,
                             params={"fmt": "csv"})
        assert r.status_code == 200
        head = r.content.decode("utf-8")[:400]
        assert "_شاذ_" in head, "الوسم غاب عن التصدير — المستخدم يفقد المعلومة"
