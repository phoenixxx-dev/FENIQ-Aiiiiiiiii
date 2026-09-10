"""ToolCall Validator — Phoenix AI Layer v1، القسم 6 من المواصفة المعمارية.

طبقة حتمية بالكامل (بلا LLM بداخلها)، بوابة إجبارية بين أي ToolCall صادر عن
LLM Intent Resolver (لم يُبنَ بعد بهذه المرحلة) وToolExecutor. خمس بوابات فحص،
بالترتيب، كل واحدة كافية لوحدها للرفض. عند فشل أي بوابة: الناتج الفوري
ToolCall(tool="unknown") + سبب الرفض مسجَّل بـValidationResult.reason — بلا
إعادة محاولة تلقائية، بلا تصحيح صامت للـarguments (قسم 6، آخر فقرة).

مرجع معماري: Phoenix_AI_Layer_v1_Final_Architecture_Specification.md، القسم 6.

قيود هذه المرحلة (لا تعديل خارج هذا الملف):
- لا LLM هنا إطلاقاً — هذه الطبقة تتحقق فقط من ToolCall جاهز، لا تستدعي أي نموذج
  ولا تنتجه.
- لا تعديل على RuleRouter أو Intent Quality Gate. AnalyticsEngine وToolExecutor
  أُضيف لهما لاحقاً أداة الفلترة `filtered_aggregate`/`aggregate_filtered`
  (إضافة فقط، بلا تعديل على أي شيء موجود سابقاً بهما) — استيراد ToolExecutor
  هنا للقراءة فقط (استخراج أسماء الأدوات ديناميكياً).
- البوابة الرابعة تمنع فقط الفلترة العشوائية غير المدعومة (مفاتيح filter/
  filters/where/value_filter على أي أداة أخرى). فلترة بقيمة بُعد محددة عبر
  أداة `filtered_aggregate` الصريحة مدعومة اليوم فعلياً (قسم 10 بالمواصفة) —
  تُفحص بعقدها الخاص بـ_TOOL_CONTRACTS مثل أي أداة أخرى.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .ask import ToolExecutor
from .models import SemanticSchema, ToolCall

# ---------------------------------------------------------------- نتيجة الفحص


@dataclass(frozen=True)
class ValidationResult:
    """نتيجة الفحص الكامل لـToolCall واحد."""
    valid: bool
    tool_call: ToolCall  # الأصلي إن نجح الفحص، أو ToolCall(tool="unknown") إن رُفض
    rejected_gate: str | None = None  # اسم البوابة التي رفضت، None إن نجح الفحص
    reason: str = ""


# --------------------------------------------- عقد كل أداة (مأخوذ من الكود الفعلي)
# القيم هنا مطابقة حرفياً لِما تقرأه دوال ToolExecutor._t_* فعلياً من
# call.arguments بملف ask.py — لا اختراع، لا افتراض بنية غير موجودة.
#
# "concept roles": أي مفتاح يحمل اسم concept يجب أن يشير لعمود من الدور
# الصحيح بالـSchema. هذا هو تحديداً منع "اختراع metrics" (قسم 6، بوابة 5):
# "measure" يجب أن يكون عمود قياس فعلي (role=measure) لا أي عمود عشوائي،
# و"dimension" يجب أن يكون عمود بُعد فعلي. "concept" (أداة count) تقبل بُعداً
# أو معرّفاً لأن _t_count تستخدمها لعدّ صفوف مميّزة بعمود بُعد أو بعمود
# معرّف مثل رقم الفاتورة.

_CONCEPT_ROLES: dict[str, set[str]] = {
    "measure": {"measure"},
    "dimension": {"dimension"},
    "concept": {"dimension", "identifier"},
}

_VALID_AGGS = {"sum", "avg", "max", "min"}
_VALID_GRANULARITIES = {"day", "week", "month", "year"}

_TOOL_CONTRACTS: dict[str, dict[str, set[str]]] = {
    "aggregate": {"required": {"measure"}, "optional": {"agg"}},
    "count": {"required": set(), "optional": {"concept"}},
    "top_n": {"required": {"measure", "dimension", "n"}, "optional": {"ascending"}},
    "timeseries": {"required": {"measure"}, "optional": {"granularity"}},
    "compare_periods": {"required": {"measure", "months"},
                       "optional": {"dimension", "value"}},
    "low_stock": {"required": set(), "optional": {"dimension", "count_only"}},
    "stagnant_items": {"required": {"dimension", "measure"}, "optional": set()},
    "filtered_aggregate": {"required": {"measure", "dimension", "value"}, "optional": {"agg"}},
}

# مفاتيح تدل صراحة على طلب فلترة بقيمة بُعد محددة. لا توجد اليوم أي أداة
# بـToolExecutor تقرأ أياً من هذه المفاتيح من call.arguments — فهي غير قابلة
# للتنفيذ فعلياً بغضّ النظر عن أي دعم جزئي غير مُستخدَم داخل AnalyticsEngine.
_FILTER_KEYS = {"filter", "filters", "where", "value_filter"}


def _discover_tool_names(executor_cls: type = ToolExecutor) -> set[str]:
    """استخراج تلقائي لأسماء الأدوات من ToolExecutor._t_* — لا قائمة يدوية
    ثابتة، حتى لا تنحرف عن الكود الفعلي إن أُضيفت أو حُذفت أداة لاحقاً."""
    return {name[len("_t_"):] for name in dir(executor_cls) if name.startswith("_t_")}


def _reject(gate: str, reason: str) -> ValidationResult:
    return ValidationResult(
        valid=False, tool_call=ToolCall(tool="unknown", arguments={}),
        rejected_gate=gate, reason=reason,
    )


def _check_arg_values(tool: str, args: dict[str, Any]) -> str | None:
    """فحوصات نوع/قيمة لا تتعلق بالـconcepts (تلك مغطاة في validate())."""
    if tool in ("aggregate", "filtered_aggregate") and "agg" in args and args["agg"] not in _VALID_AGGS:
        return f"قيمة agg غير صالحة: {args['agg']!r} (يجب أن تكون إحدى {sorted(_VALID_AGGS)})."

    if tool == "filtered_aggregate":
        value = args.get("value")
        if not isinstance(value, str) or not value.strip():
            return f"قيمة value غير صالحة: {value!r} (يجب أن تكون نصاً غير فارغ)."

    if tool == "top_n":
        n = args.get("n")
        if not isinstance(n, int) or isinstance(n, bool) or not (1 <= n <= 100):
            return f"قيمة n غير صالحة: {n!r} (يجب أن تكون عدداً صحيحاً بين 1 و100)."
        if "ascending" in args and not isinstance(args["ascending"], bool):
            return "قيمة ascending يجب أن تكون true أو false فقط."

    if tool == "timeseries" and "granularity" in args and args["granularity"] not in _VALID_GRANULARITIES:
        return (f"قيمة granularity غير صالحة: {args['granularity']!r} "
                f"(يجب أن تكون إحدى {sorted(_VALID_GRANULARITIES)}).")

    if tool == "compare_periods":
        months = args.get("months")
        if (not isinstance(months, (list, tuple)) or len(months) != 2
                or not all(isinstance(m, int) and not isinstance(m, bool) and 1 <= m <= 12
                           for m in months)):
            return f"قيمة months غير صالحة: {months!r} (يجب أن تكون قائمة من رقمي شهر بين 1 و12)."
        has_dim = args.get("dimension") is not None
        has_val = args.get("value") is not None
        if has_dim != has_val:
            return "لتطبيق فلتر على المقارنة يجب تحديد dimension وvalue معاً، أو حذفهما معاً."
        if has_val and (not isinstance(args["value"], str) or not args["value"].strip()):
            return f"قيمة value غير صالحة: {args['value']!r} (يجب أن تكون نصاً غير فارغ)."

    if tool == "low_stock" and "count_only" in args and not isinstance(args["count_only"], bool):
        return "قيمة count_only يجب أن تكون true أو false فقط."

    return None


def validate(call: ToolCall, schema: SemanticSchema) -> ValidationResult:
    """الفحص الكامل بالترتيب. أول بوابة ترفض توقف الفحص فوراً (fail-fast).

    `tool="unknown"` هو إشارة الرفض القياسية نفسها المُنتَجة أصلاً من
    RuleRouter ومن ToolExecutor.execute — تمر دائماً بلا فحص إضافي، لأنها
    أصلاً حالة "لا تخمين" الآمنة.
    """
    if call.tool == "unknown":
        return ValidationResult(valid=True, tool_call=call)

    # بوابة 1 — منع أدوات غير موجودة فعلياً
    known_tools = _discover_tool_names()
    if call.tool not in known_tools:
        return _reject("unknown_tool", f"الأداة «{call.tool}» ليست من الأدوات المنفَّذة فعلياً في المحرك.")

    contract = _TOOL_CONTRACTS[call.tool]
    allowed_keys = contract["required"] | contract["optional"]
    args = call.arguments or {}

    # بوابة 4 — منع فلاتر غير قابلة للتنفيذ (تُفحص قبل بوابة الوسائط لأنها
    # سبب رفض أوضح وأكثر تحديداً من "مفتاح غير معروف" العام)
    filter_keys = sorted(k for k in args if k in _FILTER_KEYS)
    if filter_keys:
        return _reject(
            "unsupported_filter",
            f"طلب فلترة بقيمة بُعد محددة ({', '.join(filter_keys)}) — هذه القدرة "
            "لا تقابلها اليوم أي أداة منفَّذة في محرك البيانات.",
        )

    # بوابة 3 — منع arguments غير صالحة: مفاتيح مطلوبة مفقودة
    missing = contract["required"] - set(args.keys())
    if missing:
        return _reject(
            "invalid_arguments",
            f"مفاتيح مطلوبة مفقودة لأداة «{call.tool}»: {sorted(missing)}.",
        )

    # بوابة 3 — مفاتيح زائدة غير معروفة لهذه الأداة
    extra = sorted(set(args.keys()) - allowed_keys)
    if extra:
        return _reject(
            "invalid_arguments",
            f"مفاتيح غير معروفة لأداة «{call.tool}»: {extra}.",
        )

    # بوابة 3 — أنواع/قيم كل مفتاح غير-concept
    type_error = _check_arg_values(call.tool, args)
    if type_error:
        return _reject("invalid_arguments", type_error)

    # بوابة 2 — منع concepts غير موجودة بالـSchema
    # بوابة 5 — منع اختراع metrics (الدور المسجَّل بالـSchema يجب أن يطابق
    # الدور المتوقَّع لهذا المفتاح تحديداً، لا أي عمود عشوائي)
    for key, allowed_roles in _CONCEPT_ROLES.items():
        if key not in args:
            continue
        concept_value = args[key]
        if concept_value is None:
            if key in contract["required"]:
                return _reject(
                    "invalid_arguments",
                    f"المفتاح المطلوب «{key}» لا يجوز أن يكون فارغاً (None) لأداة «{call.tool}».",
                )
            continue
        if not isinstance(concept_value, str):
            return _reject(
                "invalid_arguments",
                f"القيمة بمفتاح «{key}» يجب أن تكون نصاً (اسم concept)، وردت: {concept_value!r}.",
            )
        col = schema.by_concept(concept_value)
        if col is None:
            return _reject(
                "unknown_concept",
                f"المفهوم «{concept_value}» غير موجود فعلياً بـSchema هذا الملف.",
            )
        if col.role not in allowed_roles:
            return _reject(
                "invented_metric",
                f"المفهوم «{concept_value}» دوره «{col.role}» بالـSchema، لا يصلح "
                f"كـ«{key}» (المتوقَّع أحد الأدوار: {sorted(allowed_roles)}).",
            )

    return ValidationResult(valid=True, tool_call=call)


class ToolCallValidator:
    """واجهة الطبقة — غلاف رقيق فوق validate()، بنفس نمط IntentQualityGate."""

    def validate(self, call: ToolCall, schema: SemanticSchema) -> ValidationResult:
        return validate(call, schema)
