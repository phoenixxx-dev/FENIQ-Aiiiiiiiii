"""تخزين مؤقت للملفات المعالَجة على القرص المحلي للخادم.

المشكلة: كل طلب لوحة أو سؤال كان يُنزّل ملف Parquet من التخزين من جديد. مع
MinIO/S3 هذا نداء شبكة كامل لكل سؤال — والمستخدم يسأل عدة أسئلة على نفس الملف.

الحل: نسخة محلية مفتاحها اسم الكائن. الملفات المعالَجة **غير قابلة للتغيير**
(كل إعادة معالجة تكتب مفتاحاً جديداً)، فلا خطر من تقادم النسخة — وهذا ما يجعل
التخزين المؤقت آمناً هنا تحديداً.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

from ..config import get_settings
from .client import get_storage

log = logging.getLogger("phoenix.cache")

_lock = threading.Lock()
MAX_ENTRIES = 64
MAX_AGE_SECONDS = 6 * 60 * 60


def _cache_root() -> Path:
    root = Path(get_settings().local_storage_dir) / "_cache"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_name(key: str) -> str:
    return key.replace("/", "__").replace("\\", "__")


def fetch_processed(key: str) -> Path:
    """يُرجع مساراً محلياً للملف المعالَج، مُنزِّلاً إياه مرة واحدة فقط."""
    settings = get_settings()
    target = _cache_root() / _safe_name(key)

    if target.exists() and target.stat().st_size > 0:
        os.utime(target, None)          # آخر استخدام — يحمي المستعمَل من التنظيف
        return target

    with _lock:
        if target.exists() and target.stat().st_size > 0:
            return target
        tmp_dir = _cache_root() / f".dl-{os.getpid()}-{threading.get_ident()}"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        try:
            downloaded = get_storage().fetch_to_local(
                settings.s3_bucket_processed, key, tmp_dir)
            # النقل الذري يمنع أن يقرأ طلب آخر ملفاً نصف منزَّل
            os.replace(downloaded, target)
        finally:
            for leftover in tmp_dir.glob("*"):
                leftover.unlink(missing_ok=True)
            tmp_dir.rmdir()

    _evict_if_needed()
    return target


def _evict_if_needed() -> None:
    """تنظيف بسيط: الأقدم استخداماً يخرج أولاً. الفشل هنا لا يُسقط أي طلب."""
    try:
        entries = [p for p in _cache_root().iterdir() if p.is_file()]
        now = time.time()
        for p in entries:
            if now - p.stat().st_atime > MAX_AGE_SECONDS:
                p.unlink(missing_ok=True)

        entries = sorted((p for p in _cache_root().iterdir() if p.is_file()),
                         key=lambda p: p.stat().st_atime)
        for p in entries[:-MAX_ENTRIES] if len(entries) > MAX_ENTRIES else []:
            p.unlink(missing_ok=True)
    except Exception as e:                                   # noqa: BLE001
        log.warning("تعذّر تنظيف التخزين المؤقت: %s", e)


def invalidate(key: str) -> None:
    """تُستدعى عند إعادة معالجة ملف بنفس المفتاح."""
    try:
        (_cache_root() / _safe_name(key)).unlink(missing_ok=True)
    except Exception as e:                                   # noqa: BLE001
        log.warning("تعذّر إبطال النسخة المؤقتة (%s): %s", key, e)
