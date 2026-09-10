"""B7 — اختيار الرسم وبناء ChartSpec.

قاعدة الفصل (خطة المشروع §5.8): المحرك يقرّر **نوع** الرسم ويجهّز بياناته، ولا
يكتب سطر واحد من كود ECharts. الواجهة تترجم ChartSpec فقط، فلا تحمل أي منطق.

فائدة عملية للاختبار: قرار «أي رسم يناسب هذه البيانات؟» يصير قابلاً للفحص
بلا متصفح ولا لقطة شاشة.
"""
from __future__ import annotations

from typing import Any, Literal

from .analytics import AnalyticsEngine
from .arabic_text import fmt_number, count_ar
from .models import AxisSpec, ChartSpec, Evidence, SemanticSchema

ChartType = Literal["line", "bar", "bar_horizontal", "scatter", "histogram", "donut", "table"]

# العتبات كما وردت في خطة المشروع §5.8 — لم نخترع غيرها.
TIME_POINTS_FOR_LINE = 12      # أكثر من 12 نقطة زمنية ⇒ خط بدل أعمدة

# أسماء المنتجات والعملاء بالعربية طويلة («حامل لابتوب معدني»، «كيبورد لوجيتك
# لاسلكي»). بالأعمدة الرأسية تُدار التسميات 90° أو تُقصّ فتصير غير مقروءة —
# شوهد فعلياً على شاشة الجوال. لذلك نبقى أفقيين حتى 12 فئة، وهو ما يغطّي
# أعلى-10 الافتراضية. (الخطة قالت 8؛ رفعناها لسبب مقروئية عربية عملي.)
SMALL_CARDINALITY = 12
DONUT_MAX_SLICES = 5           # جزء-من-كل يبقى مقروءاً حتى 5 شرائح
MAX_POINTS = 500               # سقف نقاط أي رسم (الباقي يُجمَّع أو يُقصّ)

_LABELS_AR = {
    "total_amount": "الإجمالي",
    "quantity": "الكمية",
    "unit_price": "سعر الوحدة",
    "stock_qty": "المخزون",
    "opening_stock": "الرصيد الافتتاحي",
    "closing_stock": "الرصيد الختامي",
    "purchases": "المشتريات",
    "collected_amount": "المحصّل",
    "outstanding_amount": "المستحق",
    "margin_percentage": "نسبة الهامش",
    "exchange_rate": "سعر الصرف",
    "product_name": "المنتج",
    "customer": "العميل",
    "salesperson": "المندوب",
    "region": "المنطقة",
    "category": "التصنيف",
    "date": "التاريخ",
}



def _cats(n: int) -> str:
    """«3 فئات» لا «3 فئة» — تمييز العدد العربي (انظر count_ar)."""
    return count_ar(n, "فئة", "فئتان", "فئات")


# جمع المفاهيم — لعناوين العدّ فقط. «عدد العميل» خطأ يقرأه المستخدم في
# أول بطاقة على الشاشة؛ الصحيح «عدد العملاء».
_PLURALS_AR = {
    "customer": "العملاء",
    "product_name": "المنتجات",
    "salesperson": "المندوبين",
    "region": "المناطق",
    "category": "التصنيفات",
}


# نفس المفهوم يُسمّى غير اسمه باختلاف الملف. «الإجمالي» على ملف مبيعات صحيح،
# وعلى كتالوج مستودع مضلِّل: الرقم هناك قيمةُ بضاعةٍ على الرف لا مالٌ دخل.
_LABELS_BY_DOMAIN: dict[str, dict[str, str]] = {
    "inventory": {"total_amount": "قيمة المخزون", "stock_qty": "الرصيد",
                  "unit_price": "السعر"},
    "warehouse": {"total_amount": "قيمة المخزون", "stock_qty": "الرصيد"},
    "pharmacy": {"total_amount": "قيمة المخزون", "stock_qty": "الرصيد"},
}


# تسميات تُقرأ عنواناً تاماً بذاتها، فلا يُضاف إليها «إجمالي»
_SELF_TITLED = {"قيمة المخزون", "المخزون", "الرصيد", "المحصّل", "المستحق"}


def label_ar(concept: str, domain: str = "generic") -> str:
    by_domain = _LABELS_BY_DOMAIN.get(domain, {}).get(concept)
    return by_domain or _LABELS_AR.get(concept, concept)


def label_plural_ar(concept: str) -> str:
    """يُستعمل بعد «عدد». يعود للمفرد إن لم يكن للمفهوم جمع معروف —
    فمفهوم جديد يعطي عنواناً ركيكاً لا عنواناً خاطئاً."""
    return _PLURALS_AR.get(concept, label_ar(concept))


