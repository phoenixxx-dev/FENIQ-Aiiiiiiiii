"""GatedRouter — نقطة الدمج الوحيدة لطبقة AI الجديدة مع النظام الحالي.

قسم 1 + قسم 11 بالمواصفة المعمارية: "نقطة الدمج الوحيدة هي اختيار أي router
يُمرَّر لـAskPhoenix (معامل موجود أصلاً بالـconstructor)". هذا الصنف ينفّذ
QuestionRouter Protocol نفسه (نفس واجهة RuleRouter تماماً)، فيمكن تمريره
مكان RuleRouter بلا أي تعديل على AskPhoenix أو ToolExecutor أو AnalyticsEngine.

التسلسل (قسم 1، Architecture Diagram قسم 14):
المستخدم ← RuleRouter (دائماً، بلا تعديل) ← Intent Quality Gate (حتمي) ←
  [proceed] رجوع مباشر بالـToolCall كما هو، بلا أي لمسة AI
  [escalate] فقط إذا كان المفتاح مفعَّلاً: AIQuestionRouter ← LLM Intent
             Resolver ← ToolCall Validator. غير مفعَّل = unknown مباشرة
             (قسم 4، الحالة 2 — fail-safe كامل، مطابق لسلوك اليوم).

مفتاح التفعيل (قسم 13): متغيّر بيئة PHOENIX_AI_LAYER_ENABLED، افتراضه مغلق.
إغلاقه وحده يعيد النظام بالكامل لسلوك RuleRouter+Gate فقط، بلا أي تعديل كود.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from .ai_question_router import AIQuestionRouter
from .ask import RuleRouter
from .intent_quality_gate import IntentQualityGate
from .models import DatasetProfile, SemanticSchema, ToolCall

_ENV_FLAG = "PHOENIX_AI_LAYER_ENABLED"
_TRUTHY = {"1", "true", "yes", "on"}


def _flag_from_env() -> bool:
    return os.environ.get(_ENV_FLAG, "").strip().lower() in _TRUTHY


@dataclass
class GatedRouter:
    """ينفّذ QuestionRouter Protocol. مرّره لـAskPhoenix(router=...) بدل
    RuleRouter الافتراضي لتفعيل طبقة AI — نقطة الدمج الوحيدة بالنظام كله.
    """
    ai_question_router: AIQuestionRouter | None = None
    profile: DatasetProfile | None = None
    enabled: bool | None = None  # None = اقرأ من متغيّر البيئة (قسم 13)
    rule_router: RuleRouter = field(default_factory=RuleRouter)
    gate: IntentQualityGate | None = None

    def __post_init__(self) -> None:
        if self.gate is None:
            self.gate = IntentQualityGate(router=self.rule_router)

    @property
    def _is_enabled(self) -> bool:
        return _flag_from_env() if self.enabled is None else self.enabled

    def route(self, question: str, schema: SemanticSchema) -> ToolCall:
        gate_result = self.gate.evaluate(question, schema, profile=self.profile)

        if gate_result.proceed:
            # المسار الحتمي الحالي، بلا أي لمسة AI — أسرع وأوثق مسار (قسم 4، حالة 1)
            return gate_result.tool_call

        if not self._is_enabled or self.ai_question_router is None:
            # المفتاح مغلق أو لا يوجد AIQuestionRouter مُركَّب: نفس سلوك اليوم
            # تماماً — escalate يعني unknown مباشرة، بلا أي استدعاء LLM (قسم 4، حالة 2)
            return ToolCall(tool="unknown", arguments={})

        return self.ai_question_router.route_escalated(question, schema, gate_result)
