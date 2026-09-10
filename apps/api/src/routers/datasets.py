"""رفع الملفات وقراءة نتائجها."""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Request, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, update

from phoenix.models import (ChangeLog, CleaningRecipe, DatasetProfile as ProfileModel,
                            SemanticSchema)
from phoenix.currency import available_currencies
from phoenix.semantic import UnknownConceptError, available_concepts

from ..config import get_settings
from ..db.models import (CleaningRecipeRow, Conversation, Dataset,
                         DatasetProfile, DatasetSchema, InsightRow, Job,
                         Message)
from ..deps import CurrentUser, SessionDep, get_user_dataset, list_user_datasets
from ..security.rate_limit import client_ip
from ..services.audit import record
from ..services.dataset_service import claim_for_processing
from ..storage.cache import invalidate
from ..storage.client import get_storage, raw_key, sha256_of
from ..workers.queue import enqueue_processing

router = APIRouter(prefix="/datasets", tags=["datasets"])

log = logging.getLogger("phoenix.datasets")

CHUNK = 1024 * 1024


class DatasetOut(BaseModel):
    id: str
    name: str
    status: str
    duplicate_of: str | None = None
    domain: str
    detected_format: str | None = None
    row_count: int | None = None
    column_count: int | None = None
    file_size: int | None = None
    warnings: list[str] = []
    error_ar: str | None = None


class UploadOut(BaseModel):
    dataset_id: str
    job_id: str
    duplicate_of: str | None = None


def _to_out(ds: Dataset) -> DatasetOut:
    return DatasetOut(
        id=ds.id, name=ds.name, status=ds.status, domain=ds.domain,
        duplicate_of=ds.duplicate_of,
        detected_format=ds.detected_format, row_count=ds.row_count,
        column_count=ds.column_count, file_size=ds.file_size,
        warnings=list(ds.warnings_json or []), error_ar=ds.error_ar,
    )


@router.post("", response_model=UploadOut, status_code=status.HTTP_202_ACCEPTED)
async def upload_dataset(user: CurrentUser, db: SessionDep, request: Request,
                         file: UploadFile = File(...)) -> UploadOut:
    """يستقبل الملف، يخزّنه كما هو، ويُرجع job_id فوراً.

    قاعدة ذهبية #6: لا معالجة هنا — المعالجة في الـworker. هذا الـendpoint
    مهمته النقل والتسجيل فقط.
    """
    settings = get_settings()
    storage = get_storage()

    with tempfile.TemporaryDirectory(prefix="phoenix-upload-") as tmp:
        local = Path(tmp) / (Path(file.filename or "upload.dat").name)
        size = 0
        with open(local, "wb") as out:
            while chunk := await file.read(CHUNK):
                size += len(chunk)
                if size > settings.max_file_size_bytes:
                    raise HTTPException(
                        status.HTTP_413_CONTENT_TOO_LARGE,
                        detail={"error_code": "file_too_large",
                                "message_ar": f"الملف يتجاوز الحد المسموح "
                                              f"({settings.max_file_size_mb} ميغابايت)."})
                out.write(chunk)
        if size == 0:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                detail={"error_code": "empty_file",
                                        "message_ar": "الملف فارغ — تأكد أنك اخترت الملف الصحيح."})

        digest = sha256_of(local)

        # بصمة الملف: نفس التصدير لا يُعالَج مرتين (ADR-001)
        dup = await db.execute(
            select(Dataset).where(Dataset.user_id == user.id,
                                  Dataset.content_sha256 == digest,
                                  Dataset.status == "ready")
        )
        duplicate = dup.scalars().first()

        ds = Dataset(user_id=user.id, name=Path(file.filename or "ملف").name,
                     file_size=size, content_sha256=digest, status="pending")
        db.add(ds)
        await db.flush()

        key = raw_key(user.id, ds.id, ds.name)
        storage.put_file(settings.s3_bucket_raw, key, local)
        ds.raw_key = key

        job = Job(dataset_id=ds.id, user_id=user.id)
        db.add(job)
        await record(db, user_id=user.id, action="dataset.upload",
                     resource=ds.id, ip=client_ip(request))
        await db.commit()

    await enqueue_processing(job.id)
    return UploadOut(dataset_id=ds.id, job_id=job.id,
                     duplicate_of=duplicate.id if duplicate else None)


@router.get("", response_model=list[DatasetOut])
async def list_datasets(user: CurrentUser, db: SessionDep) -> list[DatasetOut]:
    return [_to_out(d) for d in await list_user_datasets(user, db)]


