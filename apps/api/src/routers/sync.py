"""مزامنة كتالوج المستودع (المرحلة ١) — نقطة استقبال وكيل FeniqSync.

    POST /v1/sync/catalog   ← الوكيل، بمفتاح جهاز (لا JWT مستخدم)

وإدارة المستودعات ومفاتيحها من صاحبها (JWT عادي):

    POST   /v1/warehouses
    GET    /v1/warehouses
    POST   /v1/warehouses/{id}/tokens
    DELETE /v1/warehouses/{id}/tokens/{token_id}
    GET    /v1/warehouses/{id}/tokens
    GET    /v1/warehouses/{id}/syncs

تسلسل الاستقبال:
  مفتاح ← حجم ← (فك gzip محروس) ← بصمة ← مكرّر؟ ← تحليل ← قفل المستودع ←
  دمج مع الحالة السابقة ← كتابة ثابتة (خام + حالة + تغيّرات أسعار) ←
  سجل مزامنة + ملف تحليل + وظيفة ← 202

الأصناف والأسعار لا تدخل Postgres (قاعدة ذهبية #2): الحالة Parquet ثابت بمفتاح
جديد لكل مزامنة، والجدول يحفظ أين هي فقط.
"""
from __future__ import annotations

import hashlib
import secrets
import tempfile
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

import polars as pl
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from phoenix.catalog_sync import CatalogPayloadError, merge_catalog, parse_catalog

from ..config import get_settings
from ..db.models import Dataset, Job, SyncRun, SyncToken, Warehouse
from ..deps import CurrentUser, SessionDep
from ..security.rate_limit import client_ip, enforce
from ..services.audit import record
from ..storage.client import get_storage, raw_key
from ..workers.queue import enqueue_processing

router = APIRouter(prefix="/v1", tags=["sync"])

TOKEN_PREFIX = "fsk_"


def _err(code: int, error_code: str, message_ar: str, details=None) -> HTTPException:
    return HTTPException(code, detail={"error_code": error_code,
                                       "message_ar": message_ar, "details": details})


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ------------------------------------------------------------------ النماذج

class WarehouseIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class WarehouseOut(BaseModel):
    id: str
    name: str
    active_item_count: int
    last_sync_at: datetime | None
    created_at: datetime


class TokenIn(BaseModel):
    label: str | None = Field(default=None, max_length=255)


class TokenOut(BaseModel):
    token_id: str
    prefix: str
    # يُعرض مرة واحدة فقط — لا نحفظه ولا نستطيع إظهاره لاحقاً.
    token: str


class TokenInfo(BaseModel):
    """ما يُعرض عن مفتاح بعد إنشائه — البادئة للتعرّف عليه، لا المفتاح نفسه."""
    token_id: str
    prefix: str
    label: str | None
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


class SyncStats(BaseModel):
    received: int
    added: int
    price_changed: int
    removed: int
    reactivated: int
    active_total: int


class SyncOut(BaseModel):
    sync_id: str
    warehouse_id: str
    duplicate: bool
    dataset_id: str | None
    job_id: str | None
    stats: SyncStats
    received_at: datetime


class SyncRunOut(SyncOut):
    mode: str
    generated_at: datetime | None


def _wh_out(w: Warehouse) -> WarehouseOut:
    return WarehouseOut(id=w.id, name=w.name, active_item_count=w.active_item_count,
                        last_sync_at=w.last_sync_at, created_at=w.created_at)


def _run_out(r: SyncRun, duplicate: bool) -> SyncOut:
    return SyncOut(sync_id=r.id, warehouse_id=r.warehouse_id, duplicate=duplicate,
                   dataset_id=r.dataset_id, job_id=r.job_id, received_at=r.received_at,
                   stats=SyncStats(received=r.item_count, added=r.added,
                                   price_changed=r.price_changed, removed=r.removed,
                                   reactivated=r.reactivated, active_total=r.active_total))


async def _own_warehouse(warehouse_id: str, user, db) -> Warehouse:
    w = await db.get(Warehouse, warehouse_id)
    if w is None or w.user_id != user.id:          # 404 لا 403 — لا نكشف الوجود
        raise _err(404, "warehouse_not_found", "المستودع غير موجود.")
    return w


# ------------------------------------------------------------ إدارة المستودع

@router.post("/warehouses", response_model=WarehouseOut, status_code=201)
async def create_warehouse(body: WarehouseIn, user: CurrentUser, db: SessionDep,
                           request: Request) -> WarehouseOut:
    w = Warehouse(user_id=user.id, name=body.name.strip())
    db.add(w)
    await db.flush()
    await record(db, user_id=user.id, action="warehouse.create", resource=w.id,
                 ip=client_ip(request))
    await db.commit()
    await db.refresh(w)
    return _wh_out(w)


