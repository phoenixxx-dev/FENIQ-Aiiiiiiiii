"""اختبارات اللوحة والجدول والتصدير — على Postgres وRedis حقيقيين."""
from __future__ import annotations

import io
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
    """مستخدم + ملف معالَج وجاهز — نقطة البداية لكل اختبار هنا."""
    email = f"an_{uuid.uuid4().hex[:10]}@example.com"
    r = await client.post("/auth/register", json={"email": email, "password": "StrongPass123"})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    with open(FIXTURE, "rb") as f:
        r = await client.post("/datasets", headers=h,
                              files={"file": ("sales.xlsx", f, "application/octet-stream")})
    ds_id, job_id = r.json()["dataset_id"], r.json()["job_id"]
    async with get_sessionmaker()() as db:
        await run_processing_job(db, job_id)
    return h, ds_id


class TestDashboard:
    @pytest.mark.asyncio
    async def test_returns_kpis_charts_insights_in_one_call(self, client, ready):
        h, ds_id = ready
        r = await client.get(f"/datasets/{ds_id}/dashboard", headers=h)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["kpis"], "لوحة بلا مؤشرات"
        assert body["charts"], "لوحة بلا رسوم"
        assert isinstance(body["insights"], list)

    @pytest.mark.asyncio
    async def test_every_kpi_carries_evidence(self, client, ready):
        """قاعدة ذهبية #1: ما في رقم بلا دليل محسوب."""
        h, ds_id = ready
        body = (await client.get(f"/datasets/{ds_id}/dashboard", headers=h)).json()
        for k in body["kpis"]:
            assert k["evidence"] is not None, f"مؤشر بلا دليل: {k['label_ar']}"
            assert k["evidence"]["sql"]

    @pytest.mark.asyncio
    async def test_charts_are_specs_not_rendering_code(self, client, ready):
        """المحرك يُنتج ChartSpec؛ لو تسرّب كود ECharts فالطبقات اختلطت."""
        h, ds_id = ready
        body = (await client.get(f"/datasets/{ds_id}/dashboard", headers=h)).json()
        for c in body["charts"]:
            assert c["type"] in {"line", "bar", "bar_horizontal", "scatter",
                                 "histogram", "donut", "table"}
            assert c["data"] and c["x"]["field"] and c["y"]
            assert c["reason_ar"], "كل رسم يشرح لماذا اختير"
            assert "series" not in c and "xAxis" not in c

    @pytest.mark.asyncio
    async def test_other_user_gets_404(self, client, ready):
        _h, ds_id = ready
        r = await client.post("/auth/register",
                              json={"email": f"x_{uuid.uuid4().hex[:8]}@example.com",
                                    "password": "StrongPass123"})
        hb = {"Authorization": f"Bearer {r.json()['access_token']}"}
        assert (await client.get(f"/datasets/{ds_id}/dashboard", headers=hb)).status_code == 404


class TestRows:
    @pytest.mark.asyncio
    async def test_pagination_shape(self, client, ready):
        h, ds_id = ready
        r = await client.get(f"/datasets/{ds_id}/rows?page=1&page_size=5", headers=h)
        assert r.status_code == 200, r.text
        body = r.json()
        assert len(body["rows"]) <= 5
        assert body["page"] == 1 and body["page_size"] == 5
        assert body["total_rows"] > 0
        assert body["total_pages"] >= 1
        assert body["columns"]

    @pytest.mark.asyncio
    async def test_second_page_differs_from_first(self, client, ready):
        h, ds_id = ready
        p1 = (await client.get(f"/datasets/{ds_id}/rows?page=1&page_size=3", headers=h)).json()
        p2 = (await client.get(f"/datasets/{ds_id}/rows?page=2&page_size=3", headers=h)).json()
        assert p1["rows"] != p2["rows"]

    @pytest.mark.asyncio
    async def test_page_size_capped(self, client, ready):
        """سقف الصفحة يحمي الهاتف — طلب 5000 صف يُرفض لا يُنفَّذ."""
        h, ds_id = ready
        r = await client.get(f"/datasets/{ds_id}/rows?page_size=5000", headers=h)
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_sort_by_unknown_column_rejected(self, client, ready):
        h, ds_id = ready
        r = await client.get(f"/datasets/{ds_id}/rows?sort_by=لا_يوجد", headers=h)
        assert r.status_code == 400
        assert r.json()["error_code"] == "unknown_column"

    @pytest.mark.asyncio
    async def test_sort_actually_orders(self, client, ready):
        """اسم العمود يؤخذ من الـschema لا بالتخمين: «الكميه» بالهاء لا تطابق
        «الكمية» بالتاء المربوطة — فخ إملائي عربي أوقعنا فيه قبل."""
        h, ds_id = ready
        schema = (await client.get(f"/datasets/{ds_id}/schema", headers=h)).json()
        num_col = next((c["column_name"] for c in schema["columns"]
                        if c["concept"] == "quantity"), None)
        assert num_col, "ملف الاختبار يجب أن يحوي عمود كمية"

        asc = (await client.get(
            f"/datasets/{ds_id}/rows?sort_by={num_col}&descending=false&page_size=20",
            headers=h)).json()["rows"]
        vals = [r[num_col] for r in asc if r[num_col] is not None]
        assert vals == sorted(vals)

        desc = (await client.get(
            f"/datasets/{ds_id}/rows?sort_by={num_col}&descending=true&page_size=20",
            headers=h)).json()["rows"]
        dvals = [r[num_col] for r in desc if r[num_col] is not None]
        assert dvals == sorted(dvals, reverse=True)


