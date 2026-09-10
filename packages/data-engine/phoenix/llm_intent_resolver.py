"""LLM Intent Resolver — Phoenix AI Layer v1، القسم 5 من المواصفة المعمارية.

يُستدعى فقط من AIQuestionRouter عند قرار escalate من Intent Quality Gate.
مسؤوليته الوحيدة: تحويل (سؤال + سياق مُرسَل) إلى ToolCall بصيغة JSON صارمة.
لا يُسمح له بإرجاع أي رقم أو نتيجة — هذا مفروض ببنية الـJSON schema المطلوبة
منه: لا حقل رقمي ولا نصي حر بالمخرَج المتوقَّع إطلاقاً (قسم 5، آخر فقرة).

استدعاء النموذج نفسه معزول خلف معامل قابل للحقن (`call_fn`) عمداً: حزمة
الاختبارات بالمشروع تعمل بلا أي خدمة خارجية (انظر رأس tests/test_engine.py) —
الاختبارات هنا تحقن دالة وهمية، ولا تتصل بـGemini الحقيقي إطلاقاً.

مزوّد النموذج الافتراضي: Google Gemini عبر حزمة google-genai (قرار المستخدم
الصريح — بديل مجاني لا يتطلب بطاقة دفع). يمكن استبداله بأي مزوّد آخر بحقن
call_fn مختلف بلا تعديل على بقية الطبقة.
"""
from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .ask import ToolExecutor
from .intent_quality_gate import GateResult
from . import llm_provider
from .models import Answer, SemanticSchema, ToolCall
from .tool_call_validator import _CONCEPT_ROLES, _TOOL_CONTRACTS, _VALID_AGGS, _VALID_GRANULARITIES

DEFAULT_MODEL = "gemini-3.6-flash"
# ملاحظة توثيقية (فحص حي فعلي، سبتمبر 2026): gemini-2.5-flash يرجع 404 لمستخدمين
# جدد اليوم ("no longer available to new users") — Gemini API يستبدل الموديلات
# بسرعة. إن ظهر 404 مشابه بالمستقبل، حدّث هذا الثابت فقط لاسم الموديل الحالي
# — لا تعديل آخر مطلوب بأي مكان تاني بالكود.

# ---------------------------------------------------------------------------
# بناء الحزمة المُرسَلة للنموذج (قسم 5: "ما يُرسَل")


def _schema_summary(schema: SemanticSchema) -> list[dict[str, Any]]:
    """اسم كل عمود، concept، role، unit — لا بيانات فعلية إطلاقاً."""
    return [
        {"column_name": c.column_name, "concept": c.concept, "role": c.role, "unit": c.unit}
        for c in schema.columns
    ]


def _tools_summary() -> list[dict[str, Any]]:
    """قائمة الأدوات، مُستخرجة تلقائياً من نفس عقد ToolCallValidator (المُشتق
    أصلاً من ToolExecutor._t_* الفعلية) — لا قائمة يدوية مكتوبة هنا من جديد،
    حتى لا تنحرف نسختان عن بعض."""
    known_tools = {name[len("_t_"):] for name in dir(ToolExecutor) if name.startswith("_t_")}
    out = []
    for tool in sorted(known_tools):
        contract = _TOOL_CONTRACTS.get(tool)
        if contract is None:
            continue  # أداة موجودة بالكود لكن بلا عقد مسجَّل بعد — لا تُرسَل للـLLM
        out.append({
            "tool": tool,
            "required_arguments": sorted(contract["required"]),
            "optional_arguments": sorted(contract["optional"]),
        })
    return out


def _gate_diagnosis(gate_result: GateResult) -> dict[str, Any]:
    return {
        "rule_router_tool_call": {
            "tool": gate_result.tool_call.tool,
            "arguments": gate_result.tool_call.arguments,
        },
        "dropped_signals": list(gate_result.dropped_signals),
        "ignored_entities": list(gate_result.ignored_entities),
        "reason": gate_result.reason,
    }


