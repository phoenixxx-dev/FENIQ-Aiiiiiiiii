"""استقبال كتالوج FeniqSync (المرحلة ١) — على Postgres وRedis حقيقيين.

ما يُثبته هذا الملف: وكيل على إنترنت ضعيف يعيد الإرسال، يُبتر رفعه، يُنسخ
لجهاز آخر، أو يُسرق مفتاحه — والخادم يتصرّف صحيحاً في كل حالة.
"""
from __future__ import annotations

import gzip
import json
import sys
import uuid
from pathlib import Path

import polars as pl
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "packages" / "data-engine"))

from apps.api.src.config import get_settings  # noqa: E402
from apps.api.src.db.models import Dataset, SyncRun, SyncToken, Warehouse  # noqa: E402
from apps.api.src.db.session import (create_all, dispose_engine,  # noqa: E402
                                     get_sessionmaker)
from apps.api.src.main import app  # noqa: E402
from apps.api.src.services.dataset_service import run_processing_job  # noqa: E402
from apps.api.src.storage.client import get_storage  # noqa: E402


@pytest_asyncio.fixture
async def client():
    await dispose_engine()
    await create_all()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    await dispose_engine()


def catalog(prices: dict[str, float], wh: str = "WH-7", **head) -> bytes:
    items = [{"item_id": f"guid-{k}", "name_raw": f"صنف {k}", "unit_raw": "علبة",
              "barcode": f"6210{n:04d}", "price": p, "qty": 10 + n}
             for n, (k, p) in enumerate(prices.items())]
    body = {"source": "Amin", "warehouse_id": wh, "price_level": "جملة",
            "generated_at": "2026-09-10T03:00:00+03:00", "count": len(items), "items": items}
    body.update(head)
    return json.dumps(body, ensure_ascii=False).encode()


BASE = {"A": 1000.0, "B": 2500.0, "C": 400.0}