class ConceptOut(BaseModel):
    concept: str
    role: str
    unit: str | None = None
    label_ar: str


@router.get("/concepts", response_model=list[ConceptOut])
def list_concepts() -> list[dict]:
    """المفاهيم التي يفهمها المحرك — تغذّي قائمة التصحيح في الواجهة.

    مصدرها قاموس المحرك نفسه، فلا تعرض الواجهة خياراً لا يفهمه المحرك.
    """
    return available_concepts()


class CurrencyOut(BaseModel):
    code: str
    symbol_ar: str


@router.get("/currencies", response_model=list[CurrencyOut])
def list_currencies() -> list[dict]:
    """العملات التي يعرفها المحرك — تغذّي قائمة اختيار العملة في الواجهة.

    الترتيب ليس أبجدياً بل ترتيب السجل في المحرك، وأوّله عملات السوق الأساسي.
    """
    return available_currencies()


@router.get("/{dataset_id}", response_model=DatasetOut)
async def get_dataset(dataset_id: str, user: CurrentUser, db: SessionDep) -> DatasetOut:
    return _to_out(await get_user_dataset(dataset_id, user, db))


async def _one(db: SessionDep, model, dataset_id: str):
    res = await db.execute(select(model).where(model.dataset_id == dataset_id))
    return res.scalars().first()


@router.delete("/{dataset_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_dataset(dataset_id: str, user: CurrentUser, db: SessionDep,
                         request: Request) -> None:
    """حذف نهائي للملف وكل ما اشتُقّ منه.

    القاعدة الذهبية #3 تمنع **المعالجة** من المساس بالملف الأصلي؛ لا تمنع
    صاحب البيانات من حذفها. بيانات مالية للعملاء بلا زر حذف ليست ميزة أمان،
    بل عيب. الحذف هنا صريح، بطلب المالك، ومسجَّل في سجل التدقيق.
    """
    ds = await get_user_dataset(dataset_id, user, db)
    settings = get_settings()
    storage = get_storage()

    # التخزين أولاً: لو فشلت قاعدة البيانات بعده نكون تركنا ملفاً يتيماً
    # (يُنظَّف لاحقاً)، وهو أهون من سجل يشير إلى ملف محذوف.
    for bucket, key in ((settings.s3_bucket_raw, ds.raw_key),
                        (settings.s3_bucket_processed, ds.processed_key)):
        if not key:
            continue
        try:
            storage.delete(bucket, key)
        except Exception as e:                                    # noqa: BLE001
            log.warning("تعذّر حذف كائن من التخزين",
                        extra={"extra_fields": {"bucket": bucket, "key": key,
                                                "error": str(e)}})
    if ds.processed_key:
        invalidate(ds.processed_key)

    for model in (DatasetSchema, DatasetProfile, CleaningRecipeRow, InsightRow):
        await db.execute(delete(model).where(model.dataset_id == ds.id))
    await db.execute(delete(Message).where(Message.conversation_id.in_(
        select(Conversation.id).where(Conversation.dataset_id == ds.id))))
    await db.execute(delete(Conversation).where(Conversation.dataset_id == ds.id))
    await db.execute(delete(Job).where(Job.dataset_id == ds.id))
    # الملفات الأخرى التي أشارت لهذا كنسخة مكرّرة تفقد إشارتها لا سجلّها
    await db.execute(update(Dataset).where(Dataset.duplicate_of == ds.id)
                     .values(duplicate_of=None))
    await db.delete(ds)
    await record(db, user_id=user.id, action="dataset.delete",
                 resource=ds.id, ip=client_ip(request))
    await db.commit()


@router.get("/{dataset_id}/schema", response_model=SemanticSchema)
async def get_schema(dataset_id: str, user: CurrentUser, db: SessionDep) -> dict:
    ds = await get_user_dataset(dataset_id, user, db)
    row = await _one(db, DatasetSchema, ds.id)
    if row is None:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            detail={"error_code": "not_ready",
                                    "message_ar": "الملف قيد المعالجة — انتظر قليلاً، الصفحة ستتحدّث وحدها."})
    return row.semantic_schema_json


class ColumnOverrideIn(BaseModel):
    column_name: str = Field(min_length=1, max_length=512)
    # None = «هذا العمود لا يعني شيئاً» — قرار صالح ويجب أن يُحترم
    concept: str | None = None
    role: str | None = None
    unit: str | None = None