def choose_chart(
    x_role: str,
    *,
    points: int = 0,
    cardinality: int = 0,
    measures: int = 1,
    part_of_whole: bool = False,
) -> ChartType:
    """نوع البيانات ⇒ نوع الرسم. دالة نقية بلا أي حالة — أسهل شيء للاختبار.

    x_role: "time" | "dimension" | "measure" | "none"
    """
    if x_role == "time":
        return "line" if points > TIME_POINTS_FOR_LINE else "bar"

    if x_role == "dimension":
        if part_of_whole and cardinality <= DONUT_MAX_SLICES:
            return "donut"
        # أفقي للعربية: أسماء المنتجات والعملاء طويلة، والأفقي يقرأها بلا دوران
        return "bar_horizontal" if cardinality <= SMALL_CARDINALITY else "bar"

    if x_role == "measure":
        return "scatter" if measures >= 2 else "histogram"

    return "table"


def _axis(field: str, concept: str, kind: str, domain: str = "generic") -> AxisSpec:
    return AxisSpec(field=field, label_ar=label_ar(concept, domain), type=kind)


def _short(v: float) -> str:
    """رقم مختصر لتسميات المدى: 2.5M بدل 2,500,000 — يمنع التراكب على الجوال."""
    a = abs(v)
    if a >= 1_000_000_000:
        return f"{v / 1_000_000_000:.1f}B"
    if a >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if a >= 1_000:
        return f"{v / 1_000:.0f}K"
    return fmt_number(round(v))


def _period_label(period: Any, granularity: str) -> str:
    """«2026-08-01 00:00:00» لفترة شهرية تصير «2026-08» — الوقت هنا ضجيج لا معلومة."""
    s = str(period)
    cut = {"year": 4, "month": 7, "week": 10, "day": 10}.get(granularity)
    return s[:cut] if cut and len(s) >= cut else s


def build_timeseries_chart(
    engine: AnalyticsEngine, measure: str, granularity: str = "month"
) -> ChartSpec | None:
    """اتجاه زمني — يُبنى فقط لو الملف فيه عمود تاريخ ومقياس فعليان."""
    if engine.schema.by_concept(measure) is None or engine.schema.by_concept("date") is None:
        return None
    rows, ev = engine.timeseries(measure, granularity)
    if not rows:
        return None
    data = [{"x": _period_label(r["period"], granularity), "y": r["value"]}
            for r in rows][:MAX_POINTS]
    ctype = choose_chart("time", points=len(data))
    gran_ar = {"day": "يومياً", "week": "أسبوعياً", "month": "شهرياً", "year": "سنوياً"}
    return ChartSpec(
        type=ctype,
        title_ar=f"{label_ar(measure, engine.schema.domain)} {gran_ar.get(granularity, granularity)}",
        x=_axis("x", "date", "time"),
        y=[_axis("y", measure, "value")],
        data=data,
        options={"granularity": granularity},
        reason_ar=(f"عمود زمني مع مقياس، و{count_ar(len(data), 'نقطة', 'نقطتان', 'نقاط')} "
                   f"⇒ {'خط لإظهار الاتجاه' if ctype == 'line' else 'أعمدة (النقاط قليلة)'}"),
        evidence=ev,
    )


def build_top_n_chart(
    engine: AnalyticsEngine, measure: str, dimension: str, n: int = 10
) -> ChartSpec | None:
    """أعلى العناصر حسب بُعد — أكثر رسم مفيد عملياً بملفات المبيعات."""
    if engine.schema.by_concept(measure) is None or engine.schema.by_concept(dimension) is None:
        return None
    rows, ev = engine.top_n(measure, dimension, n=n)
    if not rows:
        return None
    data = [{"x": r["label"], "y": r["value"]} for r in rows][:MAX_POINTS]
    # جزء-من-كل: القيم كلها موجبة والعدد صغير ⇒ الدائري المفرّغ يقرأ الحصص أفضل
    part_of_whole = all(d["y"] is not None and d["y"] > 0 for d in data)
    ctype = choose_chart("dimension", cardinality=len(data), part_of_whole=part_of_whole)
    reason = {
        "donut": f"{_cats(len(data))} فقط بقيم موجبة ⇒ حصص كل فئة من الإجمالي",
        "bar_horizontal": f"{_cats(len(data))} ⇒ أعمدة أفقية (أسماء عربية طويلة تُقرأ أوضح)",
        "bar": f"{_cats(len(data))} ⇒ أعمدة رأسية لأعلى العناصر",
    }[ctype]
    return ChartSpec(
        type=ctype,
        title_ar=(f"{label_ar(measure, engine.schema.domain)} حسب "
                  f"{label_ar(dimension, engine.schema.domain)}"),
        x=_axis("x", dimension, "category"),
        y=[_axis("y", measure, "value")],
        data=data,
        options={"sorted": True, "top_n": n},
        reason_ar=reason,
        evidence=ev,
    )