@router.get("/warehouses", response_model=list[WarehouseOut])
async def list_warehouses(user: CurrentUser, db: SessionDep) -> list[WarehouseOut]:
    res = await db.execute(select(Warehouse).where(Warehouse.user_id == user.id)
                           .order_by(Warehouse.created_at))
    return [_wh_out(w) for w in res.scalars()]


@router.post("/warehouses/{warehouse_id}/tokens", response_model=TokenOut, status_code=201)
async def create_token(warehouse_id: str, body: TokenIn, user: CurrentUser,
                       db: SessionDep, request: Request) -> TokenOut:
    w = await _own_warehouse(warehouse_id, user, db)
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    t = SyncToken(warehouse_id=w.id, token_hash=hash_token(token),
                  prefix=token[:10], label=body.label)
    db.add(t)
    await db.flush()
    await record(db, user_id=user.id, action="sync_token.create", resource=t.id,
                 ip=client_ip(request))
    await db.commit()
    return TokenOut(token_id=t.id, prefix=t.prefix, token=token)


@router.get("/warehouses/{warehouse_id}/tokens", response_model=list[TokenInfo])
async def list_tokens(warehouse_id: str, user: CurrentUser, db: SessionDep) -> list[TokenInfo]:
    w = await _own_warehouse(warehouse_id, user, db)
    res = await db.execute(select(SyncToken).where(SyncToken.warehouse_id == w.id)
                           .order_by(SyncToken.created_at.desc()))
    return [TokenInfo(token_id=t.id, prefix=t.prefix, label=t.label, created_at=t.created_at,
                      last_used_at=t.last_used_at, revoked_at=t.revoked_at)
            for t in res.scalars()]


@router.delete("/warehouses/{warehouse_id}/tokens/{token_id}", status_code=204)
async def revoke_token(warehouse_id: str, token_id: str, user: CurrentUser,
                       db: SessionDep, request: Request) -> None:
    w = await _own_warehouse(warehouse_id, user, db)
    t = await db.get(SyncToken, token_id)
    if t is None or t.warehouse_id != w.id:
        raise _err(404, "token_not_found", "المفتاح غير موجود.")
    if t.revoked_at is None:
        t.revoked_at = datetime.now(timezone.utc)
        await record(db, user_id=user.id, action="sync_token.revoke", resource=t.id,
                     ip=client_ip(request))
        await db.commit()


@router.get("/warehouses/{warehouse_id}/syncs", response_model=list[SyncRunOut])
async def list_syncs(warehouse_id: str, user: CurrentUser, db: SessionDep,
                     limit: int = 50) -> list[SyncRunOut]:
    w = await _own_warehouse(warehouse_id, user, db)
    res = await db.execute(select(SyncRun).where(SyncRun.warehouse_id == w.id)
                           .order_by(SyncRun.received_at.desc()).limit(min(max(limit, 1), 200)))
    return [SyncRunOut(**_run_out(r, False).model_dump(), mode=r.mode,
                       generated_at=r.generated_at) for r in res.scalars()]


# ------------------------------------------------------------ استقبال الوكيل

async def _agent_token(db: SessionDep,
                       authorization: Annotated[str | None, Header()] = None) -> SyncToken:
    bad = _err(401, "invalid_sync_token",
               "مفتاح المزامنة غير صالح أو أُلغي. أنشئ مفتاحاً جديداً من إعدادات المستودع.")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise bad
    token = authorization.split(" ", 1)[1].strip()
    if not token.startswith(TOKEN_PREFIX):
        raise bad
    res = await db.execute(select(SyncToken).where(SyncToken.token_hash == hash_token(token)))
    t = res.scalars().first()
    if t is None or t.revoked_at is not None:
        raise bad
    return t


async def _read_body(request: Request, limit: int) -> bytes:
    too_big = _err(413, "payload_too_large",
                   f"حمولة المزامنة تتجاوز الحد ({limit // (1024 * 1024)} ميغابايت).")
    buf = bytearray()
    async for chunk in request.stream():
        buf.extend(chunk)
        if len(buf) > limit:
            raise too_big
    if request.headers.get("content-encoding", "").lower() == "gzip":
        # gzip يوفّر ~٨٠٪ من الرفع على إنترنت ضعيف. نفكّ بسقف: قنبلة ضغط
        # لا تستهلك ذاكرة أكثر من الحد نفسه.
        d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        try:
            out = d.decompress(bytes(buf), limit + 1)
        except zlib.error:
            raise _err(400, "bad_gzip", "الحمولة مُعلَنة gzip لكنها تالفة.") from None
        if len(out) > limit or d.unconsumed_tail:
            raise too_big
        return out
    return bytes(buf)


@router.post("/sync/catalog", response_model=SyncOut, status_code=202,
             responses={200: {"model": SyncOut, "description": "حمولة مكرّرة — المزامنة الأصلية"}})
