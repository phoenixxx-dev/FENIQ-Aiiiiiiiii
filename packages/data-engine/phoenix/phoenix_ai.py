"""PhoenixAI — نقطة الدخول الموحَّدة الموصى بها بعد اكتمال طبقة AI بالكامل.

يجمع كل طبقات Phoenix AI Layer v1 فوق النظام الحالي بلا أي تعديل عليه:
RuleRouter (كما هو) ← Intent Quality Gate (كما هو) ← [proceed مباشر] أو
[escalate: AIQuestionRouter/CompoundQuestionHandler ← LLM Intent Resolver ←
ToolCall Validator] ← ToolExecutor (كما هو) ← [تفسير اختياري: AIExplainer].

مطابق لـ"Architecture Diagram نصي نهائي" بالقسم 14 من المواصفة المعمارية،
وهو "نقطة الدمج الوحيدة" المذكورة بالقسم 11 — كل هذا الملف إضافة جديدة بالكامل؛
لا تعديل على RuleRouter أو ToolExecutor أو AnalyticsEngine أو AskPhoenix.

مفتاح التفعيل (قسم 13): افتراضياً مغلق. مغلق = سلوك اليوم تماماً (RuleRouter+
Gate فقط، أي escalate يعني unknown، بلا أي استدعاء LLM ولا AIExplainer).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from .ai_explainer import AIExplainer
from .ai_question_router import AIQuestionRouter
from .ask import RuleRouter, ToolExecutor
from .compound_question_handler import CompoundQuestionHandler
from .intent_quality_gate import IntentQualityGate
from .llm_intent_resolver import LLMIntentResolver
from .models import Answer, DatasetProfile, SemanticSchema, ToolCall
from .tool_call_validator import ToolCallValidator

_ENV_FLAG = "PHOENIX_AI_LAYER_ENABLED"
_TRUTHY = {"1", "true", "yes", "on"}


def _flag_from_env() -> bool:
    return os.environ.get(_ENV_FLAG, "").strip().lower() in _TRUTHY


@dataclass
class PhoenixAI:
    """الواجهة الموصى بها: PhoenixAI(engine, schema).ask(question) -> Answer.

    كل معامل AI اختياري ومحقون من الخارج (resolver بلا call_fn = Gemini حقيقي
    عبر GEMINI_API_KEY من البيئة). بلا resolver، الطبقة كلها معطَّلة تلقائياً
    بغض النظر عن enabled — fail-safe كامل، لا استثناء ممكن.
    """
    engine: object
    schema: SemanticSchema
    profile: DatasetProfile | None = None
    resolver: LLMIntentResolver | None = None
    explainer: AIExplainer | None = None
    enabled: bool | None = None  # None = اقرأ من متغيّر البيئة

    def __post_init__(self) -> None:
        self.executor = ToolExecutor(self.engine, self.schema)
        self.rule_router = RuleRouter()
        self.gate = IntentQualityGate(router=self.rule_router)
        self.validator = ToolCallValidator()
        self.ai_router = AIQuestionRouter(resolver=self.resolver, validator=self.validator) \
            if self.resolver is not None else None
        self.compound = CompoundQuestionHandler(
            resolver=self.resolver, executor=self.executor, validator=self.validator,
        ) if self.resolver is not None else None

    @property
    def _is_enabled(self) -> bool:
        flag = _flag_from_env() if self.enabled is None else self.enabled
        return flag and self.resolver is not None

    @property
    def llm_calls(self) -> int:
        """عدد نداءات النموذج في آخر سؤال — تستعمله طبقة الـAPI لحساب الكلفة."""
        return sum(getattr(c, "llm_calls", 0) for c in (self.resolver, self.explainer) if c)

    def ask(self, question: str) -> Answer:
        # تصفير قبل كل سؤال: العدّاد يخصّ هذا السؤال لا عمر الكائن
        for component in (self.resolver, self.explainer):
            if component is not None:
                component.llm_calls = 0

        gate_result = self.gate.evaluate(question, self.schema, profile=self.profile)

        if gate_result.proceed:
            # المسار الحتمي الحالي بلا أي لمسة AI — أسرع وأوثق مسار (قسم 4، حالة 1)
            return self.executor.execute(gate_result.tool_call, question)

        if not self._is_enabled:
            # المفتاح مغلق أو لا يوجد resolver مُركَّب: نفس سلوك اليوم تماماً
            return self.executor.execute(ToolCall(tool="unknown", arguments={}), question)

        answers = self.compound.handle(question, self.schema, gate_result)
        primary = answers[0]
        if primary.tool_calls[0].tool == "unknown" or self.explainer is None:
            return primary

        explained_text = self.explainer.explain(question, answers)
        return primary.model_copy(update={"answer_ar": explained_text})
