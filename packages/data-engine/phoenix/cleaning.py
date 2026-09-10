"""B4 — التنظيف. وصفة تصريحية قابلة للتتبع والتعطيل وإعادة التطبيق.

قاعدة ذهبية #3: البيانات الخام لا تُمس. كل تنظيف يُنتج إطاراً جديداً + سجل تغييرات.
قاعدة صارمة: لا حذف صفوف بسبب قيم شاذة — الوسم فقط.
"""
from __future__ import annotations

import re

import polars as pl

from .arabic_text import (count_ar, fmt_number, has_indic_digits, normalize_ar,
                          normalize_for_display, parse_date_parts, parse_number,
                          to_western_digits)
from .models import (ChangeLog, CleaningOperation, CleaningRecipe, DatasetProfile,
                     OperationResult, SemanticSchema)

MAX_EXAMPLES = 5

# بادئة أعمدة الوسم التي يضيفها المحرك. معلنة هنا لا مكرّرة في كل طبقة:
# الواجهة تحتاج معرفة أي عمود وسمٌ لأيّ عمود، وتخمينها من الاسم يربطها
# باصطلاح داخلي قد يتغيّر.
OUTLIER_PREFIX = "_شاذ_"
FUZZY_MERGE_THRESHOLD = 0.90


# ------------------------------------------------------------ 1) التخطيط
def plan_cleaning(profile: DatasetProfile, schema: SemanticSchema) -> CleaningRecipe:
    """يقرأ التوصيف ويقترح العمليات اللازمة فقط — لا عمليات بلا سبب."""
    ops: list[CleaningOperation] = []
    n = 0

    def add(otype: str, cols: list[str], reason: str, **params):
        nonlocal n
        n += 1
        ops.append(CleaningOperation(id=f"op{n:02d}", type=otype, target_columns=cols,
                                     reason_ar=reason, params=params))

    text_cols = [c.name for c in profile.columns
                 if c.inferred_type in ("text", "categorical", "identifier")]
    num_cols = [c.name for c in profile.columns if c.inferred_type in ("integer", "float")]
    date_cols = [c.name for c in profile.columns if c.inferred_type in ("date", "datetime")]

    ws = [i for c in profile.columns for i in c.quality_issues if i.kind == "whitespace"]
    if ws:
        add("trim_whitespace", [i.column for i in ws],
            f"{sum(i.affected_count for i in ws)} قيمة تحتوي مسافات زائدة")

    if text_cols:
        add("normalize_arabic", text_cols, "توحيد شكل الحروف العربية لتفادي تكرار نفس الاسم")

    indic = [i for c in profile.columns for i in c.quality_issues if i.kind == "arabic_indic_digits"]
    if indic:
        add("normalize_digits", [i.column for i in indic],
            f"{sum(i.affected_count for i in indic)} قيمة بأرقام هندية ستتحول إلى الأرقام الإنجليزية")

    if num_cols:
        add("parse_numbers", num_cols, "تحويل الأسعار النصية ورموز العملة إلى أرقام حقيقية")
    if date_cols:
        add("parse_dates", date_cols, "توحيد صيغ التاريخ المختلطة")

    if profile.duplicate_row_count:
        add("deduplicate_rows", [], count_ar(profile.duplicate_row_count, "صف مكرر بالكامل", "صفّان مكرران بالكامل", "صفوف مكررة بالكامل"))

    for sc in schema.by_role("dimension"):
        p = profile.column(sc.column_name)
        if p and p.inferred_type in ("categorical", "text") and 1 < p.unique_count <= 200:
            add("fuzzy_entity_merge", [sc.column_name],
                f"توحيد الأسماء المتشابهة في «{sc.column_name}»")

    out = [i for c in profile.columns for i in c.quality_issues if i.kind == "outliers"]
    if out:
        add("flag_outliers", [i.column for i in out],
            "وسم القيم الشاذة دون حذفها", method="iqr")

    return CleaningRecipe(operations=ops)


# ------------------------------------------------------------ 2) التنفيذ
def _s(df: pl.DataFrame, col: str) -> pl.Series:
    return df[col].cast(pl.Utf8, strict=False)


