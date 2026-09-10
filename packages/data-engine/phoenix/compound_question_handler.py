"""CompoundQuestionHandler — Phoenix AI Layer v1، القسم 7 من المواصفة المعمارية.

مثال: «مين أكثر مندوب مبيعات وليش؟»

القرار المعماري الصريح (قسم 7): sub_calls لا تُنفَّذ كلها دفعة واحدة بالتوازي.
التنفيذ تسلسلي بمرحلتين إجباريتين:
- المرحلة الأولى: الـToolCall الأساسي يُنفَّذ عبر ToolExecutor، ينتج Evidence
  + النتيجة الفعلية.
- المرحلة الثانية: نتيجة المرحلة الأولى (لا السؤال من جديد) تُستخدَم لبناء
  الـToolCall الثاني فعلياً — هنا عبر LLM ثانٍ محدود النطاق
  (LLMIntentResolver.resolve_followup، بنفس عقد قسم 5).

كل ToolCall بالسلسلة يمر عبر نفس ToolCall Validator بشكل مستقل. الناتج قائمة
Answer منفصلة (لا دمج مصطنع بإجابة واحدة) — تصل لـAIExplainer دفعة واحدة
بالنهاية فقط (قسم 7، آخر سطر) لو أُريد تفسيرها.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .ask import ToolExecutor
from .intent_quality_gate import GateResult
from .llm_intent_resolver import LLMIntentResolver
from .models import Answer, SemanticSchema, ToolCall
from .tool_call_validator import ToolCallValidator


@dataclass
class CompoundQuestionHandler:
    resolver: LLMIntentResolver
    executor: ToolExecutor
    validator: ToolCallValidator = field(default_factory=ToolCallValidator)

    def handle(self, question: str, schema: SemanticSchema, gate_result: GateResult) -> list[Answer]:
        """يعيد قائمة Answer: عنصر واحد للأسئلة العادية (بلا sub_calls)، أو
        عنصران للأسئلة المركّبة الناجحة بمرحلتيها. لا استثناء يخرج من هنا —
        فشل أي مرحلة يعني ببساطة توقّف السلسلة عندها (رفض بدلاً من تخمين)."""
        raw_primary, sub_call_templates = self.resolver.resolve_with_subcalls(
            question, schema, gate_result
        )
        primary_result = self.validator.validate(raw_primary, schema)
        answer1 = self.executor.execute(primary_result.tool_call, question)

        if not sub_call_templates or primary_result.tool_call.tool == "unknown":
            # سؤال عادي (لا نية تركيب)، أو المرحلة الأولى نفسها فشلت — لا داعي
            # لمحاولة مرحلة ثانية على نتيجة غير موثوقة أصلاً.
            return [answer1]

        raw_followup = self.resolver.resolve_followup(
            question, schema, primary_result.tool_call, answer1
        )
        followup_result = self.validator.validate(raw_followup, schema)
        if followup_result.tool_call.tool == "unknown":
            # المرحلة الثانية غير موثوقة — نكتفي بنتيجة المرحلة الأولى الصحيحة
            # بدلاً من تخمين تفسير إضافي (نفس فلسفة "الرفض بدلاً من التخمين").
            return [answer1]

        answer2 = self.executor.execute(followup_result.tool_call, question)
        return [answer1, answer2]
