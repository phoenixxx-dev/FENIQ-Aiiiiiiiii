"""الرفع المباشر إلى التخزين (presigned) — الخطة §6.3.

لماذا؟ ملف 100 ميغابايت يمر عبر الـAPI يعني ذاكرة ومهلة وعنق زجاجة عند كل
رفع. الحل المعياري: الخادم يوقّع رابطاً، والمتصفح يرفع إلى التخزين مباشرة.

مساران خلف نفس العقد:
  • S3/MinIO  → رابط PUT موقّع حقيقي؛ **الملف لا يمر عبر الـAPI إطلاقاً**.
  • تخزين محلي (تطوير) → لا توقيع أصلي، فنعطي مسار PUT على الـAPI محمياً
    بتذكرة قصيرة العمر. العقد للواجهة واحد في الحالتين، فلا تتغيّر شيفرتها
    عند الانتقال للإنتاج.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..config import get_settings
from ..db.models import Dataset, Job
from ..deps import CurrentUser, SessionDep, get_user_dataset
from ..security.auth import create_upload_token, decode_upload_token
from ..security.rate_limit import client_ip
from ..services.audit import record
from ..storage.client import LocalStorage, get_storage, raw_key
from ..workers.queue import enqueue_processing

router = APIRouter(tags=["uploads"])

UPLOAD_TICKET_TTL_SECONDS = 15 * 60
CHUNK = 1024 * 1024

AWAITING = "awaiting_upload"
UPLOADED = "uploaded"


class UploadUrlIn(BaseModel):
    filename: str = Field(min_length=1, max_length=512)
    size: int = Field(ge=1)


class UploadUrlOut(BaseModel):
    dataset_id: str
    url: str
    method: str = "PUT"
    headers: dict[str, str] = {}
    expires_in: int = UPLOAD_TICKET_TTL_SECONDS
    # هل يذهب الملف للتخزين مباشرة؟ الواجهة لا تحتاجه، لكنه يجعل الفرق
    # ظاهراً في الاختبارات والتشخيص بدل أن يكون خفياً.
    direct: bool



# ---------------------------------------------------------------- مساحة القرص
#
# قرار المالك (ق-48): الملفات تُخزَّن على قرص السيرفر نفسه لا على خدمة
# تخزين خارجية. الفرق العملي أن **المساحة صارت مورداً محدوداً**: قرص ممتلئ
# لا يمنع الرفع وحده، بل يوقف قاعدة البيانات والسجلات والمعالجة معاً.
#
# لذلك نرفض الرفع **قبل** أن يبدأ، لا بعد أن يمتلئ القرص.
#
# لماذا ثلاثة أضعاف الحجم: الملف الخام يُحفظ، ثم يُنتَج ملف معالَج بحجم
# مشابه، ثم تُكتب نسخة مؤقتة أثناء المعالجة.
DISK_SAFETY_FACTOR = 3
# واحد ونصف جيجابايت تبقى دائماً لنظام التشغيل وقاعدة البيانات والسجلات.
DISK_RESERVE_BYTES = 1_536 * 1024 * 1024


def ensure_disk_space(size: int) -> None:
    free = get_storage().free_bytes()
    if free is None:                     # تخزين سحابي: لا سقف عملي
        return
    needed = size * DISK_SAFETY_FACTOR + DISK_RESERVE_BYTES
    if free < needed:
        raise HTTPException(
            status.HTTP_507_INSUFFICIENT_STORAGE,
            detail={"error_code": "disk_full",
                    "message_ar": "لا توجد مساحة كافية على الخادم لاستقبال هذا "
                                  "الملف الآن. احذف ملفات قديمة من صفحة «ملفاتي» "
                                  "ثم أعد المحاولة، أو تواصل معنا."})


@router.post("/datasets/upload-url", response_model=UploadUrlOut,
             status_code=status.HTTP_201_CREATED)
async def create_upload_url(body: UploadUrlIn, user: CurrentUser, db: SessionDep,
                            request: Request) -> UploadUrlOut:
    """يحجز سجل ملف ويُرجع رابط رفع. لا بايت واحد يمر من هنا."""
    settings = get_settings()
    if body.size > settings.max_file_size_bytes:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            detail={"error_code": "file_too_large",
                    "message_ar": f"الملف يتجاوز الحد المسموح "
                                  f"({settings.max_file_size_mb} ميغابايت)."})
    ensure_disk_space(body.size)

    name = Path(body.filename).name
    ds = Dataset(user_id=user.id, name=name, file_size=body.size, status=AWAITING)
    db.add(ds)
    await db.flush()
    ds.raw_key = raw_key(user.id, ds.id, name)
    await record(db, user_id=user.id, action="dataset.upload_url",
                 resource=ds.id, ip=client_ip(request))
    await db.commit()

    signed = get_storage().presign_put(settings.s3_bucket_raw, ds.raw_key,
                                       UPLOAD_TICKET_TTL_SECONDS)
    if signed:
        return UploadUrlOut(dataset_id=ds.id, url=signed, direct=True)

    ticket = create_upload_token(user.id, ds.id, UPLOAD_TICKET_TTL_SECONDS)
    return UploadUrlOut(dataset_id=ds.id, url=f"/uploads/{ticket}", direct=False)


@router.put("/uploads/{ticket}", status_code=status.HTTP_204_NO_CONTENT)
async def put_upload(ticket: str, request: Request, db: SessionDep) -> None:
    """مسار الرفع للتخزين المحلي فقط — بديل عن توقيع S3 في التطوير.

    لا يستعمل CurrentUser عمداً: التذكرة الموقّعة هي الهوية هنا، تماماً كما
    أن الرابط الموقّع من S3 هو الهوية هناك.
    """
    settings = get_settings()
    storage = get_storage()
    # مع S3 هذا المسار لا معنى له: الرفع يذهب للتخزين مباشرة بلا مرورنا.
    if not isinstance(storage, LocalStorage):
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            detail={"error_code": "not_found",
                                    "message_ar": "هذا المسار غير متاح في هذا الإعداد."})

    claims = decode_upload_token(ticket)
    if claims is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            detail={"error_code": "invalid_upload_ticket",
                                    "message_ar": "انتهت صلاحية الرفع. اختر الملف وارفعه من جديد."})
    user_id, dataset_id = claims

    ds = await db.get(Dataset, dataset_id)
    if ds is None or ds.user_id != user_id or not ds.raw_key:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            detail={"error_code": "dataset_not_found",
                                    "message_ar": "الملف غير موجود."})
    # قاعدة ذهبية #3: RAW يُكتب مرة واحدة. التذكرة لا تُعاد على ملف مكتمل.
    if ds.status != AWAITING:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            detail={"error_code": "already_uploaded",
                                    "message_ar": "هذا الملف وصل بالفعل — لا حاجة لرفعه مرة ثانية."})

    size = 0
    with tempfile.TemporaryDirectory(prefix="phoenix-put-") as tmp:
        staged = Path(tmp) / "body"
        with open(staged, "wb") as out:
            async for chunk in request.stream():
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
        # النقل بعد اكتمال الاستلام: لا يظهر بالتخزين ملف نصف مرفوع
        storage.put_file(settings.s3_bucket_raw, ds.raw_key, staged)

    # ⚠️ ختم الحالة **بعد** نجاح الكتابة. بدونه تبقى التذكرة صالحة للكتابة
    # على نفس المفتاح مرة أخرى — خرق مباشر لقاعدة «RAW لا يُمس».
    ds.status = UPLOADED
    ds.file_size = size
    await db.commit()


class CompleteOut(BaseModel):
    dataset_id: str
    job_id: str


@router.post("/datasets/{dataset_id}/complete", response_model=CompleteOut,
             status_code=status.HTTP_202_ACCEPTED)
async def complete_upload(dataset_id: str, user: CurrentUser, db: SessionDep,
                          request: Request) -> CompleteOut:
    """يؤكّد وصول الملف للتخزين ثم يبدأ المعالجة.

    لا نثق بكلمة العميل: نتحقق من وجود الكائن وحجمه الفعلي في التخزين.
    """
    settings = get_settings()
    ds = await get_user_dataset(dataset_id, user, db)
    if not ds.raw_key:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            detail={"error_code": "no_upload_url",
                                    "message_ar": "لم يبدأ رفع هذا الملف. ابدأ الرفع من جديد."})

    existing = await db.execute(select(Job).where(Job.dataset_id == ds.id))
    job = existing.scalars().first()
    if job is not None:
        # تأكيد مكرر (شبكة متقطّعة مثلاً) — نُرجع نفس الوظيفة لا وظيفة ثانية
        return CompleteOut(dataset_id=ds.id, job_id=job.id)

    try:
        size = get_storage().size(settings.s3_bucket_raw, ds.raw_key)
    except FileNotFoundError:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            detail={"error_code": "upload_missing",
                                    "message_ar": "لم يكتمل رفع الملف. جرّب رفعه من جديد."}) from None
    if size == 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            detail={"error_code": "empty_file",
                                    "message_ar": "الملف فارغ — تأكد أنك اخترت الملف الصحيح."})
    if size > settings.max_file_size_bytes:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            detail={"error_code": "file_too_large",
                    "message_ar": f"الملف يتجاوز الحد المسموح "
                                  f"({settings.max_file_size_mb} ميغابايت)."})

    ds.file_size = size
    ds.status = "pending"
    job = Job(dataset_id=ds.id, user_id=user.id)
    db.add(job)
    await record(db, user_id=user.id, action="dataset.upload_complete",
                 resource=ds.id, ip=client_ip(request))
    await db.commit()

    await enqueue_processing(job.id)
    return CompleteOut(dataset_id=ds.id, job_id=job.id)
