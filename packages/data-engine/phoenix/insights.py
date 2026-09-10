"""B6 — الاكتشافات. كل insight ينشأ من قاعدة رقمية أولاً ثم يُصاغ لغوياً.

لا LLM هنا. لو تعطّل الذكاء الاصطناعي، هذه الطبقة تعمل كما هي.
"""
from __future__ import annotations

from datetime import date, timedelta

from .arabic_text import count_ar, fmt_currency, fmt_number, fmt_percent
from .models import DatasetProfile, Insight, SemanticSchema

GROWTH_THRESHOLD_PCT = 10.0
TOP_SHARE_THRESHOLD_PCT = 25.0
PARETO_ENTITY_THRESHOLD_PCT = 20.0
QUALITY_NULL_THRESHOLD_PCT = 15.0


def generate(engine, schema: SemanticSchema, profile: DatasetProfile) -> list[Insight]:
    out: list[Insight] = []
    measure = _primary_measure(schema)

    if measure:
        out += _growth_rules(engine, measure)
        out += _concentration_rules(engine, schema, measure)
        out += _outlier_rules(engine, schema, measure)
    out += _inventory_rules(engine, schema)
    out += _quality_rules(profile)

    out.sort(key=lambda i: -i.importance_score)
    for n, i in enumerate(out, 1):
        i.id = f"ins{n:02d}"
    return out


def _primary_measure(schema: SemanticSchema) -> str | None:
    for concept in ("total_amount", "quantity", "stock_qty"):
        if schema.by_concept(concept):
            return concept
    m = schema.by_role("measure")
    return m[0].concept if m and m[0].concept else None


def _is_currency(schema: SemanticSchema, concept: str) -> bool:
    c = schema.by_concept(concept)
    return bool(c and c.unit == "currency")


def _fmt(schema: SemanticSchema, concept: str, v: float) -> str:
    if not _is_currency(schema, concept):
        return fmt_number(v)
    return fmt_currency(v, schema.currency.symbol_ar)


