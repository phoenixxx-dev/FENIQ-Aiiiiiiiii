"""Intent Quality Gate — Phoenix AI Layer v1، المرحلة الأولى.

طبقة فحص مستقلة وحتمية بالكامل، تعمل *بعد* RuleRouter و*قبل* ToolExecutor.
لا تنفّذ أي SQL ولا تتصل بـAnalyticsEngine إطلاقاً — فقط تفحص ناتج RuleRouter
(ToolCall + QuestionSignals) مقابل الـSchema وDatasetProfile (إن وُجد)، وتقرر:
هل هذا الناتج جدير بالثقة كما هو (proceed)، أم يحتاج تصعيداً لاحقاً لطبقة LLM
لم تُبنَ بعد (escalate)؟

مرجع معماري: Phoenix AI Layer v1 — Final Architecture Specification، الأقسام 2 و3.

قيود هذه المرحلة (مطابقة للتكليف):
- لا LLM هنا إطلاقاً.
- لا AIQuestionRouter ولا AIExplainer.
- لا اتصال خارجي ولا مفاتيح API.
- لا قدرات جديدة بـData Engine.
- RuleRouter نفسه لم يُعدَّل — الـGate يستخدم فقط extract_signals()/resolve_intent()
  الموجودتين أصلاً كـmethods عامة بالكلاس الحالي.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .arabic_text import normalize_ar
from .ask import QuestionSignals, RuleRouter
from .models import DatasetProfile, SemanticSchema, ToolCall

# ---------------------------------------------------------------------------
# فحص dropped_signals
#
# كل إشارة بـQuestionSignals تُفحص بدالة "تمثيل" مستقلة: هل الإشارة (إن كانت
# مفعّلة) ممثَّلة فعلياً بالـToolCall النهائي (باسم الأداة و/أو arguments)؟
# النطاق: فقط الإشارات الثمانية الصريحة المطلوبة بالتكليف (dimension, ranking,
# counting, low_stock, trend, comparing, stagnant, averaging)، بالإضافة إلى
# totaling لأنها موجودة فعلياً بـQuestionSignals وتتبع نفس المنطق تماماً
# (aggregate/sum). لا تُفحص هنا: ranking_ascending, dimension_mentioned, n,
# months, measure_explicit — هذه تفاصيل/معدِّلات مرافقة للإشارات الرئيسية لا
# إشارات نية مستقلة بحد ذاتها، وفحصها خارج نطاق هذه المرحلة عمداً.

def _dimension_represented(signals: QuestionSignals, call: ToolCall) -> bool:
    if signals.dimension is None:
        return True
    return call.arguments.get("dimension") == signals.dimension


def _ranking_represented(signals: QuestionSignals, call: ToolCall) -> bool:
    if not signals.ranking:
        return True
    if call.tool == "top_n":
        return True
    if call.tool == "aggregate" and call.arguments.get("agg") in ("max", "min"):
        return True
    return False


def _counting_represented(signals: QuestionSignals, call: ToolCall) -> bool:
    if not signals.counting:
        return True
    if call.tool == "count":
        return True
    if call.tool == "low_stock" and call.arguments.get("count_only") is True:
        return True
    return False


def _low_stock_represented(signals: QuestionSignals, call: ToolCall) -> bool:
    if not signals.low_stock:
        return True
    return call.tool == "low_stock"


def _trend_represented(signals: QuestionSignals, call: ToolCall) -> bool:
    if not signals.trend:
        return True
    return call.tool == "timeseries"


def _comparing_represented(signals: QuestionSignals, call: ToolCall) -> bool:
    if not signals.comparing:
        return True
    return call.tool == "compare_periods"


def _stagnant_represented(signals: QuestionSignals, call: ToolCall) -> bool:
    if not signals.stagnant:
        return True
    return call.tool == "stagnant_items"


def _averaging_represented(signals: QuestionSignals, call: ToolCall) -> bool:
    if not signals.averaging:
        return True
    return call.tool == "aggregate" and call.arguments.get("agg") == "avg"


def _totaling_represented(signals: QuestionSignals, call: ToolCall) -> bool:
    if not signals.totaling:
        return True
    return call.tool == "aggregate" and call.arguments.get("agg") == "sum"


_SIGNAL_CHECKS: dict[str, Any] = {
    "dimension": _dimension_represented,
    "ranking": _ranking_represented,
    "counting": _counting_represented,
    "low_stock": _low_stock_represented,
    "trend": _trend_represented,
    "comparing": _comparing_represented,
    "stagnant": _stagnant_represented,
    "averaging": _averaging_represented,
    "totaling": _totaling_represented,
}


def _dropped_signals(signals: QuestionSignals, call: ToolCall) -> list[str]:
    return [name for name, is_represented in _SIGNAL_CHECKS.items()
            if not is_represented(signals, call)]


# ---------------------------------------------------------------------------
# فحص ignored_entities
#
# مطابقة entity مع top_values ليست دليلاً قاطعاً على نية فلترة — هي
# suspicion signal فقط (تعليمات صريحة بالتكليف). كل كيان مطابق غير مستخدم
# فعلياً بأي مكان بالـToolCall يرفع suspicion_score بمقدار 1.

def _collect_string_values(obj: Any, out: set[str]) -> None:
    """يجمع كل القيم النصية المطبَّعة الموجودة بأي مكان داخل arguments، بلا
    استثناء البنى المتداخلة (قوائم/قواميس) — لضمان أن أي كيان يُستخدم فعلياً
    بالـToolCall (حتى لو مستقبلاً بمكان متداخل) لا يُعتبر مُتجاهَلاً بالخطأ.
    """
    if isinstance(obj, str):
        n = normalize_ar(obj)
        if n:
            out.add(n)
    elif isinstance(obj, dict):
        for v in obj.values():
            _collect_string_values(v, out)
    elif isinstance(obj, (list, tuple, set)):
        for v in obj:
            _collect_string_values(v, out)


def _ignored_entities(
    question_norm: str,
    call: ToolCall,
    schema: SemanticSchema,
    profile: DatasetProfile | None,
) -> list[str]:
    if profile is None:
        return []

    used_values: set[str] = set()
    _collect_string_values(call.arguments, used_values)

    ignored: list[str] = []
    seen: set[str] = set()
    for schema_col in schema.columns:
        if schema_col.role != "dimension":
            continue
        col_profile = profile.column(schema_col.column_name)
        if col_profile is None:
            continue
        for value, _count in col_profile.top_values:
            if not value:
                continue
            value_str = str(value)
            n_value = normalize_ar(value_str)
            if not n_value or n_value not in question_norm:
                continue
            if n_value in used_values:
                continue  # مُستخدَم فعلياً بالـToolCall — ليس مُتجاهَلاً
            if n_value in seen:
                continue  # تفادي تكرار نفس الكيان أكثر من مرة بالتقرير
            seen.add(n_value)
            ignored.append(value_str)
    return ignored


# ---------------------------------------------------------------------------
# القرار النهائي

@dataclass(frozen=True)
class GateResult:
    """قرار الـGate + تشخيص كامل، بلا أي رقم confidence (خارج نطاق هذه المرحلة)."""
    proceed: bool
    escalate: bool
    tool_call: ToolCall
    signals: QuestionSignals
    dropped_signals: tuple[str, ...] = ()
    ignored_entities: tuple[str, ...] = ()
    suspicion_score: int = 0
    reason: str = ""


SUSPICION_THRESHOLD = 1


def evaluate(
    question: str,
    schema: SemanticSchema,
    profile: DatasetProfile | None = None,
    router: RuleRouter | None = None,
) -> GateResult:
    """ينفّذ الفحص الكامل: dropped_signals + ignored_entities + قرار escalate/proceed.

    لا ينفّذ أي SQL ولا يتصل بـAnalyticsEngine — فقط يستدعي RuleRouter (كما هو،
    بلا تعديل) عبر واجهتيه العامتين الموجودتين أصلاً.
    """
    router = router or RuleRouter()
    signals = router.extract_signals(question, schema)
    call = router.resolve_intent(signals, schema)

    dropped = _dropped_signals(signals, call)

    q_norm = normalize_ar(question)
    ignored = _ignored_entities(q_norm, call, schema, profile)
    suspicion_score = len(ignored)

    escalate = (call.tool == "unknown") or bool(dropped) or (suspicion_score >= SUSPICION_THRESHOLD)
    proceed = not escalate

    reasons = []
    if call.tool == "unknown":
        reasons.append("tool=unknown")
    if dropped:
        reasons.append(f"dropped_signals={dropped}")
    if suspicion_score >= SUSPICION_THRESHOLD:
        reasons.append(f"ignored_entities={ignored}")
    reason = "; ".join(reasons) if reasons else "no issues detected"

    return GateResult(
        proceed=proceed,
        escalate=escalate,
        tool_call=call,
        signals=signals,
        dropped_signals=tuple(dropped),
        ignored_entities=tuple(ignored),
        suspicion_score=suspicion_score,
        reason=reason,
    )


class IntentQualityGate:
    """واجهة الطبقة — غلاف رقيق فوق evaluate() لتمرير RuleRouter مرة واحدة."""

    def __init__(self, router: RuleRouter | None = None):
        self.router = router or RuleRouter()

    def evaluate(
        self,
        question: str,
        schema: SemanticSchema,
        profile: DatasetProfile | None = None,
    ) -> GateResult:
        return evaluate(question, schema, profile=profile, router=self.router)
