"""الاعتماديات المشتركة: الجلسة، المستخدم الحالي، وحارس عزل البيانات."""
from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db.models import Dataset, SyncRun, User, Warehouse
from .db.session import get_session
from .security.auth import ACCESS, decode_token

SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def get_current_user(
    db: SessionDep,
    authorization: Annotated[str | None, Header()] = None,
) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            detail={"error_code": "unauthorized",
                                    "message_ar": "الرجاء تسجيل الدخول أولاً."})
    user_id = decode_token(authorization.split(" ", 1)[1].strip(), ACCESS)
    if not user_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            detail={"error_code": "invalid_token",
                                    "message_ar": "الجلسة منتهية أو غير صالحة."})
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            detail={"error_code": "invalid_token",
                                    "message_ar": "الجلسة منتهية أو غير صالحة."})
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


async def get_user_dataset(dataset_id: str, user: User, db: AsyncSession) -> Dataset:
    """⚠️ كل وصول لأي ملف يمر من هنا. بلا استثناء.

    نُرجع 404 لا 403 عمداً: 403 يكشف أن الملف موجود لمستخدم آخر.
    """
    ds = await db.get(Dataset, dataset_id)
    if ds is None or ds.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            detail={"error_code": "dataset_not_found",
                                    "message_ar": "الملف غير موجود."})
    return ds


async def list_user_datasets(user: User, db: AsyncSession) -> list[Dataset]:
    # awaiting_upload / uploaded = سجل محجوز لم تبدأ معالجته بعد. عرضه
    # للمستخدم يعني صفاً فارغاً لا يفتح — فنخفيه حتى تبدأ المعالجة فعلاً.
    # ملفات المزامنة: أحدثها فقط لكل مستودع (الأقدم يبقى متاحاً برابطه وسجلّه).
    latest_synced = (select(SyncRun.dataset_id)
                     .join(Warehouse, Warehouse.last_sync_id == SyncRun.id)
                     .where(Warehouse.user_id == user.id))
    res = await db.execute(
        select(Dataset)
        .where(Dataset.user_id == user.id,
               Dataset.status.notin_(("awaiting_upload", "uploaded")),
               Dataset.warehouse_id.is_(None) | Dataset.id.in_(latest_synced))
        .order_by(Dataset.created_at.desc())
    )
    return list(res.scalars().all())
