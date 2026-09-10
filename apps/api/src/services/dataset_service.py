"""تنسيق معالجة الملف: تخزين ← تشغيل المحرك ← حفظ الميتاداتا.

لا حسابات هنا — التنسيق فقط. كل رقم يأتي من المحرك.
"""
from __future__ import annotations

import asyncio
import logging
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db.models import (CleaningRecipeRow, Dataset, DatasetProfile,
                         DatasetSchema, InsightRow, Job)
from ..storage.cache import invalidate
from ..storage.client import get_storage, sha256_of
from .processing import STAGE_AR, ProcessingFailed, process_local_file

log = logging.getLogger("phoenix.job")


def _fetch_process_store(raw_key_: str, processed_key: str,
                         stages: list[tuple[str, int]],
                         schema_overrides: dict[str, dict] | None = None,
                         cleaning_disabled: list[str] | None = None,
                         currency_override: str | None = None) -> dict:
    """كل العمل الثقيل (شبكة/قرص/CPU) في مكان واحد متزامن.

    يُستدعى داخل thread — لا يلمس جلسة قاعدة البيانات إطلاقاً، لأن الجلسة تخصّ
    حلقة الأحداث التي أنشأتها، ونقلها لحلقة أخرى يكسرها (خطأ حقيقي وقعنا فيه).
    """
    settings = get_settings()
    storage = get_storage()

    def on_progress(key: str, _label: str, pct: int) -> None:
        stages.append((key, pct))

    with tempfile.TemporaryDirectory(prefix="phoenix-job-") as tmp:
        tmp_path = Path(tmp)
        local = storage.fetch_to_local(settings.s3_bucket_raw, raw_key_, tmp_path / "in")
        # البصمة تُحسب هنا لا عند الرفع: في المسار المباشر (presigned) الملف
        # لا يمر بالـAPI أصلاً، وحسابها بطلب HTTP كان سيخرق قاعدة #6.
        digest = sha256_of(local)
        result = process_local_file(local, run_dir=tmp_path / "run", on_progress=on_progress,
                                    schema_overrides=schema_overrides,
                                    cleaning_disabled=cleaning_disabled,
                                    currency_override=currency_override)
        result["content_sha256"] = digest
        # الملف المعالَج (Parquet) يُخزَّن قبل حذف المجلد المؤقت
        storage.put_file(settings.s3_bucket_processed, processed_key, result["processed_path"])
    return result


