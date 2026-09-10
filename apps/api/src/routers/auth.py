"""التسجيل / الدخول / تجديد الجلسة."""
from __future__ import annotations

import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select

from ..db.models import User
from ..deps import CurrentUser, SessionDep
from ..security.auth import (REFRESH, create_access_token, create_refresh_token,
                             hash_password, hash_password_async, token_claims,
                             verify_password_async)
from ..security.refresh_store import is_used, mark_used
from ..security.rate_limit import client_ip, limit_login
from ..services.audit import record

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    name: str | None = None


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class RefreshIn(BaseModel):
    refresh_token: str


class TokenOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


def _tokens(user_id: str) -> TokenOut:
    return TokenOut(access_token=create_access_token(user_id),
                    refresh_token=create_refresh_token(user_id))


@router.post("/register", response_model=TokenOut, status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(limit_login)])
async def register(body: RegisterIn, db: SessionDep, request: Request) -> TokenOut:
    existing = await db.execute(select(User).where(User.email == body.email.lower()))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            detail={"error_code": "email_taken",
                                    "message_ar": "البريد مستخدم مسبقاً."})
    # Argon2 مصمَّم ليكون بطيئاً عمداً (~150ms). تنفيذه داخل حلقة الأحداث
    # يجمّد **كل** الطلبات الأخرى طوال هذه المدة — ظهر ذلك في اختبار الحِمل:
    # وسيط زمن التسجيل قفز إلى 3.7 ثانية مع 30 مستخدماً متزامناً.
    password_hash = await hash_password_async(body.password)
    user = User(email=body.email.lower(), password_hash=password_hash,
                name=body.name)
    db.add(user)
    await db.flush()
    await record(db, user_id=user.id, action="auth.register",
                 resource=user.email, ip=client_ip(request))
    await db.commit()
    return _tokens(user.id)


# تجزئة ثابتة لكلمة مرور عشوائية — تُحسب مرة واحدة عند الإقلاع، وتُستعمل
# لمساواة زمن الفشل بزمن النجاح عند بريد غير موجود.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(32))


@router.post("/login", response_model=TokenOut, dependencies=[Depends(limit_login)])
async def login(body: LoginIn, db: SessionDep, request: Request) -> TokenOut:
    res = await db.execute(select(User).where(User.email == body.email.lower()))
    user = res.scalar_one_or_none()
    # نفس الرسالة في الحالتين — لا نكشف إن كان البريد مسجّلاً أصلاً.
    # ونفس **الزمن** أيضاً: لو رجعنا فوراً عند بريد غير موجود، لصار الفرق
    # الزمني بحد ذاته كاشفاً لأي بريد مسجّل عندنا. لذلك نتحقق دائماً — من
    # التجزئة الحقيقية أو من تجزئة وهمية.
    # والتنفيذ في مجمّع خيوط محدود: Argon2 بطيء عمداً ويجمّد حلقة الأحداث.
    ok = await verify_password_async(
        body.password, user.password_hash if user else _DUMMY_HASH)
    if not ok or user is None:
        await record(db, user_id=user.id if user else None, action="auth.login_failed",
                     resource=body.email.lower(), ip=client_ip(request))
        await db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            detail={"error_code": "bad_credentials",
                                    "message_ar": "البريد أو كلمة المرور غير صحيحة."})
    await record(db, user_id=user.id, action="auth.login", ip=client_ip(request))
    await db.commit()
    return _tokens(user.id)


@router.post("/refresh", response_model=TokenOut)
async def refresh(body: RefreshIn, db: SessionDep, request: Request) -> TokenOut:
    """تجديد الجلسة — مع **تدوير** التوكن (الخطة §9.1).

    كل توكن تجديد يُستهلك مرة واحدة فقط. إعادة استعماله مرفوضة، وهي في
    الوقت نفسه إشارة إلى أن نسخة منه بيد غير صاحبها — فتُسجَّل في سجل
    التدقيق.
    """
    claims = token_claims(body.refresh_token, REFRESH)
    expired = HTTPException(status.HTTP_401_UNAUTHORIZED,
                            detail={"error_code": "invalid_token",
                                    "message_ar": "الجلسة منتهية. سجّل الدخول مجدداً."})
    if claims is None:
        raise expired
    user_id = claims["sub"]
    if await db.get(User, user_id) is None:
        raise expired

    jti = claims.get("jti")
    if jti:
        if await is_used(jti):
            await record(db, user_id=user_id, action="auth.refresh_reuse",
                         resource=jti, ip=client_ip(request))
            await db.commit()
            raise expired
        remaining = int(claims.get("exp", 0) - datetime.now(timezone.utc).timestamp())
        await mark_used(jti, remaining)

    return _tokens(user_id)


class MeOut(BaseModel):
    id: str
    email: str
    name: str | None


@router.get("/me", response_model=MeOut)
async def me(user: CurrentUser) -> MeOut:
    return MeOut(id=user.id, email=user.email, name=user.name)