def execute_recipe(df: pl.DataFrame, recipe: CleaningRecipe) -> tuple[pl.DataFrame, ChangeLog]:
    rows_before = df.height
    results: list[OperationResult] = []

    for op in recipe.operations:
        if not op.enabled:
            continue
        handler = _HANDLERS.get(op.type)
        if handler is None:
            continue
        df, res = handler(df, op)
        results.append(res)

    return df, ChangeLog(results=results, rows_before=rows_before, rows_after=df.height)


def _apply_string_op(df, op, fn, label: str):
    """قالب موحّد: يطبّق دالة على أعمدة نصية ويسجّل ما تغيّر فعلياً."""
    changed, examples = 0, []
    for col in op.target_columns:
        if col not in df.columns:
            continue
        old = _s(df, col).to_list()
        new = [fn(v) if v is not None else None for v in old]
        for i, (o, nv) in enumerate(zip(old, new)):
            if o != nv:
                changed += 1
                if len(examples) < MAX_EXAMPLES:
                    examples.append({"row": i + 1, "column": col, "before": o, "after": nv})
        df = df.with_columns(pl.Series(col, new, dtype=pl.Utf8))
    return df, OperationResult(
        operation_id=op.id, operation_type=op.type, cells_changed=changed,
        columns_affected=op.target_columns, examples=examples,
        summary_ar=f"{label}: {count_ar(changed, 'خلية', 'خليتان', 'خلايا')}",
    )


def _op_trim(df, op):
    return _apply_string_op(df, op, lambda v: v.strip(), "إزالة مسافات زائدة")


def _op_normalize_arabic(df, op):
    return _apply_string_op(df, op, normalize_for_display, "توحيد شكل النص العربي")


def _op_normalize_digits(df, op):
    return _apply_string_op(df, op, to_western_digits,
                            "تحويل الأرقام الهندية إلى أرقام إنجليزية")


def _op_parse_numbers(df, op):
    changed, examples, failed = 0, [], 0
    for col in op.target_columns:
        if col not in df.columns:
            continue
        old = _s(df, col).to_list()
        new = [parse_number(v) for v in old]
        for i, (o, nv) in enumerate(zip(old, new)):
            # قيمة مكتوبة تعذّر تحويلها («غير محدد» في عمود كمية) تصير فارغة.
            # الصمت هنا خطر: المستخدم يرى ثقوباً في عموده ولا يعرف سببها،
            # والمجاميع تُحسب على أقل مما رفع. نعدّها ونعرضها في السجل.
            if nv is None and o is not None and str(o).strip():
                failed += 1
                if len(examples) < MAX_EXAMPLES:
                    examples.append({"row": i + 1, "column": col,
                                     "before": o, "after": "(فارغ)"})
            if o is None or nv is None:
                continue
            # لا نعدّ تحويل النوع تغييراً: "4" → 4.0 ليس تعديلاً على البيانات.
            # التغيير الحقيقي هو "1,250.00 ر.س" → 1250.0 (كان يتعذّر قراءته كرقم).
            try:
                float(str(o).strip())
                continue
            except ValueError:
                pass
            changed += 1
            if len(examples) < MAX_EXAMPLES:
                examples.append({"row": i + 1, "column": col, "before": o, "after": nv})
        df = df.with_columns(pl.Series(col, new, dtype=pl.Float64))
    note = f" ({count_ar(failed, 'قيمة تعذّر', 'قيمتان تعذّر', 'قيم تعذّر')} تحويلها وتُركت فارغة)" if failed else ""
    return df, OperationResult(
        operation_id=op.id, operation_type=op.type, cells_changed=changed,
        columns_affected=op.target_columns, examples=examples,
        summary_ar=f"تحويل إلى أرقام حقيقية: {count_ar(changed, 'خلية', 'خليتان', 'خلايا')}{note}",
    )


