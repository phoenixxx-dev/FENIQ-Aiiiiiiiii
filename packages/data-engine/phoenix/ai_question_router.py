"""AIQuestionRouter — Phoenix AI Layer v1، القسم 2 من المواصفة المعمارية.

طبقة تنسيق فقط. تُستدعى حصراً عند قرار escalate من Intent Quality Gate.
مسؤوليتها: استدعاء LLM Intent Resolver، ثم تمرير الناتج إجبارياً عبر
ToolCall Validator قبل أي شيء آخر (قسم 1: "هذه بوابة إضافية لا استثناء منها
إطلاقاً، حتى لو الـLLM يبدو واثقاً"). لا تتخذ أي قرار حسابي أو دلالي بنفسها.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .intent_quality_gate import GateResult
from .llm_intent_resolver import LLMIntentResolver
from .models import SemanticSchema, ToolCall
from .tool_call_validator import ToolCallValidator


@dataclass
class AIQuestionRouter:
    resolver: LLMIntentResolver
    validator: ToolCallValidator = field(default_factory=ToolCallValidator)

    def route_escalated(self, question: str, schema: SemanticSchema,
                        gate_result: GateResult) -> ToolCall:
        """يُستدعى فقط بعد أن قرر الـGate escalate=True. لا فحص إضافي هنا
        لقرار proceed/escalate — تلك مسؤولية الـGate وحده."""
        raw_call = self.resolver.resolve(question, schema, gate_result)
        result = self.validator.validate(raw_call, schema)
        # result.tool_call هو الأصلي عند النجاح، أو ToolCall(tool="unknown")
        # عند الرفض — بلا إعادة محاولة تلقائية، بلا تصحيح صامت (قسم 6).
        return result.tool_call
