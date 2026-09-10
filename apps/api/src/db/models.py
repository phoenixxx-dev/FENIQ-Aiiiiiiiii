"""جداول الميتاداتا فقط.

⚠️ قاعدة ذهبية #2: بيانات المستخدم الفعلية (صفوف ملفاته) لا تدخل Postgres أبداً.
مكانها: raw/ (الملف الأصلي) وprocessed/ (Parquet) في التخزين الكائني.
هون منخزّن: مين رفع، شو، الـschema، الـprofile، الاكتشافات، الوظائف، المحادثات.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (JSON, BigInteger, DateTime, Float, ForeignKey, Index,
                        Integer, String, Text, func)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _uuid() -> str:
    return str(uuid.uuid4())


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    locale: Mapped[str] = mapped_column(String(8), default="ar")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())

    datasets: Mapped[list[Dataset]] = relationship(back_populates="user")


class Dataset(Base):
    __tablename__ = "datasets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(512))
    domain: Mapped[str] = mapped_column(String(64), default="generic")
    # pending | processing | ready | failed
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    raw_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    processed_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    detected_format: Mapped[str | None] = mapped_column(String(16), nullable=True)
    row_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    column_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    file_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # بصمة الملف — نفس التصدير ما ينعالج مرتين (انظر ADR-001)
    content_sha256: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    # نفس الملف رُفع سابقاً؟ نشير للسجل الأقدم بدل منع الرفع — القرار للمستخدم.
    duplicate_of: Mapped[str | None] = mapped_column(String(36), nullable=True)
    warnings_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # تصحيحات المستخدم لأدوار الأعمدة — تُعاد مع كل معالجة، فقراره لا يضيع.
    schema_overrides_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # عمليات تنظيف رفضها المستخدم (بمفاتيحها الثابتة).
    cleaning_disabled_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # عملة يختارها المستخدم حين لا يوجد دليل عليها في الملف. عمودٌ مستقلّ لا
    # مفتاحٌ داخل تصحيحات الأعمدة: اسم العمود يأتي من ملف المستخدم، وأي مفتاح
    # محجوز داخل تلك الخريطة يصطدم يوماً باسم عمود حقيقي.
    currency_override: Mapped[str | None] = mapped_column(String(8), nullable=True)
    error_ar: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())

    user: Mapped[User] = relationship(back_populates="datasets")

    __table_args__ = (Index("ix_datasets_user_created", "user_id", "created_at"),)


class DatasetSchema(Base):
    """الـSemanticSchema المحفوظة (JSON) — العقد نفسه المعرّف في المحرك."""
    __tablename__ = "dataset_schemas"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    semantic_schema_json: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())


class DatasetProfile(Base):
    __tablename__ = "dataset_profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.id"), index=True)
    profile_json: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())


class CleaningRecipeRow(Base):
    __tablename__ = "cleaning_recipes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    operations_json: Mapped[dict] = mapped_column(JSON)
    changelog_json: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())


class InsightRow(Base):
    __tablename__ = "insights"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.id"), index=True)
    type: Mapped[str] = mapped_column(String(32))
    severity: Mapped[str] = mapped_column(String(16))
    title_ar: Mapped[str] = mapped_column(Text)
    body_ar: Mapped[str] = mapped_column(Text, default="")
    evidence_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    type: Mapped[str] = mapped_column(String(32), default="process_dataset")
    # queued | running | succeeded | failed
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    stage: Mapped[str] = mapped_column(String(32), default="queued")
    stage_ar: Mapped[str] = mapped_column(String(128), default="بانتظار الدور")
    error_ar: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                         nullable=True)


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    role: Mapped[str] = mapped_column(String(16))          # user | assistant
    content: Mapped[str] = mapped_column(Text)
    tool_calls_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    evidence_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    action: Mapped[str] = mapped_column(String(64))
    resource: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())
