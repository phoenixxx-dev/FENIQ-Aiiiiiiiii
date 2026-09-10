"""D — «اسأل فينيق». سؤال عربي ← استدعاء أداة ← رقم حقيقي + دليل.

في هذه الشريحة الموجّه **حتمي** (أنماط عربية)، لا LLM.
السبب: إثبات أن الأرقام صحيحة أولاً. الـLLM يُركَّب لاحقاً في نفس المكان بالضبط
عبر واجهة `QuestionRouter` — فالأدوات والأدلة والتنسيق كلها جاهزة ولا تتغيّر.

قاعدة ذهبية #1 محفوظة في الحالتين: الـLLM يختار الأداة، والمحرك يحسب الرقم.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol

from .arabic_text import (count_ar, fmt_currency, fmt_number, fmt_percent,
                          normalize_ar, to_western_digits)
from .models import Answer, MetricResult, SemanticSchema, ToolCall

# ------------------------------------------------------------ مفردات عربية
# تشمل الفصحى والعامية الشامية والخليجية والمصرية
_TOTAL = ["اجمالي", "مجموع", "كلي", "كم مجموع", "كم اجمالي", "total", "كم صار", "كم طلع"]
_AVG = ["متوسط", "معدل", "وسطي", "average", "بالمتوسط", "بالمعدل"]
_COUNT = ["عدد", "كم عدد", "كم فاتوره", "كام", "count", "كم عمليه", "كم صفقه"]
# «أغلى» و«أرخص» ترتيبٌ **وقياس** معاً: من يسأل «شو أغلى صنف؟» يقصد الترتيب
# حسب السعر تحديداً، لا حسب المقياس الافتراضي. لذلك تظهران في القائمتين.
_MAX = ["اعلى", "اكبر", "افضل", "احسن", "اكتر", "اكثر", "top", "الاول", "الاوائل",
        "اغلى", "اثمن"]
_MIN = ["اقل", "اضعف", "اسوا", "ادنى", "اصغر", "الاخير", "ارخص"]
_TREND = ["حسب الشهر", "شهريا", "بالشهر", "تطور", "اتجاه", "على مدار", "عبر الزمن",
          "كل شهر", "بالاشهر", "trend", "حسب الوقت", "حسب التاريخ"]
# كلمات التفصيل الزمني: بدونها كان «المبيعات اليومية» يُفهم إجمالياً لا كسلسلة
_GRAN_WORDS = {
    "day": ["يومي", "يوميا", "يوميه", "كل يوم", "بالايام", "daily"],
    "week": ["اسبوعي", "اسبوعيا", "اسبوعيه", "كل اسبوع", "weekly"],
    "month": ["شهري", "شهريا", "شهريه", "كل شهر", "بالاشهر", "حسب الشهر", "monthly"],
    "year": ["سنوي", "سنويا", "سنويه", "بالسنه", "كل سنه", "حسب السنه", "yearly"],
}

_COMPARE = ["قارن", "مقارنه", "مقارنة", "الفرق بين", "مقابل", "compare", "قارنلي"]
_STAGNANT = ["راكد", "راكده", "ميت", "بلا حركه", "ما بتتحرك", "بطيء", "بطيئه", "مش بتتباع"]
_WHY = ["ليش", "لماذا", "ليه", "شو سبب", "ما سبب", "سبب"]
_LOW_STOCK = ["مخزون منخفض", "مخزون قليل", "نفد", "نافد", "قرب يخلص", "قارب على النفاد",
              "تحت الحد", "اعاده طلب", "اعادة طلب", "بحاجه لطلب", "ناقص بالمستودع",
              "رصيد منخفض", "رصيد قليل", "low stock",
              # عامية شائعة: «وين المخزون ناقص» — كانت ترجع unknown
              "مخزون ناقص", "المخزون ناقص", "ناقص بالمخزون", "شارف على الانتهاء"]

# البُعد المقصود في السؤال
_DIM_WORDS = {
    "product_name": ["منتج", "منتجات", "صنف", "اصناف", "ماده", "مواد", "سلعه", "بضاعه", "product"],
    "customer": ["عميل", "عملاء", "زبون", "زباين", "زبائن", "حساب", "customer", "جهه",
                 "صيدليه", "صيدليات", "صيدليات شراء", "مشتري", "مشترين", "نقطه بيع"],
    "salesperson": ["مندوب", "مناديب", "بائع", "موظف", "مسوق", "كاشير", "rep"],
    "region": ["منطقه", "مناطق", "مدينه", "مدن", "فرع", "فروع", "محافظه", "region"],
    "category": ["تصنيف", "فئه", "فئات", "مجموعه", "قسم", "category"],
    "payment_method": ["طريقه الدفع", "الدفع", "نوع الدفع"],
}

# مقاييس لا تُجمع: معدّلات ونِسَب، لا مقادير. جمعُها ينتج رقماً بلا مرجع.
_NON_ADDITIVE = {"unit_price", "exchange_rate", "margin_percentage"}

# المقياس المقصود
_MEASURE_WORDS = {
    "total_amount": ["مبيعات", "ايراد", "ايرادات", "مبلغ", "قيمه", "دخل", "revenue", "sales", "فلوس"],
    "quantity": ["كميه", "كميات", "عدد القطع", "قطع", "وحدات", "qty"],
    "stock_qty": ["مخزون", "رصيد", "متوفر", "موجود", "stock"],
    "unit_price": ["اغلى", "ارخص", "اثمن", "سعر الوحده", "الاسعار", "price"],
}

_AR_MONTH_NAMES = {
    1: ["يناير", "كانون الثاني"], 2: ["فبراير", "شباط"], 3: ["مارس", "اذار"],
    4: ["ابريل", "نيسان"], 5: ["مايو", "ايار"], 6: ["يونيو", "حزيران"],
    7: ["يوليو", "تموز"], 8: ["اغسطس", "اب"], 9: ["سبتمبر", "ايلول"],
    10: ["اكتوبر", "تشرين الاول"], 11: ["نوفمبر", "تشرين الثاني"], 12: ["ديسمبر", "كانون الاول"],
}


@dataclass(frozen=True)
class QuestionSignals:
    """All independently detected signals in a question.

    Extraction is deliberately side-effect free: a question may carry several
    signals at once (for example low_stock + counting).
    """
    ranking: bool = False
    ranking_ascending: bool = False
    n: int | None = None
    counting: bool = False
    low_stock: bool = False
    trend: bool = False
    comparing: bool = False
    stagnant: bool = False
    averaging: bool = False
    totaling: bool = False
    dimension: str | None = None
    dimension_mentioned: bool = False
    measure_explicit: str | None = None
    months: tuple[int, ...] = ()
    granularity: str | None = None
    group_by: bool = False
    relative_period: bool = False


def _normalize_word_list(words: list[str]) -> list[str]:
    return [normalize_ar(w) for w in words if normalize_ar(w)]


def _normalize_word_map(words: dict[str, list[str]]) -> dict[str, list[str]]:
    return {k: _normalize_word_list(v) for k, v in words.items()}


# The human-readable lexicons remain the source of truth; matching always uses
# their normalized projection. This prevents normalize_ar() and the lexicons
# from drifting apart.
_TOTAL_N = _normalize_word_list(_TOTAL)
_AVG_N = _normalize_word_list(_AVG)
_COUNT_N = _normalize_word_list(_COUNT)
_MAX_N = _normalize_word_list(_MAX)
_MIN_N = _normalize_word_list(_MIN)
_TREND_N = _normalize_word_list(_TREND)
_COMPARE_N = _normalize_word_list(_COMPARE)
_STAGNANT_N = _normalize_word_list(_STAGNANT)
_WHY_N = _normalize_word_list(_WHY)
_LOW_STOCK_N = _normalize_word_list(
    _LOW_STOCK + [
        "تحت حد الطلب",
        "تحت حد اعادة الطلب",
        "تحت حد إعادة الطلب",
    ]
)
_DIM_WORDS_N = _normalize_word_map(_DIM_WORDS)
_MEASURE_WORDS_N = _normalize_word_map(_MEASURE_WORDS)


def _has(q: str, words: list[str]) -> bool:
    return any(w and w in q for w in words)


def _find_dimension(q: str, schema: SemanticSchema) -> str | None:
    for concept, words in _DIM_WORDS_N.items():
        if schema.by_concept(concept) and _has(q, words):
            return concept
    return None


def _find_measure_explicit(q: str, schema: SemanticSchema) -> str | None:
    """المقياس المذكور صراحةً في السؤال — أو None إن لم يُذكر."""
    for concept, words in _MEASURE_WORDS_N.items():
        if schema.by_concept(concept) and _has(q, words):
            return concept
    return None


def _default_measure(schema: SemanticSchema) -> str | None:
    for c in ("total_amount", "quantity", "stock_qty"):
        if schema.by_concept(c):
            return c
    return None


def _find_measure(q: str, schema: SemanticSchema) -> str | None:
    return _find_measure_explicit(q, schema) or _default_measure(schema)


def _find_n(q: str, default: int = 10) -> int:
    m = re.search(r"\b(\d{1,3})\b", to_western_digits(q))
    if m:
        n = int(m.group(1))
        if 1 <= n <= 100:
            return n
    return default


# «الشهر الماضي»، «هالشهر»: نسبية لا تُسمّي شهراً — تعني آخر شهرين بالبيانات
_RELATIVE_PERIOD = ["هالشهر", "هذا الشهر", "الشهر الحالي", "الشهر الماضي", "الشهر السابق",
                    "الشهر يلي قبلو", "الشهر اللي فات", "الشهر الفائت", "الشهرين"]


def _find_months(q: str) -> list[int]:
    """أسماء الشهور، ثم أرقامها الصريحة كاحتياط.

    «قارن مبيعات 5 و 6» شائع جداً عملياً، وكان يُرفض لأننا نبحث عن الأسماء فقط.
    نقبل الأرقام 1..12 فقط عند وجود كلمة مقارنة، حتى لا نفسّر «أعلى 5 منتجات»
    على أنها شهر مايو.
    """
    found = []
    for num, names in _AR_MONTH_NAMES.items():
        normalized_names = _normalize_word_list(names)
        if any(n in q for n in normalized_names):
            found.append(num)
    if len(found) >= 2:
        return found

    if _has(q, _COMPARE_N):
        import re
        numeric = [int(m) for m in re.findall(r"\b(\d{1,2})\b", q) if 1 <= int(m) <= 12]
        for n in numeric:
            if n not in found:
                found.append(n)
    return found


_GROUP_BY_WORDS = ["حسب", "لكل", "بحسب", "موزعه على", "توزيع", "per", "by"]


def _find_granularity(q: str) -> str | None:
    """أدق تفصيل مذكور يفوز: «يومي» أدق من «شهري» لو ذُكرا معاً."""
    for gran in ("day", "week", "month", "year"):
        if _has(q, _normalize_word_list(_GRAN_WORDS[gran])):
            return gran
    return None


def _has_low_stock_signal(q: str) -> bool:
    """Match both established phrases and safe compound aliases.

    We intentionally avoid unrestricted token-intersection matching: it would
    create false positives for unrelated uses of words such as 'حد' or 'طلب'.
    """
    return _has(q, _LOW_STOCK_N)


def _extract_signals(question: str, schema: SemanticSchema) -> QuestionSignals:
    q = normalize_ar(question)
    months = tuple(_find_months(q))
    dimension_mentioned = any(_has(q, words) for words in _DIM_WORDS_N.values())
    return QuestionSignals(
        ranking=_has(q, _MAX_N) or _has(q, _MIN_N),
        ranking_ascending=_has(q, _MIN_N),
        n=_find_n(q, 5) if (_has(q, _MAX_N) or _has(q, _MIN_N)) else None,
        counting=_has(q, _COUNT_N),
        low_stock=_has_low_stock_signal(q),
        trend=_has(q, _TREND_N) or _find_granularity(q) is not None,
        granularity=_find_granularity(q),
        relative_period=_has(q, _normalize_word_list(_RELATIVE_PERIOD)),
        group_by=(_has(q, _normalize_word_list(_GROUP_BY_WORDS))
                  and _find_dimension(q, schema) is not None),
        comparing=_has(q, _COMPARE_N) or len(months) >= 2,
        stagnant=_has(q, _STAGNANT_N),
        averaging=_has(q, _AVG_N),
        totaling=_has(q, _TOTAL_N),
        dimension=_find_dimension(q, schema),
        dimension_mentioned=dimension_mentioned,
        measure_explicit=_find_measure_explicit(q, schema),
        months=months,
    )


def _resolve_intent(signals: QuestionSignals, schema: SemanticSchema) -> ToolCall:
    """Resolve all detected signals by semantic specificity, not first match."""
    # Comparison is the most specific multi-signal temporal intent.
    if signals.comparing:
        if len(signals.months) >= 2:
            return ToolCall(
                tool="compare_periods",
                arguments={
                    "measure": signals.measure_explicit or _default_measure(schema),
                    "months": list(signals.months[:2]),
                },
            )
        # مقارنة نسبية («هالشهر مقارنة بالشهر يلي قبلو»): لا تسمّي شهوراً، لكن
        # معناها محدّد تماماً — آخر شهرين موجودين بالبيانات. الأداة تحسبهما من
        # مدى التاريخ الفعلي، فلا تخمين.
        if signals.relative_period:
            return ToolCall(
                tool="compare_periods",
                arguments={
                    "measure": signals.measure_explicit or _default_measure(schema),
                    "months": [],
                    "last_two": True,
                },
            )
        # An explicit comparison word without enough periods is not a generic
        # aggregate request; refuse rather than silently answering another question.
        return ToolCall(tool="unknown", arguments={})

    # Low-stock is a semantic filter. Counting modifies its presentation,
    # rather than replacing the intent with generic count().
    if signals.low_stock:
        return ToolCall(
            tool="low_stock",
            arguments={
                "dimension": signals.dimension or "product_name",
                "count_only": signals.counting,
            },
        )

    if signals.stagnant:
        return ToolCall(
            tool="stagnant_items",
            arguments={
                "dimension": signals.dimension or "product_name",
                "measure": signals.measure_explicit or _default_measure(schema),
            },
        )

    if signals.trend:
        return ToolCall(
            tool="timeseries",
            arguments={
                "measure": signals.measure_explicit or _default_measure(schema),
                "granularity": signals.granularity or "month",
            },
        )

    # «المبيعات حسب المنطقة» تجميع حسب بُعد — كانت تُفهم إجمالياً عاماً فتضيع
    # المقارنة بين الفئات، وهي بالضبط ما يريده السؤال.
    if signals.group_by and signals.dimension and not signals.ranking:
        return ToolCall(
            tool="top_n",
            arguments={
                "measure": signals.measure_explicit or _default_measure(schema),
                "dimension": signals.dimension,
                "n": signals.n or 10,
                "ascending": False,
            },
        )

    # Ranking with a real dimension is more specific than generic counting.
    if signals.ranking and signals.dimension:
        return ToolCall(
            tool="top_n",
            arguments={
                "measure": signals.measure_explicit or _default_measure(schema),
                "dimension": signals.dimension,
                "n": signals.n or 5,
                "ascending": signals.ranking_ascending,
            },
        )

    # If the user explicitly named a dimension that this dataset does not have,
    # never silently reinterpret the request as a global max/min.
    if signals.ranking and signals.dimension_mentioned and not signals.dimension:
        return ToolCall(tool="unknown", arguments={})

    # Ranking without a dimension is a legitimate aggregate max/min request.
    if signals.ranking:
        measure = signals.measure_explicit or _default_measure(schema)
        if measure:
            return ToolCall(
                tool="aggregate",
                arguments={"measure": measure, "agg": "min" if signals.ranking_ascending else "max"},
            )
        return ToolCall(tool="unknown", arguments={})

    if signals.counting:
        if signals.dimension_mentioned and not signals.dimension:
            return ToolCall(tool="unknown", arguments={})
        target = signals.dimension
        if not target:
            target = "invoice_no" if schema.by_concept("invoice_no") else None
        return ToolCall(tool="count", arguments={"concept": target})

    if signals.averaging:
        return ToolCall(
            tool="aggregate",
            arguments={"measure": signals.measure_explicit or _default_measure(schema), "agg": "avg"},
        )

    # Generic aggregation is safe only when no specialized intent signal exists.
    if signals.totaling or signals.measure_explicit:
        return ToolCall(
            tool="aggregate",
            arguments={"measure": signals.measure_explicit or _default_measure(schema), "agg": "sum"},
        )

    return ToolCall(tool="unknown", arguments={})


class QuestionRouter(Protocol):
    """الواجهة التي سيحلّ محلها موجّه LLM لاحقاً دون تغيير أي شيء آخر."""
    def route(self, question: str, schema: SemanticSchema) -> ToolCall: ...


class RuleRouter:
    """Deterministic router: extract signals first, then resolve intent."""

    def extract_signals(self, question: str, schema: SemanticSchema) -> QuestionSignals:
        return _extract_signals(question, schema)

    def resolve_intent(self, signals: QuestionSignals, schema: SemanticSchema) -> ToolCall:
        return _resolve_intent(signals, schema)

    def route(self, question: str, schema: SemanticSchema) -> ToolCall:
        signals = self.extract_signals(question, schema)
        return self.resolve_intent(signals, schema)


# ------------------------------------------------------------ منفّذ الأدوات
class ToolExecutor:
    """ينفّذ الأداة على محرك التحليل ويصوغ الإجابة العربية.

    كل رقم في النص يأتي من `MetricResult` — لا صياغة حرة لأي قيمة.
    """

    def __init__(self, engine, schema: SemanticSchema):
        self.engine = engine
        self.schema = schema

    def _cur(self, concept: str) -> bool:
        c = self.schema.by_concept(concept)
        return bool(c and c.unit == "currency")

    def _fmt(self, concept: str, v: float) -> str:
        if not self._cur(concept):
            return fmt_number(v)
        return fmt_currency(v, self.schema.currency.symbol_ar)

    # نفس المفهوم، جملتان مختلفتان. قول «إجمالي المبيعات» على كتالوج مستودع
    # ليس ركاكة بل خبرٌ كاذب: الملف لا يحوي مبيعة واحدة.
    _LABELS = {"total_amount": "المبيعات", "quantity": "الكميات",
               "stock_qty": "المخزون", "unit_price": "السعر"}
    _LABELS_BY_DOMAIN = {
        "inventory": {"total_amount": "قيمة المخزون", "stock_qty": "الرصيد"},
        "warehouse": {"total_amount": "قيمة المخزون", "stock_qty": "الرصيد"},
        "pharmacy": {"total_amount": "قيمة المخزون", "stock_qty": "الرصيد"},
    }

    def _label(self, concept: str) -> str:
        by_domain = self._LABELS_BY_DOMAIN.get(self.schema.domain, {})
        return by_domain.get(concept) or self._LABELS.get(concept, concept)

    def _dim_label(self, concept: str) -> str:
        return {"product_name": "المنتجات", "customer": "العملاء",
                "salesperson": "المندوبين", "region": "المناطق",
                "category": "التصنيفات"}.get(concept, concept)

    def execute(self, call: ToolCall, question: str) -> Answer:
        fn = getattr(self, f"_t_{call.tool}", None)
        if fn is None or call.tool == "unknown":
            return Answer(
                question=question, understood_as="لم يُفهم السؤال",
                tool_calls=[call], confidence=0.0,
                answer_ar=("لم أفهم السؤال بدقة. جرّب مثلاً: "
                           + "، أو ".join(f"«{x}»" for x in suggest_questions(self.schema, 3))
                           + "."),
            )
        try:
            return fn(call, question)
        except ValueError as e:
            return Answer(question=question, understood_as=call.tool, tool_calls=[call],
                          confidence=0.0, answer_ar=f"تعذّر الحساب: {e}")

    # ---------------------------------------------------------- الأدوات
    def _t_aggregate(self, call: ToolCall, question: str) -> Answer:
        measure, agg = call.arguments["measure"], call.arguments.get("agg", "sum")
        if not measure:
            raise ValueError("لا يوجد عمود عددي مناسب في هذا الملف.")
        r = self.engine.aggregate(measure, agg)
        word = {"sum": "إجمالي", "avg": "متوسط", "max": "أعلى قيمة في", "min": "أقل قيمة في"}[agg]
        return Answer(
            question=question, understood_as=f"{word} {self._label(measure)}",
            tool_calls=[call], metrics=[r],
            answer_ar=(f"{word} {self._label(measure)} يساوي **{r.formatted_ar}** "
                       f"محسوباً من {count_ar(r.evidence.rows_in_scope, 'صف', 'صفّين', 'صفوف')}."),
        )

    def _t_filtered_aggregate(self, call: ToolCall, question: str) -> Answer:
        """مقياس مقيّد بصف واحد بقيمة بُعد محددة (مثلاً: مبيعات اللاذقية
        تحديداً). لا يُستدعى اليوم من RuleRouter (لم يُعدَّل، ولن يُعدَّل هنا) —
        هذه الأداة موجودة لتُستخدَم لاحقاً من مسار الـLLM بعد اجتيازها
        ToolCall Validator. قسم 10 بالمواصفة المعمارية.
        """
        a = call.arguments
        measure, dim, value = a["measure"], a["dimension"], a["value"]
        agg = a.get("agg", "sum")
        if not measure:
            raise ValueError("لا يوجد عمود عددي مناسب في هذا الملف.")
        r = self.engine.aggregate_filtered(measure, dim, value, agg)
        word = {"sum": "إجمالي", "avg": "متوسط", "max": "أعلى قيمة في", "min": "أقل قيمة في"}[agg]
        understood = f"{word} {self._label(measure)} لـ «{value}»"
        if r.evidence.rows_in_scope == 0:
            return Answer(
                question=question, understood_as=understood, tool_calls=[call], metrics=[r],
                answer_ar=(f"لا يوجد أي صف مطابق لـ «{value}» ضمن {self._dim_label(dim)} "
                           f"تحديداً — {word} {self._label(measure)} هو 0."),
            )
        return Answer(
            question=question, understood_as=understood, tool_calls=[call], metrics=[r],
            answer_ar=(f"{word} {self._label(measure)} لـ «{value}» يساوي **{r.formatted_ar}** "
                       f"محسوباً من {count_ar(r.evidence.rows_in_scope, 'صف', 'صفّين', 'صفوف')}."),
        )

    def _t_count(self, call: ToolCall, question: str) -> Answer:
        concept = call.arguments.get("concept")
        r = self.engine.count_distinct(concept) if concept else self.engine.count_rows()
        what = {"invoice_no": "فاتورة", "customer": "عميل", "product_name": "منتج",
                "salesperson": "مندوب", "region": "منطقة"}.get(concept, "صف")
        return Answer(
            question=question, understood_as=f"عدد {what}", tool_calls=[call], metrics=[r],
            answer_ar=f"عدد {what} في البيانات: **{r.formatted_ar}**.",
        )

    def _t_top_n(self, call: ToolCall, question: str) -> Answer:
        a = call.arguments
        measure, dim, n, asc = a["measure"], a["dimension"], a["n"], a.get("ascending", False)
        if not measure:
            raise ValueError("لا يوجد مقياس عددي للترتيب.")
        rows, ev = self.engine.top_n(measure, dim, n=n, ascending=asc)
        if not rows:
            raise ValueError("لا توجد نتائج.")

        # «حصّة من الإجمالي» تصحّ للمقادير التي تُجمع (مبلغ، كمية، رصيد). أمّا
        # سعر الوحدة فمعدّل لا مقدار: مجموع الأسعار رقمٌ لا وجود له، ونسبةُ صنف
        # منه رقمٌ لا معنى له — «هذا الصنف يمثّل 1.2% من الأسعار» جملة فارغة.
        additive = measure not in _NON_ADDITIVE
        total = (self.engine.aggregate(measure, "sum").value or 1) if additive else None
        lines = []
        for i, r in enumerate(rows, 1):
            line = f"{i}. {r['label']} — {self._fmt(measure, r['value'])}"
            if additive and isinstance(total, (int, float)):
                line += f" ({fmt_percent(r['value'] / total * 100)})"
            lines.append(line)
        word = "أقل" if asc else "أعلى"
        metric = MetricResult(value=rows, formatted_ar=f"{len(rows)} نتيجة",
                              unit=None, evidence=ev)
        return Answer(
            question=question,
            understood_as=f"{word} {n} من {self._dim_label(dim)} حسب {self._label(measure)}",
            tool_calls=[call], metrics=[metric],
            answer_ar=(f"{word} {fmt_number(len(rows))} من {self._dim_label(dim)} "
                       f"حسب {self._label(measure)}:\n\n" + "\n".join(lines)),
        )

    def _t_timeseries(self, call: ToolCall, question: str) -> Answer:
        measure = call.arguments["measure"]
        rows, ev = self.engine.timeseries(measure, call.arguments.get("granularity", "month"))
        if not rows:
            raise ValueError("لا يوجد عمود تاريخ صالح.")

        lines = [f"• {r['period'][:7]} — {self._fmt(measure, r['value'])}" for r in rows]
        peak = max(rows, key=lambda r: r["value"])
        first, last = rows[0]["value"], rows[-1]["value"]
        change = ((last - first) / first * 100) if first else None
        tail = ""
        if change is not None:
            direction = "ارتفاع" if change > 0 else "انخفاض"
            tail = (f"\n\nمن أول شهر إلى آخره: {direction} بنسبة "
                    f"{fmt_percent(abs(change))}. أعلى شهر: {peak['period'][:7]} "
                    f"بقيمة {self._fmt(measure, peak['value'])}.")

        metric = MetricResult(value=rows, formatted_ar=f"{len(rows)} فترة", evidence=ev)
        return Answer(
            question=question, understood_as=f"{self._label(measure)} حسب الشهر",
            tool_calls=[call], metrics=[metric],
            answer_ar=f"{self._label(measure)} شهرياً:\n\n" + "\n".join(lines) + tail,
        )

    def _t_compare_periods(self, call: ToolCall, question: str) -> Answer:
        a = call.arguments
        measure, months = a["measure"], a["months"]
        dimension, value = a.get("dimension"), a.get("value")
        if bool(dimension) != bool(value):
            raise ValueError(
                "يجب تحديد dimension وvalue معاً لتطبيق فلتر على المقارنة، أو حذفهما معاً."
            )
        bounds = self.engine.date_bounds()
        if not bounds:
            raise ValueError("لا يوجد عمود تاريخ للمقارنة.")
        try:
            year = date.fromisoformat(bounds[1]).year
        except (ValueError, TypeError):
            raise ValueError(
                f"عمود التاريخ بصيغة ناقصة («{bounds[1]}») — المقارنة بين شهور "
                "تحتاج تواريخ كاملة (سنة-شهر-يوم)."
            )

        def rng(m: int) -> tuple[str, str]:
            last = 31 if m in (1, 3, 5, 7, 8, 10, 12) else (30 if m != 2 else 28)
            return (f"{year}-{m:02d}-01", f"{year}-{m:02d}-{last}")

        # مقارنة نسبية: «هالشهر مقابل الشهر يلي قبلو» ⇒ آخر شهرين فيهما بيانات
        # فعلاً. نأخذهما من مدى التاريخ لا من تاريخ اليوم: الملفات غالباً
        # تاريخية، ومقارنة شهر فارغ بآخر فارغ جواب بلا معنى.
        if a.get("last_two") and len(months) < 2:
            last_month = date.fromisoformat(bounds[1]).month
            prev = last_month - 1
            if prev < 1:
                raise ValueError(
                    "البيانات تغطي شهراً واحداً فقط — لا يوجد شهر سابق للمقارنة."
                )
            months = [prev, last_month]

        r = self.engine.compare_periods(measure, rng(months[0]), rng(months[1]),
                                        filter_concept=dimension, filter_value=value)
        v1, v2, ch = r["period_1"]["value"], r["period_2"]["value"], r["change_pct"]
        n1 = _AR_MONTH_NAMES[months[0]][0]
        n2 = _AR_MONTH_NAMES[months[1]][0]

        verdict = "لا يوجد فرق يُذكر"
        if ch is not None:
            verdict = (f"{'ارتفاع' if ch > 0 else 'انخفاض'} بنسبة {fmt_percent(abs(ch))}")

        label_suffix = f" لـ «{value}»" if value else ""
        metric = MetricResult(value={"v1": v1, "v2": v2, "change_pct": ch},
                              formatted_ar=verdict, evidence=r["evidence"])
        return Answer(
            question=question, understood_as=f"مقارنة {n1} مع {n2}{label_suffix}",
            tool_calls=[call], metrics=[metric],
            answer_ar=(f"{self._label(measure)}{label_suffix} في {n1}: **{self._fmt(measure, v1)}**\n"
                       f"{self._label(measure)}{label_suffix} في {n2}: **{self._fmt(measure, v2)}**\n\n"
                       f"النتيجة: {verdict}."),
        )

    def _low_stock_rows(self, dimension: str) -> tuple[list[tuple], Any]:
        """Return the canonical low-stock rows using one filtering rule."""
        stock = self.schema.by_concept("stock_qty")
        if not stock:
            raise ValueError(
                "لا يوجد عمود رصيد مخزون في هذه البيانات. "
                "المبيعات وحدها لا تكفي لمعرفة المخزون المنخفض."
            )
        scol = stock.column_name
        dim = self.schema.name_of(dimension) or scol
        reorder = self.schema.by_concept("reorder_level")
        if not reorder:
            return [], None

        rcol = reorder.column_name
        sql = (f'SELECT "{dim}" AS label, "{scol}" AS stock, "{rcol}" AS reorder, '
               f'"{scol}" - "{rcol}" AS gap FROM {self.engine.TABLE} '
               f'WHERE "{scol}" <= "{rcol}" ORDER BY gap ASC')
        return self.engine.run(sql, "أصناف تحت حد إعادة الطلب", [scol, rcol, dim])

    def _t_low_stock(self, call: ToolCall, question: str) -> Answer:
        """Find items at/below reorder level; optionally return only the count."""
        stock = self.schema.by_concept("stock_qty")
        if not stock:
            raise ValueError(
                "لا يوجد عمود رصيد مخزون في هذه البيانات. "
                "المبيعات وحدها لا تكفي لمعرفة المخزون المنخفض."
            )

        dimension = call.arguments.get("dimension", "product_name")
        rows, ev = self._low_stock_rows(dimension)
        if ev is None:
            # Without a real reorder level we can still preserve the old
            # informative list behavior, but we must never turn that heuristic
            # list into a count and present it as a threshold-based fact.
            if call.arguments.get("count_only"):
                raise ValueError(
                    "لا يمكن إعطاء عدد دقيق للأصناف تحت حد إعادة الطلب لأن البيانات "
                    "لا تحتوي على عمود «حد إعادة الطلب»."
                )
            rows, ev = self.engine.top_n(
                "stock_qty", dimension, n=10, agg="min", ascending=True
            )
            lines = [f"{i}. {r['label']} — {fmt_number(r['value'])}"
                     for i, r in enumerate(rows, 1)]
            metric = MetricResult(value=rows, formatted_ar=count_ar(len(rows), "صنف", "صنفان", "أصناف"), evidence=ev)
            return Answer(
                question=question, understood_as="الأصناف الأقل رصيداً",
                tool_calls=[call], metrics=[metric],
                answer_ar=("لا يوجد عمود «حد إعادة الطلب» في البيانات، لذلك هذه قائمة "
                           "الأقل رصيداً وليست بالضرورة أصنافاً منخفضة فعلاً:\n\n"
                           + "\n".join(lines)),
            )

        if call.arguments.get("count_only"):
            count = len(rows)
            metric = MetricResult(
                value=count,
                formatted_ar=fmt_number(count),
                unit="items",
                evidence=ev,
            )
            return Answer(
                question=question,
                understood_as="عدد الأصناف تحت حد إعادة الطلب",
                tool_calls=[call],
                metrics=[metric],
                answer_ar=f"عدد الأصناف تحت حد إعادة الطلب: **{fmt_number(count)}**.",
            )

        if not rows:
            return Answer(
                question=question, understood_as="أصناف تحت حد إعادة الطلب",
                tool_calls=[call],
                metrics=[MetricResult(value=[], formatted_ar="لا يوجد", evidence=ev)],
                answer_ar=("لا يوجد أي صنف تحت حد إعادة الطلب حالياً — "
                           "كل الأصناف ضمن المستوى الآمن."),
            )

        lines = [f"{i}. {r[0]} — الرصيد {fmt_number(r[1])} مقابل حد طلب "
                 f"{fmt_number(r[2])} (نقص {fmt_number(abs(r[3]))})"
                 for i, r in enumerate(rows, 1)]
        metric = MetricResult(
            value=[dict(zip(("label", "stock", "reorder", "gap"), r)) for r in rows],
            formatted_ar=count_ar(len(rows), "صنف", "صنفان", "أصناف"),
            evidence=ev,
        )
        return Answer(
            question=question, understood_as="أصناف وصلت حد إعادة الطلب",
            tool_calls=[call], metrics=[metric],
            answer_ar=(f"{count_ar(len(rows), 'صنف بلغ', 'صنفان بلغا', 'أصناف بلغت')} أو تجاوز حد إعادة الطلب:\n\n"
                       + "\n".join(lines)),
        )

    def _t_stagnant_items(self, call: ToolCall, question: str) -> Answer:
        dim, measure = call.arguments["dimension"], call.arguments["measure"]
        rows, ev = self.engine.top_n(measure, dim, n=5, ascending=True)
        if not rows:
            raise ValueError("لا توجد بيانات كافية.")
        lines = [f"{i}. {r['label']} — {self._fmt(measure, r['value'])}"
                 for i, r in enumerate(rows, 1)]
        metric = MetricResult(value=rows, formatted_ar=f"{len(rows)} عنصر", evidence=ev)
        return Answer(
            question=question, understood_as=f"الأبطأ حركة من {self._dim_label(dim)}",
            tool_calls=[call], metrics=[metric],
            answer_ar=("الأقل حركة (لا يعني بالضرورة راكداً — يحتاج مقارنة بالمخزون):\n\n"
                       + "\n".join(lines)),
        )


class AskPhoenix:
    """الواجهة الوحيدة لطبقة الأسئلة."""

    def __init__(self, engine, schema: SemanticSchema, router: QuestionRouter | None = None):
        self.router = router or RuleRouter()
        self.executor = ToolExecutor(engine, schema)
        self.schema = schema

    def ask(self, question: str) -> Answer:
        call = self.router.route(question, self.schema)
        return self.executor.execute(call, question)

    def suggested_questions(self) -> list[str]:
        """أسئلة مقترحة مبنية على الـschema الفعلية لا على قالب ثابت."""
        s, out = self.schema, []
        if s.by_concept("total_amount"):
            out.append("كم إجمالي المبيعات؟")
        if s.by_concept("product_name") and s.by_concept("total_amount"):
            out.append("مين أكتر 5 منتجات مبيعاً؟")
        if s.by_concept("date") and s.by_concept("total_amount"):
            out.append("المبيعات حسب الشهر")
        if s.by_concept("salesperson"):
            out.append("مين أفضل مندوب؟")
        if s.by_concept("customer"):
            out.append("أكبر 3 عملاء")
        if s.by_concept("invoice_no"):
            out.append("كم عدد الفواتير؟")
        return out[:6]


# ------------------------------------------------------- الأسئلة المقترحة

def suggest_questions(schema: SemanticSchema, limit: int = 4) -> list[str]:
    """أسئلة مقترحة مبنية على أعمدة هذا الملف بالذات (الخطة §8.5).

    لماذا لا نكتبها في الواجهة؟ لأن اقتراح «المبيعات حسب الشهر» على ملف بلا
    عمود تاريخ يعطي «لم أفهم» عند أول ضغطة — وهذا أسوأ أثر ممكن في أول
    تجربة للمستخدم. المحرك وحده يعرف ما يستطيع الإجابة عنه.

    كل اقتراح هنا مضمون أن يمرّ من الموجّه ويُنتج رقماً — يحرس ذلك اختبار.
    """
    has = {c.concept for c in schema.columns if c.concept}
    roles = {c.role for c in schema.columns}
    money = "total_amount" in has
    stock = schema.domain in ("inventory", "warehouse", "pharmacy")
    out: list[str] = []

    # لقطة جرد: الأسئلة نفسها بصياغة المبيعات تعطي جملاً كاذبة، والأسئلة
    # الزمنية لا معنى لها أصلاً في ملفٍ بلا تاريخ.
    if stock:
        if money:
            out.append("كم قيمة المخزون؟")
        if "stock_qty" in has:
            out.append("كم إجمالي الرصيد؟")
        if "product_name" in has and (money or "stock_qty" in has):
            out.append("شو أعلى 5 أصناف قيمةً؟")
        if "unit_price" in has and "product_name" in has:
            out.append("شو أغلى 5 أصناف؟")
        return out[:limit] if out else ["كم عدد الصفوف؟"]

    if money:
        out.append("كم إجمالي المبيعات؟")
    elif "quantity" in has:
        out.append("كم إجمالي الكمية؟")

    if "product_name" in has and (money or "quantity" in has):
        out.append("مين أكتر 5 منتجات مبيعاً؟")

    if "time" in roles and (money or "quantity" in has):
        out.append("المبيعات حسب الشهر")

    if "region" in has and (money or "quantity" in has):
        out.append("المبيعات حسب المنطقة")
    elif "salesperson" in has and (money or "quantity" in has):
        out.append("مين أفضل مندوب؟")
    elif "customer" in has and (money or "quantity" in has):
        out.append("مين أكبر 5 عملاء؟")

    if not out:
        # ملف بلا مقياس معروف: نسأل عمّا نملكه يقيناً — حجم البيانات نفسه
        out.append("كم عدد الصفوف؟")

    return out[:limit]
