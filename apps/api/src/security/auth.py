"""المصادقة: Argon2id لكلمات المرور + JWT (access/refresh)."""
from __future__ import annotations

import asyncio
import os
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt
from passlib.context import CryptContext

from ..config import get_settings

_pwd = CryptContext(schemes=["argon2"], deprecated="auto")

ACCESS = "access"
REFRESH = "refresh"


# مجمّع خيوط مخصّص للتجزئة، بحجم عدد الأنوية.
#
# لماذا ليس داخل حلقة الأحداث؟ Argon2 بطيء عمداً (~150ms) فيجمّد كل الطلبات.
# ولماذا ليس asyncio.to_thread العام؟ مجمّعه الافتراضي يصل إلى 32 خيطاً،
# فتتزاحم 30 عملية تجزئة على نواتين وتبطؤ كلُّها معاً — قِسناه: الوسيط ساء
# من 3.7s إلى 7.0s. الحدّ بعدد الأنوية يجعلها تنتظر دورها وتُنفَّذ بسرعتها
# الكاملة، وحلقة الأحداث تبقى حرّة طوال الوقت.
_HASH_POOL = ThreadPoolExecutor(
    max_workers=max(2, len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity")
                    else (os.cpu_count() or 2)),
    thread_name_prefix="argon2",
)


async def hash_password_async(raw: str) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_HASH_POOL, hash_password, raw)


async def verify_password_async(raw: str, hashed: str) -> bool:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_HASH_POOL, verify_password, raw, hashed)


def hash_password(raw: str) -> str:
    return _pwd.hash(raw)


def verify_password(raw: str, hashed: str) -> bool:
    try:
        return _pwd.verify(raw, hashed)
    except Exception:
        return False


def _create_token(user_id: str, kind: str, ttl: timedelta) -> str:
    s = get_settings()
    now = datetime.now(timezone.utc)
    payload = {"sub": user_id, "type": kind,
               # jti: معرّف فريد لكل توكن. بدونه لا يمكن تمييز نسخة مستهلَكة
               # من أخرى، فلا تدوير ولا كشف لإعادة الاستعمال.
               "jti": secrets.token_urlsafe(12),
               "iat": int(now.timestamp()),
               "exp": int((now + ttl).timestamp())}
    return jwt.encode(payload, s.jwt_secret, algorithm=s.jwt_algorithm)


def token_claims(token: str, expected_type: str) -> dict | None:
    """يُرجع كل الادعاءات (jti وexp ضمنها) أو None لو التوكن غير صالح."""
    s = get_settings()
    try:
        payload = jwt.decode(token, s.jwt_secret, algorithms=[s.jwt_algorithm])
    except JWTError:
        return None
    if payload.get("type") != expected_type:
        return None
    return payload if isinstance(payload.get("sub"), str) else None


def create_access_token(user_id: str) -> str:
    s = get_settings()
    return _create_token(user_id, ACCESS, timedelta(minutes=s.jwt_access_ttl_minutes))


def create_refresh_token(user_id: str) -> str:
    s = get_settings()
    return _create_token(user_id, REFRESH, timedelta(days=s.jwt_refresh_ttl_days))


def decode_token(token: str, expected_type: str) -> str | None:
    """يُرجع user_id، أو None لو التوكن غير صالح/منتهٍ/من نوع خاطئ."""
    s = get_settings()
    try:
        payload = jwt.decode(token, s.jwt_secret, algorithms=[s.jwt_algorithm])
    except JWTError:
        return None
    if payload.get("type") != expected_type:
        return None
    sub = payload.get("sub")
    return sub if isinstance(sub, str) else None


UPLOAD = "upload"


def create_upload_token(user_id: str, dataset_id: str, ttl_seconds: int) -> str:
    """تذكرة رفع قصيرة العمر تربط المستخدم بملف واحد بعينه.

    تُستعمل فقط في مسار التخزين المحلي (تطوير)، حيث لا يوجد توقيع S3 أصلي.
    فائدتها الأمنية نفسها: من يملك الرابط يستطيع الكتابة على هذا المفتاح
    وحده، ولمدة دقائق فقط.
    """
    s = get_settings()
    now = datetime.now(timezone.utc)
    payload = {"sub": user_id, "type": UPLOAD, "ds": dataset_id,
               "iat": int(now.timestamp()),
               "exp": int((now + timedelta(seconds=ttl_seconds)).timestamp())}
    return jwt.encode(payload, s.jwt_secret, algorithm=s.jwt_algorithm)


def decode_upload_token(token: str) -> tuple[str, str] | None:
    """يُرجع (user_id, dataset_id) أو None لو التذكرة غير صالحة."""
    s = get_settings()
    try:
        payload = jwt.decode(token, s.jwt_secret, algorithms=[s.jwt_algorithm])
    except JWTError:
        return None
    if payload.get("type") != UPLOAD:
        return None
    sub, ds = payload.get("sub"), payload.get("ds")
    if isinstance(sub, str) and isinstance(ds, str):
        return sub, ds
    return None