async def owner(client) -> dict:
    email = f"sync_{uuid.uuid4().hex[:10]}@example.com"
    r = await client.post("/auth/register", json={"email": email, "password": "StrongPass123"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def setup_wh(client, auth=None) -> tuple[dict, str, str, dict]:
    auth = auth or await owner(client)
    wh = (await client.post("/v1/warehouses", headers=auth, json={"name": "مستودع النور"})).json()
    tok = (await client.post(f"/v1/warehouses/{wh['id']}/tokens", headers=auth,
                             json={"label": "جهاز المحاسبة"})).json()
    return auth, wh["id"], tok["token_id"], {"Authorization": f"Bearer {tok['token']}"}


async def push(client, agent, body: bytes, **headers):
    return await client.post("/v1/sync/catalog", headers={**agent, **headers}, content=body)


class TestFirstSync:
    @pytest.mark.asyncio
    async def test_catalog_arrives_and_becomes_an_analysable_dataset(self, client):
        auth, wh, _, agent = await setup_wh(client)
        r = await push(client, agent, catalog(BASE))
        assert r.status_code == 202, r.text
        out = r.json()
        assert out["duplicate"] is False
        assert out["stats"] == {"received": 3, "added": 3, "price_changed": 0,
                                "removed": 0, "reactivated": 0, "active_total": 3}

        async with get_sessionmaker()() as db:
            await run_processing_job(db, out["job_id"])
        ds = (await client.get(f"/datasets/{out['dataset_id']}", headers=auth)).json()
        assert ds["status"] == "ready", ds
        assert ds["row_count"] == 3

        whs = (await client.get("/v1/warehouses", headers=auth)).json()
        assert whs[0]["active_item_count"] == 3 and whs[0]["last_sync_at"]


class TestRetriesAndChanges:
    @pytest.mark.asyncio
    async def test_resending_the_same_payload_is_idempotent(self, client):
        auth, wh, _, agent = await setup_wh(client)
        first = (await push(client, agent, catalog(BASE))).json()
        again = await push(client, agent, catalog(BASE))
        assert again.status_code == 200
        assert again.json()["duplicate"] is True
        assert again.json()["sync_id"] == first["sync_id"]
        runs = (await client.get(f"/v1/warehouses/{wh}/syncs", headers=auth)).json()
        assert len(runs) == 1, "إعادة المحاولة أنشأت مزامنة ثانية"

    @pytest.mark.asyncio
    async def test_price_change_and_removal_are_tracked_in_storage(self, client):
        auth, wh, _, agent = await setup_wh(client)
        first = (await push(client, agent, catalog(BASE))).json()
        r = await push(client, agent, catalog({"A": 1200.0, "B": 2500.0}))
        assert r.status_code == 202, r.text
        s = r.json()["stats"]
        assert (s["price_changed"], s["removed"], s["active_total"]) == (1, 1, 2)

        storage, bucket = get_storage(), get_settings().s3_bucket_processed
        async with get_sessionmaker()() as db:
            run1 = await db.get(SyncRun, first["sync_id"])
            run2 = await db.get(SyncRun, r.json()["sync_id"])
        tmp = Path(f"/tmp/phoenix-test-{uuid.uuid4().hex}")
        changes = pl.read_parquet(storage.fetch_to_local(bucket, run2.price_changes_key, tmp))
        assert changes.select("item_id", "old_price", "new_price").to_dicts() == [
            {"item_id": "guid-A", "old_price": 1000.0, "new_price": 1200.0}]
        state = pl.read_parquet(storage.fetch_to_local(bucket, run2.state_key, tmp / "s2"))
        assert state.height == 3, "الصنف الغائب حُذف بدل أن يُعلَّم"
        assert state.filter(pl.col("item_id") == "guid-C")["active"][0] is False
        # الحالة السابقة لم تُمس — قاعدة #3 بروحها
        assert storage.exists(bucket, run1.state_key)
        assert run1.price_changes_key is None

    @pytest.mark.asyncio
    async def test_concurrent_pushes_are_serialised_not_lost(self, client):
        """وكيل أعاد الإرسال قبل وصول الرد الأول + مزامنة مختلفة بنفس اللحظة:
        بلا قفل، الاثنتان تُدمجان فوق الحالة القديمة نفسها فتضيع إحداهما."""
        import asyncio
        auth, wh, _, agent = await setup_wh(client)
        await push(client, agent, catalog(BASE))
        same = catalog({"A": 1100.0, "B": 2500.0, "C": 400.0})
        other = catalog({"A": 1100.0, "B": 2600.0, "C": 400.0})
        rs = await asyncio.gather(push(client, agent, same), push(client, agent, same),
                                  push(client, agent, other))
        codes = sorted(r.status_code for r in rs)
        assert codes == [200, 202, 202], [r.text for r in rs]
        runs = (await client.get(f"/v1/warehouses/{wh}/syncs", headers=auth)).json()
        assert len(runs) == 3
        # الفحص الحاسم: الأخيرة دُمجت فوق حالة التي قبلها لا فوق الأصل.
        # «السعر القديم» في تغيّراتها يجب أن يطابق سعر الحالة الوسطى — بلا قفل
        # كان سيطابق سعر المزامنة الأولى (1000) فتضيع الوسطى بصمت.
        latest, middle = runs[0], runs[1]
        async with get_sessionmaker()() as db:
            lr = await db.get(SyncRun, latest["sync_id"])
            mr = await db.get(SyncRun, middle["sync_id"])
        storage, bucket = get_storage(), get_settings().s3_bucket_processed
        tmp = Path(f"/tmp/phoenix-test-{uuid.uuid4().hex}")
        mid_state = pl.read_parquet(storage.fetch_to_local(bucket, mr.state_key, tmp / "m"))
        mid_price = dict(zip(mid_state["item_id"], mid_state["price"]))
        assert lr.price_changes_key, "المزامنة الأخيرة لم ترَ أي تغيير — دُمجت فوق نفسها؟"
        ch = pl.read_parquet(storage.fetch_to_local(bucket, lr.price_changes_key, tmp / "l"))
        for row in ch.to_dicts():
            assert row["old_price"] == mid_price[row["item_id"]], (
                f"{row['item_id']}: السعر القديم {row['old_price']} لا يطابق الحالة الوسطى "
                f"{mid_price[row['item_id']]} — تحديث ضائع")

    @pytest.mark.asyncio
    async def test_gzip_payload_is_accepted(self, client):
        _, _, _, agent = await setup_wh(client)
        r = await push(client, agent, gzip.compress(catalog(BASE)), **{"Content-Encoding": "gzip"})
        assert r.status_code == 202, r.text

    @pytest.mark.asyncio
    async def test_corrupt_gzip_is_a_clear_error(self, client):
        _, _, _, agent = await setup_wh(client)
        r = await push(client, agent, b"not gzip at all", **{"Content-Encoding": "gzip"})
        assert r.status_code == 400 and r.json()["error_code"] == "bad_gzip"


class TestRejections:
    @pytest.mark.asyncio
    async def test_truncated_upload_changes_nothing(self, client):
        auth, wh, _, agent = await setup_wh(client)
        await push(client, agent, catalog(BASE))
        r = await push(client, agent, catalog({"A": 1.0}, count=3))
        assert r.status_code == 422 and r.json()["error_code"] == "truncated_catalog"
        assert r.json()["message_ar"]
        whs = (await client.get("/v1/warehouses", headers=auth)).json()
        assert whs[0]["active_item_count"] == 3, "حمولة مبتورة غيّرت الحالة"

    @pytest.mark.asyncio
    async def test_agent_copied_to_another_warehouse_is_refused(self, client):
        _, _, _, agent = await setup_wh(client)
        assert (await push(client, agent, catalog(BASE, wh="WH-7"))).status_code == 202
        r = await push(client, agent, catalog({"X": 5.0}, wh="WH-99"))
        assert r.status_code == 409 and r.json()["error_code"] == "warehouse_mismatch"

    @pytest.mark.asyncio
    async def test_bad_missing_and_revoked_tokens(self, client):
        auth, wh, tid, agent = await setup_wh(client)
        for h in ({}, {"Authorization": "Bearer fsk_nope"}, {"Authorization": "Bearer abc"}):
            r = await client.post("/v1/sync/catalog", headers=h, content=catalog(BASE))
            assert r.status_code == 401 and r.json()["error_code"] == "invalid_sync_token"
        # JWT المستخدم نفسه ليس مفتاح مزامنة
        r = await client.post("/v1/sync/catalog", headers=auth, content=catalog(BASE))
        assert r.status_code == 401

        assert (await client.delete(f"/v1/warehouses/{wh}/tokens/{tid}",
                                    headers=auth)).status_code == 204
        r = await push(client, agent, catalog(BASE))
        assert r.status_code == 401, "مفتاح ملغى ما زال يعمل"


class TestIsolationAndSecrets:
    @pytest.mark.asyncio
    async def test_other_user_cannot_touch_my_warehouse(self, client):
        _, wh, tid, _ = await setup_wh(client)
        other = await owner(client)
        assert (await client.get(f"/v1/warehouses/{wh}/syncs", headers=other)).status_code == 404
        assert (await client.post(f"/v1/warehouses/{wh}/tokens", headers=other,
                                  json={})).status_code == 404
        assert (await client.delete(f"/v1/warehouses/{wh}/tokens/{tid}",
                                    headers=other)).status_code == 404
        assert (await client.get("/v1/warehouses", headers=other)).json() == []

    @pytest.mark.asyncio
    async def test_synced_dataset_belongs_to_the_warehouse_owner_only(self, client):
        auth, _, _, agent = await setup_wh(client)
        out = (await push(client, agent, catalog(BASE))).json()
        other = await owner(client)
        assert (await client.get(f"/datasets/{out['dataset_id']}",
                                 headers=other)).status_code == 404
        async with get_sessionmaker()() as db:
            ds = await db.get(Dataset, out["dataset_id"])
            wh = (await db.execute(select(Warehouse).where(Warehouse.id == out["warehouse_id"]))
                  ).scalars().one()
        assert ds.user_id == wh.user_id

    @pytest.mark.asyncio
    async def test_plaintext_token_is_never_stored(self, client):
        _, _, tid, agent = await setup_wh(client)
        token = agent["Authorization"].split()[1]
        async with get_sessionmaker()() as db:
            t = await db.get(SyncToken, tid)
        assert token not in (t.token_hash, t.prefix, t.label or "")
        assert len(t.token_hash) == 64

    def test_sync_tables_hold_no_item_data(self):
        """قاعدة ذهبية #2: لا عمود لاسم صنف ولا سعر ولا رصيد في جداول المزامنة."""
        forbidden = {"price", "qty", "name_raw", "barcode", "items", "payload"}
        for model in (Warehouse, SyncToken, SyncRun):
            cols = {c.name for c in model.__table__.columns}
            assert not cols & forbidden, f"{model.__tablename__}: {cols & forbidden}"