def build_distribution_chart(engine: AnalyticsEngine, measure: str,
                             bins: int = 12) -> ChartSpec | None:
    """توزيع قياس واحد — يكشف التركّز والذيول الطويلة."""
    col = engine.schema.by_concept(measure)
    if col is None:
        return None
    sql = (f'SELECT min("{col.column_name}") AS lo, max("{col.column_name}") AS hi '
           f'FROM {engine.TABLE} WHERE "{col.column_name}" IS NOT NULL')
    rows, _ = engine.run(sql, f"range({measure})", [col.column_name])
    if not rows or rows[0][0] is None:
        return None
    lo, hi = float(rows[0][0]), float(rows[0][1])
    if hi <= lo:
        return None
    width = (hi - lo) / bins
    sql = (f'SELECT floor(("{col.column_name}" - {lo}) / {width}) AS bucket, '
           f'count(*) AS c FROM {engine.TABLE} '
           f'WHERE "{col.column_name}" IS NOT NULL GROUP BY bucket ORDER BY bucket')
    rows, ev = engine.run(sql, f"histogram({measure})", [col.column_name])
    data = []
    for b, c in rows:
        if b is None:
            continue
        start = lo + float(b) * width
        # U+200E (علامة اتجاه يسار-يمين) قبل المدى: بدونها ينقلب ترتيب المدى
        # داخل صفحة عربية فيظهر «85,416 – 45,000-» بدل «-45,000 – 85,416».
        label = f"‎{_short(start)} – {_short(start + width)}"
        data.append({"x": label, "y": int(c)})
    if not data:
        return None
    return ChartSpec(
        type=choose_chart("measure", measures=1),
        title_ar=f"توزيع {label_ar(measure, engine.schema.domain)}",
        x=_axis("x", measure, "category"),
        y=[AxisSpec(field="y", label_ar="عدد الصفوف", type="value")],
        data=data[:MAX_POINTS],
        options={"bins": bins},
        reason_ar=f"قياس عددي واحد ⇒ مدرّج تكراري على {_cats(bins)} يُظهر التوزيع والتركّز",
        evidence=ev,
    )


def suggest_charts(engine: AnalyticsEngine, limit: int = 3) -> list[ChartSpec]:
    """يقرأ الـschema ويبني الرسوم المناسبة فعلياً لهذا الملف — لا رسوم فارغة.

    الأولوية: الاتجاه الزمني (لو في تاريخ) ← التوزيع حسب أهم بُعد ← توزيع القياس.
    """
    schema: SemanticSchema = engine.schema
    measure = next((c for c in ("total_amount", "quantity", "stock_qty", "outstanding_amount")
                    if schema.by_concept(c)), None)
    if measure is None:
        measures = [c.concept for c in schema.by_role("measure") if c.concept]
        measure = measures[0] if measures else None
    if measure is None:
        return []

    out: list[ChartSpec] = []

    if schema.by_concept("date"):
        chart = build_timeseries_chart(engine, measure)
        if chart:
            out.append(chart)

    for dim in ("product_name", "customer", "salesperson", "region", "category"):
        if len(out) >= limit:
            break
        if schema.by_concept(dim):
            chart = build_top_n_chart(engine, measure, dim)
            if chart:
                out.append(chart)
                break

    if len(out) < limit:
        chart = build_distribution_chart(engine, measure)
        if chart:
            out.append(chart)

    return out[:limit]


def build_kpis(engine: AnalyticsEngine) -> list[dict[str, Any]]:
    """مؤشرات الرأس — كل رقم من المحرك مع دليله (قاعدة ذهبية #1)."""
    kpis: list[dict[str, Any]] = []
    schema = engine.schema

    rows_res = engine.count_rows()
    kpis.append({"key": "rows", "label_ar": "عدد الصفوف",
                 "value": rows_res.value, "formatted_ar": rows_res.formatted_ar,
                 "unit": None, "evidence": _ev(rows_res.evidence)})

    for concept, agg in (("total_amount", "sum"), ("quantity", "sum"),
                         ("outstanding_amount", "sum"), ("stock_qty", "sum")):
        if schema.by_concept(concept) is None:
            continue
        res = engine.aggregate(concept, agg)
        name = label_ar(concept, schema.domain)
        # «إجمالي الإجمالي» ركيك، و«إجمالي قيمة المخزون» أركك: التسمية المرتبطة
        # بالمجال مكتوبة أصلاً كعنوان تام، فلا تُسبَق بشيء.
        standalone = name.startswith("الإجمالي") or name in _SELF_TITLED
        title = name if standalone else f"إجمالي {name}"
        kpis.append({"key": concept, "label_ar": title,
                     "value": res.value, "formatted_ar": res.formatted_ar,
                     "unit": res.unit, "evidence": _ev(res.evidence)})
        if len(kpis) >= 4:
            break

    for dim in ("product_name", "customer"):
        if len(kpis) >= 4:
            break
        if schema.by_concept(dim) is None:
            continue
        res = engine.count_distinct(dim)
        kpis.append({"key": f"{dim}_count", "label_ar": f"عدد {label_plural_ar(dim)}",
                     "value": res.value, "formatted_ar": res.formatted_ar,
                     "unit": None, "evidence": _ev(res.evidence)})

    return kpis


def _ev(evidence: Evidence | None) -> dict | None:
    return evidence.model_dump(mode="json") if evidence is not None else None