class SchemaPatchIn(BaseModel):
    # الأعمدة اختيارية الآن: قد يكون كل ما يريده المستخدم تحديد العملة
    columns: list[ColumnOverrideIn] = Field(default_factory=list, max_length=200)
    # رمز العملة (SYP/USD…) — يعلو على أي استنتاج. `null` صريحة تعني «امسح
    # اختياري وعُد للاستنتاج»، وغيابُ الحقل يعني «لا تلمس ما اخترته سابقاً».
    currency: str | None = None
    set_currency: bool = False


class SchemaPatchOut(BaseModel):
    dataset_id: str
    job_id: str


@router.patch("/{dataset_id}/schema", response_model=SchemaPatchOut,
              status_code=status.HTTP_202_ACCEPTED)
async def patch_schema(dataset_id: str, body: SchemaPatchIn, user: CurrentUser,
                       db: SessionDep, request: Request) -> SchemaPatchOut:
    """تصحيح المستخدم لدور عمود — الخطة §5.4 تجعل هذه الواجهة إجبارية.

    التصحيح لا يُعدّل الـschema المحفوظة فقط: يُعاد تشغيل المعالجة من الملف
    الأصلي بالتصحيح مطبَّقاً، لأن كل ما بعد الدلالة يعتمد عليها (التنظيف،
    الأعمدة المشتقّة، التحليلات، الاكتشافات). تحديث الـschema وحدها كان
    سيترك أرقاماً محسوبة على فهم قديم — وهذا أسوأ من عدم التصحيح.
    """
    ds = await get_user_dataset(dataset_id, user, db)
    if ds.status not in ("ready", "failed"):
        raise HTTPException(status.HTTP_409_CONFLICT,
                            detail={"error_code": "not_ready",
                                    "message_ar": "الملف قيد المعالجة الآن — انتظر انتهاءها ثم أعد المحاولة."})
    if not ds.raw_key:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            detail={"error_code": "no_raw_file",
                                    "message_ar": "الملف الأصلي غير متاح — ارفعه من جديد لإعادة الحساب."})

    known_columns = set()
    res = await db.execute(select(DatasetSchema).where(DatasetSchema.dataset_id == ds.id))
    row = res.scalars().first()
    if row is not None:
        known_columns = {c.get("column_name") for c in row.semantic_schema_json.get("columns", [])}

    if not body.columns and not body.set_currency:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            detail={"error_code": "empty_patch",
                                    "message_ar": "لم تُرسل أي تصحيح."})

    if body.set_currency:
        known = {c["code"] for c in available_currencies()}
        if body.currency is not None and body.currency not in known:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                detail={"error_code": "unknown_currency",
                                        "message_ar": f"عملة غير معروفة: «{body.currency}»."})
        ds.currency_override = body.currency

    merged = dict(ds.schema_overrides_json or {})
    for c in body.columns:
        if known_columns and c.column_name not in known_columns:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                detail={"error_code": "unknown_column",
                                        "message_ar": f"لا يوجد عمود باسم «{c.column_name}»."})
        merged[c.column_name] = {"concept": c.concept, "role": c.role, "unit": c.unit}

    # نتحقق من المفاهيم عبر المحرك قبل الحفظ — القاموس يعيش هناك لا هنا
    try:
        available = {x["concept"] for x in available_concepts()}
        for name, patch in merged.items():
            if patch.get("concept") is not None and patch["concept"] not in available:
                raise UnknownConceptError(patch["concept"])
    except UnknownConceptError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            detail={"error_code": "unknown_concept",
                                    "message_ar": f"مفهوم غير معروف: «{e}»."}) from e

    ds.schema_overrides_json = merged
    if not await claim_for_processing(db, ds.id):
        raise HTTPException(status.HTTP_409_CONFLICT,
                            detail={"error_code": "already_processing",
                                    "message_ar": "طلب إعادة حساب جارٍ بالفعل على هذا الملف."})
    job = Job(dataset_id=ds.id, user_id=user.id)
    db.add(job)
    await record(db, user_id=user.id, action="dataset.schema_override",
                 resource=ds.id, ip=client_ip(request))
    await db.commit()

    await enqueue_processing(job.id)
    return SchemaPatchOut(dataset_id=ds.id, job_id=job.id)


@router.get("/{dataset_id}/profile", response_model=ProfileModel)
async def get_profile(dataset_id: str, user: CurrentUser, db: SessionDep) -> dict:
    ds = await get_user_dataset(dataset_id, user, db)
    row = await _one(db, DatasetProfile, ds.id)
    if row is None:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            detail={"error_code": "not_ready",
                                    "message_ar": "الملف قيد المعالجة — انتظر قليلاً، الصفحة ستتحدّث وحدها."})
    return row.profile_json