# ---------------------------------------------------------------- النمو
def _growth_rules(engine, measure: str) -> list[Insight]:
    bounds = engine.date_bounds()
    if not bounds:
        return []
    try:
        lo, hi = date.fromisoformat(bounds[0]), date.fromisoformat(bounds[1])
    except (ValueError, TypeError):
        # عمود زمني بصيغة ناقصة (مثل "2026-08" شهر بلا يوم) — نتخطّى تحليل النمو
        # بدل ما ننهار. اختراع يوم مفقود تخمين، والانهيار أسوأ من الصمت.
        return []
    span = (hi - lo).days
    if span < 14:
        return []

    mid = lo + timedelta(days=span // 2)
    cmp = engine.compare_periods(
        measure,
        (lo.isoformat(), mid.isoformat()),
        ((mid + timedelta(days=1)).isoformat(), hi.isoformat()),
    )
    ch = cmp["change_pct"]
    if ch is None or abs(ch) < GROWTH_THRESHOLD_PCT:
        return []

    up = ch > 0
    return [Insight(
        id="", type="growth" if up else "decline",
        severity="info" if up else "warning",
        icon="📈" if up else "📉",
        title_ar=f"{'ارتفاع' if up else 'انخفاض'} بنسبة {fmt_percent(abs(ch))}",
        description_ar=(
            f"{'ارتفعت' if up else 'انخفضت'} القيمة الإجمالية بنسبة {fmt_percent(abs(ch))} "
            f"في النصف الثاني من الفترة ({mid.isoformat()} ← {hi.isoformat()}) "
            f"مقارنة بالنصف الأول."
        ),
        importance_score=min(abs(ch) / 100 * 3 + 0.5, 3.0),
        evidence=cmp["evidence"],
    )]


# ---------------------------------------------------------------- التركّز
def _measure_label(schema: SemanticSchema, measure: str) -> str:
    """«80% من الإجمالي» على كتالوج مستودع جملة مضلِّلة: الرقم قيمةُ بضاعةٍ
    راكدة على الرف، لا مالٌ دخل. الاسم يتبع الملف لا المفهوم."""
    from .charts import label_ar
    return label_ar(measure, schema.domain)


def _concentration_rules(engine, schema: SemanticSchema, measure: str) -> list[Insight]:
    out: list[Insight] = []
    stock = schema.domain in ("inventory", "warehouse", "pharmacy")
    item = "صنف" if stock else "منتج"
    total_label = _measure_label(schema, measure)
    for dim, label in (("product_name", item), ("customer", "عميل"), ("salesperson", "مندوب")):
        if not schema.by_concept(dim):
            continue
        try:
            c, ev = engine.concentration(measure, dim)
        except ValueError:
            continue

        if c["top_share_pct"] >= TOP_SHARE_THRESHOLD_PCT:
            out.append(Insight(
                id="", type="concentration", severity="warning", icon="⚠️",
                title_ar=f"تركّز مرتفع في {label} واحد",
                description_ar=(
                    f"«{c['top_label']}» يمثّل {fmt_percent(c['top_share_pct'])} من {total_label} "
                    f"({_fmt(schema, measure, c['top_value'])}) — اعتماد كبير على {label} واحد."
                ),
                importance_score=c["top_share_pct"] / 100 * 3,
                evidence=ev,
            ))

        k, total = c["entities_for_80pct"], c["total_entities"]
        if k and total > 4 and (k / total * 100) <= PARETO_ENTITY_THRESHOLD_PCT:
            out.append(Insight(
                id="", type="concentration", severity="info", icon="💡",
                title_ar=f"{fmt_number(k)} {label} يشكّلون 80% من {total_label}",
                description_ar=(
                    f"{fmt_number(k)} فقط من أصل {fmt_number(total)} {label} "
                    f"({fmt_percent(k/total*100)}) يشكّلون 80% من {total_label} — "
                    + ("راجع هذه الأصناف أولاً: عليها يقوم رأس مالك."
                       if stock else "تركيز الجهد عليهم يعطي أعلى عائد.")
                ),
                importance_score=1.8,
                evidence=ev,
            ))
    return out


# ---------------------------------------------------------------- الشذوذ
def _outlier_rules(engine, schema: SemanticSchema, measure: str) -> list[Insight]:
    try:
        rows, ev = engine.outlier_rows(measure, limit=3)
    except ValueError:
        return []
    if not rows:
        return []

    mcol = schema.name_of(measure)
    top = rows[0]
    val = top.get(mcol)
    desc_col = schema.name_of("product_name") or schema.name_of("customer")
    what = f"«{top.get(desc_col)}» " if desc_col and top.get(desc_col) else ""
    dcol = schema.name_of("date")
    when = f" بتاريخ {top.get(dcol)}" if dcol and top.get(dcol) else ""

    return [Insight(
        id="", type="anomaly", severity="warning", icon="🚨",
        title_ar=f"قيمة غير معتادة: {_fmt(schema, measure, float(val or 0))}",
        description_ar=(
            f"أعلى قيمة شاذة {what}تبلغ {_fmt(schema, measure, float(val or 0))}{when}. "
            f"تم رصد {count_ar(len(rows), 'قيمة شاذة', 'قيمتين شاذتين', 'قيم شاذة')} على الأقل — تستحق المراجعة "
            f"(قد تكون طلبية جملة حقيقية أو خطأ إدخال)."
        ),
        importance_score=2.2,
        evidence=ev,
    )]


# ---------------------------------------------------------------- الجرد
DEAD_STOCK_THRESHOLD_PCT = 10.0
ZERO_PRICE_THRESHOLD_PCT = 2.0


def _count_where(engine, column: str, condition: str) -> int:
    rows, _ = engine.run(
        f'SELECT count(*) FROM {engine.TABLE} WHERE "{column}" {condition}',
        f"count({column} {condition})", [column])
    return int(rows[0][0]) if rows else 0


def _inventory_rules(engine, schema: SemanticSchema) -> list[Insight]:
    """قواعد لا معنى لها إلا على لقطة جرد — وهي أول ما يسأل عنه صاحب مستودع.

    «كم صنف بلا رصيد؟» و«كم صنف بلا سعر؟» سؤالان يُجابان برقم واحد، ويغيّران
    قراراً فعلياً: الأول بضاعةٌ ناقصة أو أصنافٌ ماتت، والثاني بيانات ناقصة
    تُفسد كل حساب قيمة بعدها بصمت.
    """
    if schema.domain not in ("inventory", "warehouse", "pharmacy"):
        return []
    out: list[Insight] = []
    total = engine.row_total or 1

    stock = schema.by_concept("stock_qty") or schema.by_concept("quantity")
    if stock is not None:
        zero = _count_where(engine, stock.column_name, "= 0")
        pct = zero / total * 100
        if pct >= DEAD_STOCK_THRESHOLD_PCT:
            out.append(Insight(
                id="", type="opportunity",
                severity="warning" if pct < 40 else "critical", icon="📦",
                title_ar=f"{count_ar(zero, 'صنف بلا رصيد', 'صنفان بلا رصيد', 'أصناف بلا رصيد')}",
                description_ar=(
                    f"{fmt_percent(pct)} من الأصناف رصيدها صفر — إمّا نفدت وتحتاج طلباً، "
                    "أو لم تعد تُباع وتشغل مكاناً في الكتالوج. راجعها قبل الجرد القادم."),
                importance_score=1.5 + pct / 100,
            ))

    price = schema.by_concept("unit_price")
    if price is not None:
        zero = _count_where(engine, price.column_name, "= 0")
        pct = zero / total * 100
        if pct >= ZERO_PRICE_THRESHOLD_PCT:
            out.append(Insight(
                id="", type="quality", severity="warning", icon="🏷️",
                title_ar=f"{count_ar(zero, 'صنف بلا سعر', 'صنفان بلا سعر', 'أصناف بلا سعر')}",
                description_ar=(
                    f"{fmt_percent(pct)} من الأصناف سعرها صفر — قيمة المخزون المحسوبة "
                    "أقلّ من الحقيقة بمقدار قيمة هذه الأصناف."),
                importance_score=1.2 + pct / 100,
            ))
    return out


# ---------------------------------------------------------------- الجودة
def _quality_rules(profile: DatasetProfile) -> list[Insight]:
    out: list[Insight] = []
    for c in profile.columns:
        for i in c.quality_issues:
            if i.kind == "missing_values" and i.affected_pct >= QUALITY_NULL_THRESHOLD_PCT:
                out.append(Insight(
                    id="", type="quality", severity="warning", icon="⚠️",
                    title_ar=f"بيانات ناقصة في «{c.name}»",
                    description_ar=(
                        f"{count_ar(i.affected_count, 'قيمة مفقودة', 'قيمتان مفقودتان', 'قيم مفقودة')} "
                        f"({fmt_percent(i.affected_pct)}) — التحليلات المعتمدة على هذا "
                        f"العمود تغطي جزءاً من البيانات فقط."
                    ),
                    importance_score=i.affected_pct / 100 * 2 + 0.5,
                ))
    if profile.duplicate_row_count:
        out.append(Insight(
            id="", type="quality", severity="info", icon="🧹",
            title_ar=(f"{count_ar(profile.duplicate_row_count, 'صف مكرر أُزيل', 'صفّان مكرران أُزيلا', 'صفوف مكررة أُزيلت')}"),
            description_ar=(
                f"عُثر على {count_ar(profile.duplicate_row_count, 'صف مطابق', 'صفّين مطابقين', 'صفوف مطابقة')} تماماً "
                f"لصفوف أخرى وأُزيل قبل التحليل لمنع تضخيم الأرقام."
            ),
            importance_score=0.9,
        ))
    return out
