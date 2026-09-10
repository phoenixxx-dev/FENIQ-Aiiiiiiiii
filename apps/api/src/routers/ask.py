"""«اسأل فينيق» — سؤال عربي ← إجابة برقم من المحرك ودليل يشرح مصدره.

⚠️ قاعدة ذهبية #1: كل رقم يخرج من المحرك. هذه الطبقة لا تحسب شيئاً، ولا
تُنشئ نصاً فيه أرقام من عندها — فقط تنقل ما أنتجه المحرك مع دليله.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from phoenix.ai_explainer import AIExplainer
from phoenix.analytics import AnalyticsEngine
from phoenix.ask import RuleRouter, ToolExecutor, suggest_questions
from phoenix.llm_intent_resolver import LLMIntentResolver
from phoenix.models import SemanticSchema
from phoenix.phoenix_ai import PhoenixAI

from ..config import get_settings
from ..services.ai_budget import cost_of, within_budget
from ..db.models import Conversation, DatasetSchema, Message
from ..deps import CurrentUser, SessionDep, get_user_dataset
from ..security.rate_limit import limit_ask
from ..storage.cache import fetch_processed

router = APIRouter(prefix="/datasets", tags=["ask"])


class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=1000)


class AskOut(BaseModel):
    question: str
    understood_as: str
    answer_ar: str
    confidence: float
    tool: str
    # المقاييس كما أنتجها المحرك: القيمة + التنسيق العربي + الوحدة + الدليل.
    # الدليل مصدره metrics[i].evidence — ولا يُبنى في طبقة الـAPI إطلاقاً.
    metrics: list[dict] = []
    evidence: dict | None = None
    chart: dict | None = None
    # true عندما تُوقَف طبقة الذكاء لتجاوز سقف الشهر — الواجهة تُخبر
    # المستخدم بدل أن يظن أن الجودة تراجعت بلا سبب
    ai_paused_over_budget: bool = False


def _answer_sync(processed_local: Path, schema: SemanticSchema, question: str,
                 allow_ai: bool = True) -> dict:
    """يشغّل المحرك فعلياً — متزامن، لذا يُستدعى داخل thread.

    مساران:
      • طبقة الذكاء مغلقة (الافتراضي) ⇒ RuleRouter وحده، بلا أي نداء خارجي.
      • مفعّلة بمفتاح صالح ⇒ PhoenixAI: نفس المسار الحتمي للأسئلة المفهومة،
        وتصعيد للـLLM فقط لما يعجز الـGate — والأرقام تبقى من المحرك دائماً.
    """
    settings = get_settings()
    engine = AnalyticsEngine(processed_local, schema,
                             timeout_seconds=settings.query_timeout_seconds)
    try:
        if settings.ai_ready and allow_ai:
            resolver = LLMIntentResolver(api_key=settings.gemini_api_key)
            explainer = (AIExplainer(api_key=settings.gemini_api_key)
                         if settings.ai_explain_answers else None)
            phoenix = PhoenixAI(engine=engine, schema=schema, resolver=resolver,
                                explainer=explainer, enabled=True)
            answer = phoenix.ask(question)
            payload = json.loads(answer.model_dump_json())
            payload["llm_calls"] = phoenix.llm_calls
            return payload
        call = RuleRouter().route(question, schema)
        answer = ToolExecutor(engine, schema).execute(call, question)
        payload = json.loads(answer.model_dump_json())
        payload["llm_calls"] = 0
        return payload
    finally:
        engine.close()


@router.get("/{dataset_id}/suggestions", response_model=list[str])
async def suggestions(dataset_id: str, user: CurrentUser, db: SessionDep) -> list[str]:
    """أسئلة مقترحة مبنية على أعمدة هذا الملف بالذات.

    المحرك هو من يقرّرها: اقتراح سؤال لا يستطيع الإجابة عنه يعطي «لم أفهم»
    عند أول ضغطة — وهذا أسوأ انطباع أول ممكن.
    """
    ds = await get_user_dataset(dataset_id, user, db)
    res = await db.execute(select(DatasetSchema).where(DatasetSchema.dataset_id == ds.id))
    row = res.scalars().first()
    if row is None:
        return []
    return suggest_questions(SemanticSchema(**row.semantic_schema_json))


@router.post("/{dataset_id}/ask", response_model=AskOut,
             dependencies=[Depends(limit_ask)])
async def ask(dataset_id: str, body: AskIn, user: CurrentUser, db: SessionDep) -> AskOut:
    ds = await get_user_dataset(dataset_id, user, db)
    if ds.status != "ready" or not ds.processed_key:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            detail={"error_code": "not_ready",
                                    "message_ar": "الملف قيد المعالجة — انتظر قليلاً، الصفحة ستتحدّث وحدها."})

    res = await db.execute(select(DatasetSchema).where(DatasetSchema.dataset_id == ds.id))
    schema_row = res.scalars().first()
    if schema_row is None:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            detail={"error_code": "not_ready",
                                    "message_ar": "الملف قيد المعالجة — انتظر قليلاً، الصفحة ستتحدّث وحدها."})
    schema = SemanticSchema(**schema_row.semantic_schema_json)

    # نسخة محلية مؤقتة: المستخدم يسأل عدة أسئلة على نفس الملف، وتنزيله من
    # التخزين في كل مرة نداء شبكة كامل بلا فائدة.
    # سقف الإنفاق يُفحص **قبل** التصعيد لا بعده — بعده تكون التكلفة وقعت
    allow_ai, spent = await within_budget(db)
    local = await asyncio.to_thread(fetch_processed, ds.processed_key)
    payload = await asyncio.to_thread(_answer_sync, local, schema, body.question, allow_ai)
    cost = cost_of(int(payload.get("llm_calls", 0)))

    tool_calls = payload.get("tool_calls") or []
    tool = tool_calls[0]["tool"] if tool_calls else "unknown"
    metrics = payload.get("metrics") or []
    evidence = metrics[0].get("evidence") if metrics else None

    # حفظ المحادثة (ميتاداتا فقط)
    conv_res = await db.execute(
        select(Conversation).where(Conversation.dataset_id == ds.id,
                                   Conversation.user_id == user.id)
    )
    conv = conv_res.scalars().first()
    if conv is None:
        conv = Conversation(dataset_id=ds.id, user_id=user.id, title=body.question[:120])
        db.add(conv)
        await db.flush()
    db.add(Message(conversation_id=conv.id, role="user", content=body.question))
    db.add(Message(conversation_id=conv.id, role="assistant",
                   content=payload.get("answer_ar", ""),
                   tool_calls_json=tool_calls, evidence_json=evidence,
                   cost_usd=cost))
    await db.commit()

    return AskOut(
        question=body.question,
        understood_as=payload.get("understood_as", ""),
        answer_ar=payload.get("answer_ar", ""),
        confidence=float(payload.get("confidence", 0.0)),
        tool=tool,
        metrics=metrics,
        evidence=evidence,
        chart=payload.get("chart"),
        ai_paused_over_budget=(settings_ai_ready() and not allow_ai),
    )


def settings_ai_ready() -> bool:
    """هل طبقة الذكاء مُهيّأة أصلاً؟ بلا مفتاح لا معنى للحديث عن سقف."""
    return get_settings().ai_ready