class TestExport:
    @pytest.mark.asyncio
    async def test_csv_has_bom_for_arabic_excel(self, client, ready):
        """بدون BOM يعرض إكسل العربية كرموز مشوّهة — عيب شائع ومزعج."""
        h, ds_id = ready
        r = await client.get(f"/datasets/{ds_id}/export?fmt=csv", headers=h)
        assert r.status_code == 200
        assert r.content.startswith(b"\xef\xbb\xbf")
        assert "attachment" in r.headers["content-disposition"]

    @pytest.mark.asyncio
    async def test_xlsx_is_a_real_workbook(self, client, ready):
        h, ds_id = ready
        r = await client.get(f"/datasets/{ds_id}/export?fmt=xlsx", headers=h)
        assert r.status_code == 200
        assert r.content[:4] == b"PK\x03\x04", "ليس ملف xlsx حقيقي"
        import polars as pl
        df = pl.read_excel(io.BytesIO(r.content))
        assert df.height > 0

    @pytest.mark.asyncio
    async def test_unknown_format_rejected(self, client, ready):
        h, ds_id = ready
        assert (await client.get(f"/datasets/{ds_id}/export?fmt=pdf",
                                 headers=h)).status_code == 422


class TestExportDelivery:
    """التصدير كان يُسلَّم سطراً سطراً — 54 ثانية لملف يُبنى في عُشر ثانية."""

    @pytest.mark.asyncio
    async def test_csv_arrives_whole_and_with_a_bom(self, client, ready):
        auth, ds_id = ready
        r = await client.get(f"/datasets/{ds_id}/export", headers=auth,
                             params={"fmt": "csv"})
        assert r.status_code == 200
        assert r.content.startswith(b"\xef\xbb\xbf"), "بلا BOM يعرض إكسل العربية مشوّهة"
        assert r.headers.get("content-length") == str(len(r.content))
        # الرأس + كل الصفوف موجودة
        text = r.content.decode("utf-8-sig")
        assert text.count("\n") >= 400

    @pytest.mark.asyncio
    async def test_xlsx_is_refused_above_the_row_ceiling(self, client, ready, monkeypatch):
        """xlsxwriter يبني خلية بخلية: 300 ألف صف ≈ 33 ثانية.

        الصادق أن نرفض ونقترح CSV، لا أن نُبقي المستخدم ينتظر دقيقة.
        """
        from apps.api.src.routers import analysis

        auth, ds_id = ready
        monkeypatch.setattr(analysis, "XLSX_MAX_ROWS", 10)

        r = await client.get(f"/datasets/{ds_id}/export", headers=auth,
                             params={"fmt": "xlsx"})
        assert r.status_code == 413
        assert r.json()["error_code"] == "too_large_for_xlsx"
        assert "CSV" in r.json()["message_ar"], "الرسالة لا تقترح البديل"

    @pytest.mark.asyncio
    async def test_csv_has_no_such_ceiling(self, client, ready, monkeypatch):
        from apps.api.src.routers import analysis

        auth, ds_id = ready
        monkeypatch.setattr(analysis, "XLSX_MAX_ROWS", 10)
        r = await client.get(f"/datasets/{ds_id}/export", headers=auth,
                             params={"fmt": "csv"})
        assert r.status_code == 200