async def sync_catalog(request: Request, db: SessionDep,
                       token: Annotated[SyncToken, Depends(_agent_token)]):
    settings = get_settings()
    await enforce(request, f"sync:{token.id}", 30)

    raw = await _read_body(request, settings.max_file_size_bytes)
    if not raw:
        raise _err(400, "empty_payload", "الحمولة فارغة.")
    digest = hashlib.sha256(raw).hexdigest()

    async def existing() -> SyncRun | None:
        r = await db.execute(select(SyncRun).where(SyncRun.warehouse_id == token.warehouse_id,
                                                   SyncRun.content_sha256 == digest))
        return r.scalars().first()

    # إعادة محاولة بعد انقطاع: نفس الجواب، بلا مزامنة ثانية ولا أرقام مضاعفة.
    if (prev := await existing()) is not None:
        return JSONResponse(status_code=200, content=_run_out(prev, True).model_dump(mode="json"))

    try:
        snap = parse_catalog(raw)
    except CatalogPayloadError as e:
        raise _err(422, e.code, e.message_ar) from None

    # قفل صف المستودع: مزامنتان متزامنتان تُدمجان واحدة بعد الأخرى، لا فوق
    # نفس الحالة القديمة فتضيع إحداهما.
    wh = (await db.execute(select(Warehouse).where(Warehouse.id == token.warehouse_id)
                           .with_for_update())).scalars().one()
    if (prev := await existing()) is not None:
        body = _run_out(prev, True).model_dump(mode="json")   # قبل rollback
        await db.rollback()
        return JSONResponse(status_code=200, content=body)

    # رقم المستودع في رأس الحمولة يُثبَّت مع أول مزامنة. رقمٌ مختلف لاحقاً
    # بنفس المفتاح = وكيل نُسخ لجهاز مستودع آخر بإعداده؛ دمجهما يخلط كتالوجين.
    if snap.warehouse_id is not None:
        if wh.external_id is None:
            wh.external_id = snap.warehouse_id
        elif wh.external_id != snap.warehouse_id:
            bound = wh.external_id        # يُقرأ قبل rollback — بعده يصير الكائن منتهياً
            await db.rollback()
            raise _err(409, "warehouse_mismatch",
                       f"هذا المفتاح مربوط بالمستودع «{bound}» والحمولة من "
                       f"«{snap.warehouse_id}». أنشئ مستودعاً ومفتاحاً منفصلين له.")

    storage = get_storage()
    now = datetime.now(timezone.utc)
    with tempfile.TemporaryDirectory(prefix="phoenix-sync-") as tmp:
        tmp = Path(tmp)
        previous = None
        if wh.catalog_state_key:
            local = storage.fetch_to_local(settings.s3_bucket_processed,
                                           wh.catalog_state_key, tmp / "prev")
            previous = pl.read_parquet(local)
        result = merge_catalog(previous, snap, now)

        run = SyncRun(warehouse_id=wh.id, token_id=token.id, content_sha256=digest,
                      mode=snap.mode, generated_at=snap.generated_at, received_at=now,
                      item_count=result.stats.received, added=result.stats.added,
                      price_changed=result.stats.price_changed, removed=result.stats.removed,
                      reactivated=result.stats.reactivated,
                      active_total=result.stats.active_total, raw_key="", state_key="")
        db.add(run)
        await db.flush()

        ds = Dataset(user_id=wh.user_id, name=f"كتالوج {wh.name} — {now:%Y-%m-%d %H:%M}",
                     file_size=len(raw), content_sha256=digest, status="pending")
        db.add(ds)
        await db.flush()

        # الخام يُكتب مرة واحدة ويُحلَّل بخط الأنابيب المعتاد (قاعدة #3).
        run.raw_key = ds.raw_key = raw_key(wh.user_id, ds.id, "catalog.json")
        raw_path = tmp / "catalog.json"
        raw_path.write_bytes(raw)
        storage.put_file(settings.s3_bucket_raw, run.raw_key, raw_path)

        base = f"{wh.user_id}/catalog/{wh.id}"
        run.state_key = f"{base}/state-{run.id}.parquet"
        state_path = tmp / "state.parquet"
        result.state.write_parquet(state_path)
        storage.put_file(settings.s3_bucket_processed, run.state_key, state_path)

        if result.price_changes.height:
            run.price_changes_key = f"{base}/price-changes-{run.id}.parquet"
            ch_path = tmp / "changes.parquet"
            result.price_changes.write_parquet(ch_path)
            storage.put_file(settings.s3_bucket_processed, run.price_changes_key, ch_path)

    job = Job(dataset_id=ds.id, user_id=wh.user_id)
    db.add(job)
    await db.flush()
    run.dataset_id, run.job_id = ds.id, job.id

    wh.catalog_state_key = run.state_key
    wh.active_item_count = result.stats.active_total
    wh.last_sync_at = now
    wh.last_sync_id = run.id
    token.last_used_at = now
    await record(db, user_id=wh.user_id, action="sync.catalog", resource=run.id,
                 ip=client_ip(request))
    await db.commit()

    await enqueue_processing(job.id)
    return _run_out(run, False)
