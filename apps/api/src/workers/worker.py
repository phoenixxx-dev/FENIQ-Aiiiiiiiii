"""عامل المعالجة (ARQ) — يشغّل خط أنابيب فينيق خارج الـAPI تماماً.

التشغيل:  arq apps.api.src.workers.worker.WorkerSettings
"""
from __future__ import annotations

import logging

from ..db.session import get_sessionmaker
from ..observability import configure_logging
from ..services.dataset_service import run_processing_job
from ..services.maintenance import run_maintenance
from arq import cron

from .queue import PROCESS_TASK, redis_settings

log = logging.getLogger("phoenix.worker")


async def process_dataset_task(ctx: dict, job_id: str) -> str:
    """جلسة قاعدة البيانات تبقى في حلقة الأحداث نفسها؛ العمل الثقيل ينتقل إلى
    thread داخل run_processing_job نفسها (لا حلقات متداخلة)."""
    async with get_sessionmaker()() as db:
        await run_processing_job(db, job_id)
    return job_id


async def _startup(_ctx: dict) -> None:
    # نفس صيغة سجل الـAPI: سطر JSON واحد لكل حدث، فتقرأهما أداة واحدة
    configure_logging()


async def maintenance_task(_ctx: dict) -> dict:
    """تنظيف دوري: رفعات مهجورة + سياسة الاحتفاظ."""
    async with get_sessionmaker()() as db:
        return await run_maintenance(db)


class WorkerSettings:
    functions = [process_dataset_task]
    on_startup = _startup
    # الصيانة كل ساعة عند الدقيقة 7 — بعيداً عن رأس الساعة حتى لا تزاحم
    # مهامَّ أخرى تبدأ عادةً عندها.
    cron_jobs = [cron(maintenance_task, minute=7)]
    redis_settings = redis_settings()
    max_jobs = 4
    job_timeout = 600


# اسم المهمة كما يتوقعه الطابور
process_dataset_task.__name__ = PROCESS_TASK