async def run_processing_job(db: AsyncSession, job_id: str) -> None:
    """ينفّذ وظيفة معالجة كاملة ويحدّث التقدّم مرحلة بمرحلة.

    تُستدعى من الـworker (ARQ) أو من اختبار — نفس الدالة بالضبط، حتى يكون ما
    نختبره هو ما يعمل في الإنتاج فعلاً.

    كل تعامل مع قاعدة البيانات يبقى في حلقة الأحداث الحالية، والعمل الثقيل
    يذهب إلى thread (قاعدة ذهبية #6).
    """
    job = await db.get(Job, job_id)
    if job is None:
        return
    ds = await db.get(Dataset, job.dataset_id)
    if ds is None or not ds.raw_key:
        return

    job.status, job.stage, job.stage_ar, job.progress = (
        "running", "validating", STAGE_AR["validating"], 5)
    ds.status = "processing"
    await db.commit()

    stages: list[tuple[str, int]] = []
    processed_key = f"{ds.user_id}/{ds.id}/processed.parquet"
    # إعادة المعالجة تكتب على نفس المفتاح، فلا بد من إسقاط النسخة المؤقتة
    # القديمة — وإلا استمر الخادم يجيب من بيانات ما قبل التنظيف الجديد.
    invalidate(processed_key)

    started = time.perf_counter()
    try:
        result = await asyncio.to_thread(_fetch_process_store, ds.raw_key, processed_key,
                                         stages, ds.schema_overrides_json or None,
                                         ds.cleaning_disabled_json or None,
                                         ds.currency_override or None)
    except ProcessingFailed as e:
        # السبب التقني في السجل فقط؛ المستخدم يرى رسالة عربية مفهومة
        log.error("processing failed", extra={"extra_fields": {
            "job_id": job.id, "dataset_id": ds.id, "user_id": ds.user_id,
            "reason_ar": e.message_ar, "technical": e.technical,
            "duration_ms": round((time.perf_counter() - started) * 1000)}})
        job.status, job.error_ar = "failed", e.message_ar
        job.finished_at = datetime.now(timezone.utc)
        ds.status, ds.error_ar = "failed", e.message_ar
        await db.commit()
        return

    if ds.content_sha256 is None:
        ds.content_sha256 = result["content_sha256"]
    if ds.duplicate_of is None:
        dup = await db.execute(
            select(Dataset).where(Dataset.user_id == ds.user_id,
                                  Dataset.content_sha256 == result["content_sha256"],
                                  Dataset.status == "ready",
                                  Dataset.id != ds.id).limit(1)
        )
        older = dup.scalars().first()
        if older is not None:
            ds.duplicate_of = older.id

    if stages:
        last_key, last_pct = stages[-1]
        job.stage, job.stage_ar, job.progress = (
            last_key, STAGE_AR.get(last_key, last_key), last_pct)

    # ميتاداتا فقط في Postgres (قاعدة ذهبية #2)
    await db.execute(delete(DatasetSchema).where(DatasetSchema.dataset_id == ds.id))
    await db.execute(delete(DatasetProfile).where(DatasetProfile.dataset_id == ds.id))
    await db.execute(delete(CleaningRecipeRow).where(CleaningRecipeRow.dataset_id == ds.id))
    await db.execute(delete(InsightRow).where(InsightRow.dataset_id == ds.id))

    db.add(DatasetSchema(dataset_id=ds.id, semantic_schema_json=result["schema_json"]))
    db.add(DatasetProfile(dataset_id=ds.id, profile_json=result["profile_json"]))
    db.add(CleaningRecipeRow(dataset_id=ds.id,
                             operations_json=result["recipe_json"],
                             changelog_json=result["changelog_json"]))
    for ins in result["insights"]:
        db.add(InsightRow(
            dataset_id=ds.id, type=ins.get("type", "info"),
            severity=ins.get("severity", "info"),
            title_ar=ins.get("title_ar", ""), body_ar=ins.get("description_ar", ""),
            evidence_json=ins.get("evidence"), score=float(ins.get("importance_score", 0.0)),
        ))

    ds.processed_key = processed_key
    ds.detected_format = result["detected_format"]
    ds.warnings_json = result["warnings"]
    ds.row_count = result["row_count"]
    ds.column_count = result["column_count"]
    ds.domain = result["domain"]
    ds.status = "ready"

    job.status, job.stage, job.stage_ar, job.progress = (
        "succeeded", "ready", STAGE_AR["ready"], 100)
    job.finished_at = datetime.now(timezone.utc)
    log.info("processing succeeded", extra={"extra_fields": {
        "job_id": job.id, "dataset_id": ds.id, "user_id": ds.user_id,
        "rows": ds.row_count, "columns": ds.column_count, "domain": ds.domain,
        "duration_ms": round((time.perf_counter() - started) * 1000)}})
    await db.commit()


async def claim_for_processing(db: AsyncSession, dataset_id: str) -> bool:
    """يحجز الملف للمعالجة ذرّياً. يُرجع False لو حجزه طلبٌ آخر.

    ⚠️ الفحص ثم الكتابة (`if ds.status == "ready": ds.status = "pending"`) ليس
    آمناً: طلبان متزامنان يقرآن «ready» معاً فينشئان وظيفتين على الملف نفسه —
    جُرِّب فعلياً وحدث. عاملان يعالجان الملف نفسه يحذفان ويكتبان الصفوف
    بالتناوب، والنتيجة حالة غير متوقَّعة.

    الشرط داخل جملة UPDATE نفسها يجعل قاعدة البيانات هي الحَكَم: أول طلب
    يغيّر صفاً، والثاني يغيّر صفراً.
    """
    res = await db.execute(
        update(Dataset)
        .where(Dataset.id == dataset_id, Dataset.status.in_(("ready", "failed")))
        .values(status="pending")
    )
    return res.rowcount == 1