class CleaningOut(BaseModel):
    recipe: CleaningRecipe
    changelog: ChangeLog
    # المفاتيح المطفأة الآن — بدونها تعرض الواجهة حالة خاطئة للمستخدم
    disabled: list[str] = []


@router.get("/{dataset_id}/cleaning", response_model=CleaningOut)
async def get_cleaning(dataset_id: str, user: CurrentUser, db: SessionDep) -> dict:
    """«ماذا تغيّر في بياناتي؟» — الوصفة وسجل التغييرات معاً.

    نُرجع المعطَّل صراحةً أيضاً: بدونه لا تعرف الواجهة أي مفتاح مطفأ الآن،
    فتعرض حالة خاطئة للمستخدم.
    """
    ds = await get_user_dataset(dataset_id, user, db)
    row = await _one(db, CleaningRecipeRow, ds.id)
    if row is None:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            detail={"error_code": "not_ready",
                                    "message_ar": "الملف قيد المعالجة — انتظر قليلاً، الصفحة ستتحدّث وحدها."})
    return {"recipe": row.operations_json, "changelog": row.changelog_json,
            "disabled": list(ds.cleaning_disabled_json or [])}


class CleaningPatchIn(BaseModel):
    # قائمة المفاتيح المعطَّلة كاملةً — لا فرق بين «أطفئ» و«أشعل»، الواجهة
    # ترسل الحالة النهائية فيبقى الخادم بلا حالة وسيطة غامضة.
    disabled: list[str] = Field(default_factory=list, max_length=100)


@router.patch("/{dataset_id}/cleaning", response_model=SchemaPatchOut,
              status_code=status.HTTP_202_ACCEPTED)
async def patch_cleaning(dataset_id: str, body: CleaningPatchIn, user: CurrentUser,
                         db: SessionDep, request: Request) -> SchemaPatchOut:
    """تعطيل/تفعيل عملية تنظيف ثم إعادة التشغيل (الخطة §6.3).

    التنظيف اقتراح لا حكم: المستخدم قد يعرف أن اسمين متشابهين هما فعلاً
    عميلان مختلفان. رفضه يجب أن يُعيد الحساب من الملف الأصلي، لا أن يخفي
    سطراً من السجل فقط.
    """
    ds = await get_user_dataset(dataset_id, user, db)
    if ds.status not in ("ready", "failed"):
        raise HTTPException(status.HTTP_409_CONFLICT,
                            detail={"error_code": "not_ready",
                                    "message_ar": "الملف قيد المعالجة الآن — انتظر انتهاءها ثم أعد المحاولة."})

    row = await _one(db, CleaningRecipeRow, ds.id)
    known = {op.get("key") for op in ((row.operations_json or {}).get("operations", [])
                                      if row else [])}
    for key in body.disabled:
        if known and key not in known:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                detail={"error_code": "unknown_operation",
                                        "message_ar": "عملية تنظيف غير معروفة — قد تكون الصفحة قديمة، حدّثها وأعد المحاولة."})

    ds.cleaning_disabled_json = list(dict.fromkeys(body.disabled))
    if not await claim_for_processing(db, ds.id):
        raise HTTPException(status.HTTP_409_CONFLICT,
                            detail={"error_code": "already_processing",
                                    "message_ar": "طلب إعادة حساب جارٍ بالفعل على هذا الملف."})
    job = Job(dataset_id=ds.id, user_id=user.id)
    db.add(job)
    await record(db, user_id=user.id, action="dataset.cleaning_override",
                 resource=ds.id, ip=client_ip(request))
    await db.commit()

    await enqueue_processing(job.id)
    return SchemaPatchOut(dataset_id=ds.id, job_id=job.id)


@router.get("/{dataset_id}/insights")
async def get_insights(dataset_id: str, user: CurrentUser, db: SessionDep) -> list[dict]:
    ds = await get_user_dataset(dataset_id, user, db)
    res = await db.execute(
        select(InsightRow).where(InsightRow.dataset_id == ds.id).order_by(InsightRow.score.desc())
    )
    return [{"id": r.id, "type": r.type, "severity": r.severity,
             "title_ar": r.title_ar, "body_ar": r.body_ar,
             "evidence": r.evidence_json, "score": r.score}
            for r in res.scalars().all()]