def _op_parse_dates(df, op):
    changed, examples, failed = 0, [], 0
    for col in op.target_columns:
        if col not in df.columns:
            continue
        old = _s(df, col).to_list()
        new = []
        for v in old:
            parts = parse_date_parts(v) if v is not None else None
            if parts is None:
                new.append(None)
                if v is not None and str(v).strip():
                    failed += 1
            else:
                new.append(f"{parts[0]}-{parts[1]:02d}-{parts[2]:02d}")
        for i, (o, nv) in enumerate(zip(old, new)):
            if o != nv and nv is not None:
                changed += 1
                if len(examples) < MAX_EXAMPLES:
                    examples.append({"row": i + 1, "column": col, "before": o, "after": nv})
        df = df.with_columns(
            pl.Series(col, new, dtype=pl.Utf8).str.to_date("%Y-%m-%d", strict=False).alias(col)
        )
    note = f" ({count_ar(failed, 'قيمة تعذّر', 'قيمتان تعذّر', 'قيم تعذّر')} تحليلها)" if failed else ""
    return df, OperationResult(
        operation_id=op.id, operation_type=op.type, cells_changed=changed,
        columns_affected=op.target_columns, examples=examples,
        summary_ar=f"توحيد صيغة التاريخ: {count_ar(changed, 'خلية', 'خليتان', 'خلايا')}{note}",
    )


def _op_dedupe(df, op):
    before = df.height
    df2 = df.unique(keep="first", maintain_order=True)
    removed = before - df2.height
    return df2, OperationResult(
        operation_id=op.id, operation_type=op.type, rows_affected=removed,
        summary_ar=f"حذف {count_ar(removed, 'صف مكرر بالكامل', 'صفّين مكررين بالكامل', 'صفوف مكررة بالكامل')}",
    )


_RICH_CHARS = set("أإآةئؤ")


def _orthographic_richness(s: str) -> int:
    """عدد العلامات الإملائية العربية (همزات + تاء مربوطة).

    «متجر الأمانة» أغنى من «متجر الامانة» ⇒ الأصح للعرض.
    """
    return sum(1 for ch in s if ch in _RICH_CHARS)


def _pick_canonical(variants: list[str], counts: dict[str, int]) -> str:
    """الأكثر تكراراً يفوز، إلا إذا تقاربت الأعداد فيفوز الأصح إملائياً.

    بدون هذا، «شركة النور» تتحول إلى «شركه النور» لمجرد أن الخطأ تكرر أكثر.
    """
    # الأشكال هنا تُطبَّع إلى نفس النص، أي أنها نفس الكلمة بإملاء مختلف.
    # في العربية الشكل الأغنى هو الأصح دائماً: «مؤسسة» صحيحة و«مؤسسه» خطأ إملائي،
    # مهما تكرر الخطأ. لذلك الغنى أولاً والتكرار مرجّح ثانوي.
    return max(variants, key=lambda v: (_orthographic_richness(v), counts[v]))


_MERGE_KEY_STRIP = re.compile(r"[-_\s]+")


def _merge_key(v: str) -> str:
    """مفتاح تجميع أكثر تسامحاً من normalize_ar — للمطابقة فقط، لا للعرض.

    عيب حقيقي شُوهد على ملفات مستخدمين: "Panadol Extra 500mg" و"PANADOL EXTRA
    500 MG" و"Panadol-Extra 500mg" هي نفس المنتج فعلياً (نفس رمز الصنف SKU)،
    لكن normalize_ar وحده ما يوحّدها لأنها تختلف بمكان مسافة أو بوجود شرطة فقط.
    نشيل كل الشرطات والمسافات من مفتاح المطابقة (مش من النص المعروض) لأن هالفروق
    شكل كتابة غالباً، مو منتجاً مختلفاً — وهذا لا يزال أضيق بكثير من مطابقة
    تقريبية حقيقية (Levenshtein)، فما بيدمج منتجات مختلفة فعلاً بالغلط.
    """
    return _MERGE_KEY_STRIP.sub("", normalize_ar(v))