def build_llm_package(question: str, schema: SemanticSchema, gate_result: GateResult) -> dict[str, Any]:
    """الحزمة الكاملة المُرسَلة — نص السؤال حرفياً + ملخص Schema + الأدوات +
    تشخيص الـGate. لا بيانات خام، لا صفوف فعلية بأي مكان."""
    return {
        "question": question,
        "schema": _schema_summary(schema),
        "available_tools": _tools_summary(),
        "valid_agg_values": sorted(_VALID_AGGS),
        "valid_granularity_values": sorted(_VALID_GRANULARITIES),
        "concept_roles_by_argument_key": {k: sorted(v) for k, v in _CONCEPT_ROLES.items()},
        "gate_diagnosis": _gate_diagnosis(gate_result),
    }


# ---------------------------------------------------------------------------
# عقد مخرَج الـLLM (قسم 5: "ما يجب أن يُعيده بالضبط")
# JSON صارم: tool (نص)، arguments (قاموس)، sub_calls اختياري (قائمة بنفس بنية
# {tool, arguments}). ممنوع أي حقل إضافي يحمل رقماً أو نتيجة أو نص تفسيري —
# الـschema أدناه لا يحتوي أصلاً أي حقل من هذا النوع، فالهلوسة الرقمية بهذه
# المرحلة مستحيلة بنيوياً.

RESPONSE_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tool": {"type": "string"},
        "arguments": {"type": "object"},
        "sub_calls": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "tool": {"type": "string"},
                    "arguments": {"type": "object"},
                },
                "required": ["tool", "arguments"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["tool", "arguments"],
    "additionalProperties": False,
}


_PROMPT_TEMPLATE = """أنت مكوّن حتمي داخل نظام تحليل بيانات. مهمتك الوحيدة: تحويل سؤال \
المستخدم إلى استدعاء أداة واحدة (ToolCall) بصيغة JSON صارمة تطابق الـschema \
المُعطى تماماً. لا تحسب أي رقم، لا تشرح، لا تكتب أي نص خارج الـJSON.

قواعد إلزامية:
- اختر tool من "available_tools" فقط — لا اسم أداة من عندك.
- كل مفهوم concept تستخدمه بـarguments يجب أن يكون موجوداً حرفياً بـ"schema" \
(بحقل concept)، وبنفس الدور المتوقَّع لمفتاحه (انظر concept_roles_by_argument_key).
- إن لم تكن واثقاً تماماً، أو السؤال غامض، أو لا توجد أداة مناسبة: أعد \
{{"tool": "unknown", "arguments": {{}}}} بالضبط — لا تخمين، لا اختيار "أقرب" أداة.

السياق:
{package_json}

أعد الـJSON فقط، بلا أي نص إضافي قبله أو بعده."""


