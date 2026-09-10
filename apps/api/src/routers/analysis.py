"""اللوحة، استعراض الجدول، والتصدير.

كل رقم هنا من المحرك مع دليله. طبقة الـAPI تنقل ولا تحسب.
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import tempfile
from pathlib import Path
from typing import Any

import polars as pl
from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select

from phoenix import charts
from phoenix.analytics import AnalyticsEngine
from phoenix.cleaning import outlier_flag_map
from phoenix.models import SemanticSchema

from ..config import get_settings
from ..db.models import Dataset, DatasetSchema, InsightRow
from ..deps import CurrentUser, SessionDep, get_user_dataset
from ..storage.cache import fetch_processed

router = APIRouter(prefix="/datasets", tags=["analysis"])

ROWS_PAGE_DEFAULT = 50
ROWS_PAGE_MAX = 200          # سقف مقصود: الهاتف هو الجهاز الأهم (docs/DECISIONS.md ق-4)


class DashboardOut(BaseModel):
    dataset_id: str
    kpis: list[dict]
    charts: list[dict]
    insights: list[dict]


async def _ready_dataset(dataset_id: str, user, db) -> tuple[Dataset, SemanticSchema]:
    ds = await get_user_dataset(dataset_id, user, db)
    if ds.status != "ready" or not ds.processed_key:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            detail={"error_code": "not_ready",
                                    "message_ar": "الملف قيد المعالجة — انتظر قليلاً، الصفحة ستتحدّث وحدها."})
    res = await db.execute(select(DatasetSchema).where(DatasetSchema.dataset_id == ds.id))
    row = res.scalars().first()
    if row is None:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            detail={"error_code": "not_ready",
                                    "message_ar": "الملف قيد المعالجة — انتظر قليلاً، الصفحة ستتحدّث وحدها."})
    return ds, SemanticSchema(**row.semantic_schema_json)


def _with_local_parquet(processed_key: str, fn):
    """ينفّذ العملية على نسخة محلية من الملف المعالَج.

    النسخة مخزَّنة مؤقتاً: الملفات المعالَجة غير قابلة للتغيير (كل إعادة معالجة
    تكتب مفتاحاً جديداً)، فإعادة تنزيلها في كل طلب نداء شبكة بلا فائدة.
    """
    return fn(fetch_processed(processed_key))


@router.get("/{dataset_id}/dashboard", response_model=DashboardOut)
async def dashboard(dataset_id: str, user: CurrentUser, db: SessionDep) -> DashboardOut:
    """كل ما تحتاجه شاشة النظرة العامة بطلب واحد — بدل أربع رحلات من الهاتف."""
    ds, schema = await _ready_dataset(dataset_id, user, db)

    timeout = get_settings().query_timeout_seconds

    def build(local: Path) -> tuple[list[dict], list[dict]]:
        engine = AnalyticsEngine(local, schema, timeout_seconds=timeout)
        try:
            kpis = charts.build_kpis(engine)
            specs = [c.model_dump(mode="json") for c in charts.suggest_charts(engine)]
            return kpis, specs
        finally:
            engine.close()

    kpis, specs = await asyncio.to_thread(_with_local_parquet, ds.processed_key, build)

    res = await db.execute(
        select(InsightRow).where(InsightRow.dataset_id == ds.id)
        .order_by(InsightRow.score.desc()).limit(5)
    )
    insights = [{"id": r.id, "type": r.type, "severity": r.severity,
                 "title_ar": r.title_ar, "body_ar": r.body_ar,
                 "evidence": r.evidence_json, "score": r.score}
                for r in res.scalars().all()]

    return DashboardOut(dataset_id=ds.id, kpis=kpis, charts=specs, insights=insights)


class RowsOut(BaseModel):
    columns: list[str]
    # {العمود الأصلي: عمود الوسم} — تستعمله الواجهة لتلوين الخلية الشاذة
    # بدل عرض عمود منطقي باسم داخلي. الاصطلاح يعيش في المحرك لا هنا.
    outlier_flags: dict[str, str] = {}
    rows: list[dict[str, Any]]
    page: int
    page_size: int
    total_rows: int
    total_pages: int


@router.get("/{dataset_id}/rows", response_model=RowsOut)
async def rows(
    dataset_id: str,
    user: CurrentUser,
    db: SessionDep,
    page: int = Query(1, ge=1),
    page_size: int = Query(ROWS_PAGE_DEFAULT, ge=1, le=ROWS_PAGE_MAX),
    sort_by: str | None = None,
    descending: bool = False,
    search: str | None = None,
) -> RowsOut:
    """استعراض الجدول بصفحات — لا نُنزّل 100 ألف صف على الهاتف أبداً."""
    ds, _schema = await _ready_dataset(dataset_id, user, db)

    def read(local: Path) -> RowsOut:
        lf = pl.scan_parquet(local)
        columns = lf.collect_schema().names()

        if sort_by and sort_by not in columns:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                detail={"error_code": "unknown_column",
                                        "message_ar": f"لا يوجد عمود باسم «{sort_by}»."})
        if search:
            # بحث نصّي على كل الأعمدة.
            #
            # ⚠️ الطرفان يُطبَّعان معاً. الخطأ السابق: تطبيع المُدخَل وحده
            # («شركة» ← «شركه») ومقارنته بقيم غير مطبَّعة — فأي كلمة فيها
            # ة أو أ أو ى لا تُطابق شيئاً أبداً، ومعظم الأسماء العربية كذلك.
            from phoenix.arabic_text import normalize_ar, normalize_expr
            needle = normalize_ar(search)
            # التطبيع الكامل للأعمدة النصّية فقط. تطبيع عمود أرقام أو تواريخ
            # عملٌ بلا أثر: لا حروف عربية فيه — وكان يضاعف زمن البحث.
            dtypes = lf.collect_schema()
            expr = None
            for c in columns:
                col = pl.col(c)
                e = (normalize_expr(col) if dtypes[c] == pl.Utf8
                     else col.cast(pl.Utf8, strict=False).str.to_lowercase()
                     ).str.contains(needle, literal=True)
                expr = e if expr is None else (expr | e)
            if expr is not None:
                lf = lf.filter(expr.fill_null(False))
        if sort_by:
            lf = lf.sort(sort_by, descending=descending, nulls_last=True)

        total = lf.select(pl.len()).collect().item()
        chunk = lf.slice((page - 1) * page_size, page_size).collect()
        return RowsOut(
            columns=columns,
            outlier_flags=outlier_flag_map(columns),
            rows=chunk.to_dicts(),
            page=page, page_size=page_size,
            total_rows=total,
            total_pages=max(1, (total + page_size - 1) // page_size),
        )

    return await asyncio.to_thread(_with_local_parquet, ds.processed_key, read)


# سقف صفوف تصدير إكسل. xlsxwriter يبني الملف خلية بخلية: 300 ألف صف ≈ 33
# ثانية — خرق صريح للقاعدة الذهبية #6. وإكسل نفسه يختنق عند هذا الحجم،
# فالصادق أن نقول ذلك ونقترح CSV بدل إبقاء المستخدم ينتظر دقيقة.
XLSX_MAX_ROWS = 100_000
STREAM_CHUNK = 256 * 1024


def _chunks(payload: bytes):
    """يقسّم الحمولة إلى قطع ثابتة.

    ⚠️ تمرير io.BytesIO إلى StreamingResponse يبدو صحيحاً وهو كارثي: ستارليت
    تتكرّر على الكائن، والتكرار على BytesIO **يُنتج سطراً سطراً**. ملف CSV
    فيه 300 ألف سطر صار 300 ألف قطعة إرسال — 54 ثانية لملف يُبنى في عُشر
    ثانية. القياس هو ما كشف ذلك، لا قراءة الكود.
    """
    for i in range(0, len(payload), STREAM_CHUNK):
        yield payload[i:i + STREAM_CHUNK]


@router.get("/{dataset_id}/export")
async def export(dataset_id: str, user: CurrentUser, db: SessionDep,
                 fmt: str = Query("csv", pattern="^(csv|xlsx)$")) -> StreamingResponse:
    """تصدير الملف المنظَّف. CSV بـBOM ليفتح صحيحاً بإكسل العربي."""
    ds, _schema = await _ready_dataset(dataset_id, user, db)

    if fmt == "xlsx" and (ds.row_count or 0) > XLSX_MAX_ROWS:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            detail={"error_code": "too_large_for_xlsx",
                    "message_ar": f"الملف أكبر من {XLSX_MAX_ROWS:,} صف — "
                                  f"صدّره بصيغة CSV، فهي أسرع ويفتحها إكسل."})

    def make(local: Path) -> bytes:
        df = pl.read_parquet(local)
        if fmt == "csv":
            out = io.BytesIO()
            # BOM: بدونه يعرض إكسل العربية كرموز مشوّهة
            out.write("﻿".encode("utf-8"))
            df.write_csv(out)
            return out.getvalue()
        out = io.BytesIO()
        df.write_excel(out)
        return out.getvalue()

    payload = await asyncio.to_thread(_with_local_parquet, ds.processed_key, make)

    stem = Path(ds.name).stem or "phoenix"
    media = ("text/csv; charset=utf-8" if fmt == "csv"
             else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    return StreamingResponse(
        _chunks(payload),
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{stem}_phoenix.{fmt}"',
                 "Content-Length": str(len(payload))},
    )
