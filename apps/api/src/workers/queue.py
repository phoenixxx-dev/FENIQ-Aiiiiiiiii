"""طابور المهام (ARQ فوق Redis).

سلوك مقصود: لو تعذّر الوصول للطابور، لا نُسقط الرفع — نُبقي الوظيفة بحالة
queued ونُرجع للمستخدم job_id، والـworker يلتقطها لاحقاً. الفشل الصامت ممنوع:
سبب عدم الإدراج يُسجَّل بسجلّات الخدمة.
"""
from __future__ import annotations

import logging

from arq.connections import RedisSettings, create_pool

from ..config import get_settings

log = logging.getLogger("phoenix.queue")

PROCESS_TASK = "process_dataset_task"


def redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(get_settings().redis_url)


async def enqueue_processing(job_id: str) -> bool:
    try:
        pool = await create_pool(redis_settings())
        try:
            await pool.enqueue_job(PROCESS_TASK, job_id)
            return True
        finally:
            await pool.aclose()
    except Exception as e:                                   # noqa: BLE001
        log.warning("تعذّر إدراج الوظيفة %s بالطابور: %s", job_id, e)
        return False
