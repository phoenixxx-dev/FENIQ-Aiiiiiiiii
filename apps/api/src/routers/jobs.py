"""حالة الوظائف — الفرونت يقرأ منها التقدّم نصاً عربياً.

مساران متكافئان:
  • GET /jobs/{id}          — لقطة واحدة (polling).
  • GET /jobs/{id}/stream   — بث لحظي (SSE): الخادم يدفع كل تغيّر فور حدوثه.

لماذا الاثنان؟ لأن الهاتف على شبكة ضعيفة قد يفقد البث، فيبقى الـpolling
شبكةَ أمان. الواجهة تبدأ بالبث وترتدّ للـpolling عند أي فشل.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..db.models import Job
from ..db.session import get_sessionmaker
from ..deps import CurrentUser, SessionDep

# فاصل السؤال عن الحالة داخل البث. أقصر من الـpolling (1.5 ثانية) لأن الكلفة
# استعلام واحد على مفتاح أساسي، بلا مصافحة HTTP جديدة في كل مرة.
STREAM_POLL_SECONDS = 0.5
# سقف صارم: أي بث مفتوح يستهلك اتصالاً. المعالجة الطويلة تتجاوزه فترتدّ
# الواجهة للـpolling — لا تتعطّل.
STREAM_MAX_SECONDS = 15 * 60

router = APIRouter(prefix="/jobs", tags=["jobs"])


class JobOut(BaseModel):
    id: str
    dataset_id: str
    status: str
    progress: int
    stage: str
    stage_ar: str
    error_ar: str | None = None


@router.get("/{job_id}", response_model=JobOut)
async def get_job(job_id: str, user: CurrentUser, db: SessionDep) -> JobOut:
    job = await db.get(Job, job_id)
    # 404 لا 403 — لا نكشف وجود وظيفة لمستخدم آخر
    if job is None or job.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            detail={"error_code": "job_not_found",
                                    "message_ar": "عملية المعالجة غير موجودة."})
    return JobOut(id=job.id, dataset_id=job.dataset_id, status=job.status,
                  progress=job.progress, stage=job.stage, stage_ar=job.stage_ar,
                  error_ar=job.error_ar)


def _sse(event: str, payload: dict) -> str:
    """يبني رسالة SSE واحدة. ensure_ascii=False حتى تصل العربية كما هي."""
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _snapshot(job: Job) -> dict:
    return {"id": job.id, "dataset_id": job.dataset_id, "status": job.status,
            "progress": job.progress, "stage": job.stage, "stage_ar": job.stage_ar,
            "error_ar": job.error_ar}


@router.get("/{job_id}/stream")
async def stream_job(job_id: str, request: Request, user: CurrentUser,
                     db: SessionDep) -> StreamingResponse:
    """بث تقدّم الوظيفة لحظياً (SSE).

    التحقق من الملكية يتم **قبل** بدء البث، حتى يصل 404 كخطأ HTTP عادي لا
    كحدث داخل بث ناجح.
    """
    job = await db.get(Job, job_id)
    if job is None or job.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            detail={"error_code": "job_not_found",
                                    "message_ar": "عملية المعالجة غير موجودة."})
    first = _snapshot(job)

    async def events() -> AsyncGenerator[str, None]:
        yield _sse("progress", first)
        if first["status"] in ("succeeded", "failed"):
            yield _sse("done", first)
            return

        last = first
        waited = 0.0
        maker = get_sessionmaker()
        while waited < STREAM_MAX_SECONDS:
            if await request.is_disconnected():
                return
            await asyncio.sleep(STREAM_POLL_SECONDS)
            waited += STREAM_POLL_SECONDS
            # جلسة جديدة في كل دورة: العامل (ARQ) عملية أخرى، ولو أعدنا
            # استخدام جلسة واحدة لبقينا نقرأ لقطة المعاملة القديمة نفسها.
            async with maker() as s:
                fresh = await s.get(Job, job_id)
                if fresh is None:
                    return
                cur = _snapshot(fresh)
            if cur != last:
                yield _sse("progress", cur)
                last = cur
            if cur["status"] in ("succeeded", "failed"):
                yield _sse("done", cur)
                return
        # انتهت المهلة والوظيفة ما زالت تعمل — نُبلغ الواجهة لترتدّ للـpolling
        yield _sse("timeout", last)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "Connection": "keep-alive"},
    )