def _build_prompt(package: dict[str, Any]) -> str:
    return _PROMPT_TEMPLATE.format(package_json=json.dumps(package, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# المرحلة الثانية للأسئلة المركّبة (قسم 7) — "LLM ثانٍ محدود النطاق بنفس عقد
# قسم 5". المُدخل الإضافي الوحيد: نتيجة المرحلة الأولى الفعلية (Answer/Evidence
# المُنتَجة أصلاً من ToolExecutor) — نفس ما يصل لـAIExplainer بالضبط، لا بيانات
# خام إضافية.

_FOLLOWUP_PROMPT_TEMPLATE = """أنت مكوّن حتمي داخل نظام تحليل بيانات. هذا سؤال \
مركّب، وقد نُفِّذت مرحلته الأولى فعلياً. مهمتك: بناء استدعاء أداة واحد إضافي \
(ToolCall) يجيب على الجزء المتبقي من السؤال الأصلي (مثل "ليش")، بالاعتماد على \
نتيجة المرحلة الأولى الفعلية أدناه — لا تعد تنفيذ نفس المرحلة الأولى.

قواعد إلزامية (نفس قواعد الاستدعاء الأول):
- اختر tool من "available_tools" فقط.
- كل concept تستخدمه يجب أن يطابق schema حرفياً وبنفس الدور المتوقَّع.
- إن لم تكن واثقاً، أو لا توجد أداة مفيدة إضافية: أعد \
{{"tool": "unknown", "arguments": {{}}}} بالضبط.

السؤال الأصلي: {question}

نتيجة المرحلة الأولى الفعلية:
{phase1_json}

السياق (نفس Schema والأدوات المتاحة):
{package_json}

أعد الـJSON فقط، بلا أي نص إضافي قبله أو بعده."""


def _phase1_summary(phase1_call: ToolCall, phase1_answer: Answer) -> dict[str, Any]:
    """فقط ما يخرج أصلاً من ToolExecutor — القيم المحسوبة فعلاً، لا بيانات خام
    (نفس ما يصل لـAIExplainer بالضبط، انظر ai_explainer.py)."""
    return {
        "tool": phase1_call.tool,
        "arguments": phase1_call.arguments,
        "understood_as": phase1_answer.understood_as,
        "answer_ar": phase1_answer.answer_ar,
        "metrics": [
            {"value": m.value, "formatted_ar": m.formatted_ar, "unit": m.unit,
             "rows_in_scope": m.evidence.rows_in_scope, "rows_total": m.evidence.rows_total}
            for m in phase1_answer.metrics
        ],
    }


def _build_followup_prompt(question: str, schema: SemanticSchema,
                           phase1_call: ToolCall, phase1_answer: Answer) -> str:
    package = {
        "schema": _schema_summary(schema),
        "available_tools": _tools_summary(),
        "valid_agg_values": sorted(_VALID_AGGS),
        "valid_granularity_values": sorted(_VALID_GRANULARITIES),
        "concept_roles_by_argument_key": {k: sorted(v) for k, v in _CONCEPT_ROLES.items()},
    }
    return _FOLLOWUP_PROMPT_TEMPLATE.format(
        question=question,
        phase1_json=json.dumps(_phase1_summary(phase1_call, phase1_answer), ensure_ascii=False, indent=2),
        package_json=json.dumps(package, ensure_ascii=False, indent=2),
    )


# ---------------------------------------------------------------------------
# استدعاء النموذج الفعلي (Gemini) — دالة افتراضية، قابلة للاستبدال بالكامل


def _default_gemini_call(prompt: str, model: str, api_key: str | None) -> str:
    """الاستدعاء الحقيقي للنموذج. لا يُستدعى أبداً من الاختبارات (تحقن
    call_fn وهمياً دائماً) — فقط عند استخدام فعلي بمفتاح حقيقي.

    الشبكة نفسها ليست هنا: هي في `llm_provider` — الملف الوحيد المسموح له
    بذلك (القاعدة الذهبية #4، والحارس يفرضها).
    """
    return llm_provider.default_provider(api_key).complete(
        prompt, model=model, json_schema=RESPONSE_JSON_SCHEMA)


# ---------------------------------------------------------------------------
# مهلة نداء النموذج

# نداء نموذج بلا مهلة يعلّق الطلب إلى الأبد. الخطة (§7.4) تفرض 30 ثانية،
# والقاعدة الذهبية #6 تفرض ألا يتجاوز أي endpoint ثلاث ثوانٍ — فالمهلة هنا
# سقف أمان لا زمن مقبول.
LLM_TIMEOUT_SECONDS = 20.0

_TIMEOUT_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="llm")


class LLMTimeout(Exception):
    """تجاوز نداء النموذج مهلته."""


def call_with_timeout(fn, prompt: str, seconds: float) -> str:
    """ينفّذ النداء بمهلة قصوى.

    ⚠️ صدق تقني: لا يمكن إيقاف خيط بايثون عالق في I/O. ما نضمنه أن **الطلب
    يعود** في الوقت، لا أن الخيط يموت فوراً — سيموت عندما يعود نداء الشبكة
    أو تنتهي مهلته هو. البديل (انتظار بلا حد) يعلّق الطلب إلى الأبد، وهو
    أسوأ يقيناً. لذلك المزوّد يُعطى مهلته الخاصة أيضاً كخط دفاع أول.
    """
    future = _TIMEOUT_POOL.submit(fn, prompt)
    try:
        return future.result(timeout=seconds)
    except FuturesTimeout as e:
        future.cancel()
        raise LLMTimeout(f"تجاوز نداء النموذج {seconds} ثانية") from e


class LLMCallFn(Protocol):
    def __call__(self, prompt: str) -> str: ...


@dataclass
class LLMIntentResolver:
    """يحوّل (سؤال + حزمة سياق) إلى ToolCall واحد عبر LLM حقيقي أو مُحاكى.

    `call_fn(prompt) -> str`: نص JSON خام. الافتراضي يستدعي Gemini فعلياً؛
    الاختبارات تحقن دالة وهمية بديلة (بلا شبكة إطلاقاً).
    """
    api_key: str | None = field(default_factory=lambda: os.environ.get("GEMINI_API_KEY"))
    model: str = DEFAULT_MODEL
    call_fn: Callable[[str], str] | None = None
    # سقف أمان لا زمن مقبول: نداء بلا مهلة يعلّق الطلب إلى الأبد
    timeout_seconds: float = LLM_TIMEOUT_SECONDS

    # عدّاد نداءات النموذج. الحساب يجري في طبقة الـAPI (هي التي تملك
    # التخزين)، لكن **العدّ** يجب أن يكون هنا: هذا هو المكان الوحيد الذي
    # يعرف يقيناً أن نداءً خرج فعلاً.
    llm_calls: int = 0

    def _call(self, prompt: str) -> str:
        self.llm_calls += 1
        fn = self.call_fn or (
            lambda p: _default_gemini_call(p, self.model, self.api_key))
        return call_with_timeout(fn, prompt, self.timeout_seconds)

    def _resolve_raw(self, prompt: str) -> dict[str, Any] | None:
        """الجزء المشترك: استدعاء + تحليل JSON + فحص بنيوي أدنى (tool/arguments
        نصّ/قاموس). None يعني فشلاً بأي مرحلة — لا استثناء يخرج من هنا أبداً."""
        try:
            raw = self._call(prompt)
        except Exception:
            return None
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        tool, arguments = data.get("tool"), data.get("arguments")
        if not isinstance(tool, str) or not isinstance(arguments, dict):
            return None
        return data

    def resolve(self, question: str, schema: SemanticSchema, gate_result: GateResult) -> ToolCall:
        """لا استثناء يخرج من هذه الدالة أبداً — أي فشل (شبكة، JSON فاسد،
        بنية غير متوقَّعة) يُترجَم فوراً لـToolCall(tool="unknown"). هذا هو
        "الرفض بدلاً من التخمين" (قسم 4، قسم 9: invalid LLM ToolCall)."""
        call, _ = self.resolve_with_subcalls(question, schema, gate_result)
        return call

    def resolve_with_subcalls(
        self, question: str, schema: SemanticSchema, gate_result: GateResult,
    ) -> tuple[ToolCall, list[ToolCall]]:
        """مثل resolve()، لكن يعيد أيضاً sub_calls الخام إن وُجدت (قسم 7: الأسئلة
        المركّبة). sub_calls هنا مجرد "نية" أولية من الاستدعاء الأول — الوسائط
        الفعلية للمرحلة الثانية تُبنى لاحقاً بعد تنفيذ المرحلة الأولى فعلياً
        (انظر resolve_followup) لأن الـLLM لا يرى بيانات فعلية ليعرف نتيجة
        المرحلة الأولى مسبقاً."""
        package = build_llm_package(question, schema, gate_result)
        data = self._resolve_raw(_build_prompt(package))
        if data is None:
            return ToolCall(tool="unknown", arguments={}), []

        primary = ToolCall(tool=data["tool"], arguments=data["arguments"])
        sub_calls: list[ToolCall] = []
        for sc in (data.get("sub_calls") or []):
            if isinstance(sc, dict) and isinstance(sc.get("tool"), str) and isinstance(sc.get("arguments"), dict):
                sub_calls.append(ToolCall(tool=sc["tool"], arguments=sc["arguments"]))
        return primary, sub_calls

    def resolve_followup(
        self, question: str, schema: SemanticSchema,
        phase1_call: ToolCall, phase1_answer: Any,
    ) -> ToolCall:
        """المرحلة الثانية الفعلية للأسئلة المركّبة (قسم 7): LLM ثانٍ محدود
        النطاق، بنفس عقد قسم 5، يستقبل السؤال الأصلي + نتيجة المرحلة الأولى
        الفعلية (المحسوبة فعلاً — لا بيانات خام، فقط Answer/Evidence المُنتَجة
        أصلاً من ToolExecutor، تماماً كما يستقبلها AIExplainer). لا sub_calls
        بهذه المرحلة — عمق واحد إضافي فقط (بمرحلتين إجباريتين، لا أكثر)."""
        prompt = _build_followup_prompt(question, schema, phase1_call, phase1_answer)
        data = self._resolve_raw(prompt)
        if data is None:
            return ToolCall(tool="unknown", arguments={})
        return ToolCall(tool=data["tool"], arguments=data["arguments"])