def _op_fuzzy_merge(df, op):
    """يوحّد الأسماء المتشابهة: «شركه النور» ← «شركة النور».

    يختار الشكل الأكثر تكراراً كممثّل للمجموعة (الأغلبية أصح إحصائياً).
    """
    changed, examples, groups = 0, [], 0
    for col in op.target_columns:
        if col not in df.columns:
            continue
        vals = _s(df, col).to_list()
        counts: dict[str, int] = {}
        for v in vals:
            if v:
                counts[v] = counts.get(v, 0) + 1

        # تجميع حسب مفتاح مطابقة متسامح (بلا شرطات/مسافات)
        buckets: dict[str, list[str]] = {}
        for v in counts:
            buckets.setdefault(_merge_key(v), []).append(v)

        mapping: dict[str, str] = {}
        for variants in buckets.values():
            if len(variants) > 1:
                canonical = _pick_canonical(variants, counts)
                groups += 1
                for v in variants:
                    if v != canonical:
                        mapping[v] = canonical

        if mapping:
            new = [mapping.get(v, v) for v in vals]
            for i, (o, nv) in enumerate(zip(vals, new)):
                if o != nv:
                    changed += 1
                    if len(examples) < MAX_EXAMPLES:
                        examples.append({"row": i + 1, "column": col, "before": o, "after": nv})
            df = df.with_columns(pl.Series(col, new, dtype=pl.Utf8))

    return df, OperationResult(
        operation_id=op.id, operation_type=op.type, cells_changed=changed,
        columns_affected=op.target_columns, examples=examples,
        summary_ar=(f"توحيد {count_ar(changed, 'اسم متشابه', 'اسمين متشابهين', 'أسماء متشابهة')}"
                  f" ضمن {count_ar(groups, 'مجموعة', 'مجموعتين', 'مجموعات')}"),
    )


def _op_flag_outliers(df, op):
    """وسم فقط — عمود جديد `_شاذ_<العمود>`. لا حذف إطلاقاً."""
    flagged = 0
    for col in op.target_columns:
        if col not in df.columns:
            continue
        nums = [parse_number(v) for v in _s(df, col).to_list()]
        clean = [x for x in nums if x is not None]
        if len(clean) < 10:
            continue
        s = pl.Series(clean)
        q1, q3 = float(s.quantile(0.25)), float(s.quantile(0.75))
        iqr = q3 - q1
        if iqr <= 0:
            continue
        lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
        flags = [bool(x is not None and (x < lo or x > hi)) for x in nums]
        flagged += sum(flags)
        df = df.with_columns(pl.Series(f"{OUTLIER_PREFIX}{col}", flags, dtype=pl.Boolean))
    return df, OperationResult(
        operation_id=op.id, operation_type=op.type, rows_affected=flagged,
        columns_affected=op.target_columns,
        summary_ar=f"وسم {count_ar(flagged, 'قيمة شاذة', 'قيمتين شاذتين', 'قيم شاذة')} (لم تُحذف — القرار لك)",
    )


_HANDLERS = {
    "trim_whitespace": _op_trim,
    "normalize_arabic": _op_normalize_arabic,
    "normalize_digits": _op_normalize_digits,
    "parse_numbers": _op_parse_numbers,
    "parse_dates": _op_parse_dates,
    "deduplicate_rows": _op_dedupe,
    "fuzzy_entity_merge": _op_fuzzy_merge,
    "flag_outliers": _op_flag_outliers,
}


def apply_disabled(recipe: CleaningRecipe, disabled_keys: list[str] | None) -> CleaningRecipe:
    """يطفئ العمليات التي رفضها المستخدم.

    التنظيف اقتراح لا حكم: المستخدم قد يعرف أن «شركة النور» و«شركه النور»
    عميلان مختلفان فعلاً. المفتاح ثابت (النوع + الأعمدة) فلا ينتقل التعطيل
    إلى عملية أخرى عند إعادة المعالجة.
    """
    if not disabled_keys:
        return recipe
    off = set(disabled_keys)
    return CleaningRecipe(
        operations=[op.model_copy(update={"enabled": op.enabled and op.key not in off})
                    for op in recipe.operations],
        version=recipe.version,
    )


def outlier_flag_map(columns: list[str]) -> dict[str, str]:
    """{اسم العمود الأصلي: اسم عمود الوسم} لكل وسم موجود فعلاً في البيانات.

    تستعملها الواجهة لتمييز الخلية الشاذة بلونٍ بدل عرض عمود منطقي إضافي
    اسمه داخلي — الخطة §8.5 تطلب التمييز اللوني لا أعمدة تقنية.
    """
    present = set(columns)
    return {c[len(OUTLIER_PREFIX):]: c
            for c in columns
            if c.startswith(OUTLIER_PREFIX) and c[len(OUTLIER_PREFIX):] in present}
