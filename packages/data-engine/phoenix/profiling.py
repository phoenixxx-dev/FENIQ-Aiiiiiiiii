"""B2 — التوصيف: استنتاج النوع + الإحصاء + كشف مشاكل الجودة."""
from __future__ import annotations

import polars as pl

from .arabic_text import (count_ar, has_indic_digits, is_blank, is_hijri_like,
                          parse_date_parts, parse_number)
from .models import ColumnProfile, DatasetProfile, QualityIssue

TYPE_ACCEPT_THRESHOLD = 0.95     # النوع يُقبل إذا نجح على 95% من القيم غير الفارغة
CATEGORICAL_MAX_UNIQUE = 50
CATEGORICAL_MAX_RATIO = 0.05

# سقف القيم المفحوصة لاستنتاج النوع.
#
# القياس: استنتاج النوع كان يستدعي parse_number على 3.9 مليون قيمة و
# parse_date_parts على 2.4 مليون — 20 ثانية من أصل 27 لمعالجة 300 ألف صف،
# أي ثلاثة أرباع الانتظار في خطوة واحدة.
#
# نسبة 95% تُقدَّر بثقة تامة على عيّنة بهذا الحجم. ونأخذ من **الرأس والذيل**
# معاً لا من الرأس وحده: الملفات العربية الحقيقية كثيراً ما تكون نظيفة في
# أولها وتحمل صفوف مجاميع أو ملاحظات في آخرها.
INFER_SAMPLE_MAX = 20_000


# تحت هذا العدد من الصفوف، تساوي كل القيم صدفةٌ واردة لا تصميم — فلا نسمّي
# العمود «ثابتاً». يستعمل نفس العتبة الفهمُ الدلالي (semantic.constant_columns)
# حتى لا يكون للثبات تعريفان في محرك واحد.
CONSTANT_MIN_ROWS = 10


def _as_str_series(df: pl.DataFrame, col: str) -> pl.Series:
    return df[col].cast(pl.Utf8, strict=False)


def _loses_leading_zero(vals: list[str]) -> bool:
    """هل بين القيم رقم صحيح موجب مكتوب بصفر بادئ (زي "001")؟

    لو حوّلناها لرقم (1) وبعدين رجّعناها نص ("1")، ما بترجع نفس الأصل — يعني في
    معلومة (طول الرمز/الصفر البادئ) رح تنضاع لو عاملناها كرقم.
    """
    for v in vals:
        s = str(v).strip()
        if s.isdigit() and len(s) > 1 and s[0] == "0":
            return True
    return False


def _sample(vals: list, cap: int = INFER_SAMPLE_MAX) -> list:
    """رأس وذيل — لا رأس وحده (انظر INFER_SAMPLE_MAX)."""
    if len(vals) <= cap:
        return vals
    half = cap // 2
    return vals[:half] + vals[-half:]


def infer_type(values: list[str], dtype: pl.DataType | None = None) -> tuple[str, float]:
    """يُرجع (النوع, درجة الثقة). الترتيب: bool → int → float → date → categorical → text.

    dtype: نوع العمود كما قرأه Polars. حين يكون رقماً أو تاريخاً فالجواب
    معروف يقيناً — تحليل ثلاثمئة ألف نص لإعادة اكتشافه هدر خالص.
    """
    vals_all = [v for v in values if not is_blank(v)]
    if not vals_all:
        return "empty", 1.0

    if dtype is not None:
        if dtype in (pl.Date, pl.Datetime):
            return "date", 1.0
        if dtype == pl.Boolean:
            return "boolean", 1.0
        if dtype.is_integer():
            return ("identifier", 0.9) if _loses_leading_zero(_sample(vals_all)) \
                else ("integer", 1.0)
        if dtype.is_float():
            ints = all(float(v).is_integer() for v in _sample(vals_all)
                       if _is_num(v))
            return ("integer" if ints else "float"), 1.0

    vals = _sample(vals_all)
    n = len(vals)

    bools = {"true", "false", "نعم", "لا", "0", "1", "yes", "no", "صح", "خطأ"}
    if sum(1 for v in vals if str(v).strip().lower() in bools) / n >= TYPE_ACCEPT_THRESHOLD:
        uniq = {str(v).strip().lower() for v in vals}
        if len(uniq) <= 3:
            return "boolean", 1.0

    nums = [parse_number(v) for v in vals]
    num_ok = sum(1 for x in nums if x is not None)
    num_ratio = num_ok / n

    dates_ok = sum(1 for v in vals if parse_date_parts(v) is not None)
    date_ratio = dates_ok / n

    # التاريخ له أولوية على الرقم: "2026-01-24" قد يُقرأ كنص رقمي
    if date_ratio >= TYPE_ACCEPT_THRESHOLD:
        return "date", date_ratio
    if num_ratio >= TYPE_ACCEPT_THRESHOLD:
        ints = all(x is not None and float(x).is_integer() for x in nums if x is not None)
        # معرّفات رقمية الشكل لكنها نصية بالمعنى: "001" لو تحوّلت لرقم بتفقد الصفر البادئ
        # (رمز صنف، رمز بريدي...). التحويل لرقم هون فيه فقدان معلومة حقيقي، فمعاملتها
        # كمعرّف (نص) أدق من معاملتها كرقم، بغض النظر عن عدد الصفوف.
        if ints and _loses_leading_zero(vals):
            return "identifier", 0.9
        return ("integer" if ints else "float"), num_ratio

    uniq = len({str(v).strip() for v in vals})
    if uniq == n and n > 10:
        return "identifier", 0.9
    if uniq <= CATEGORICAL_MAX_UNIQUE or uniq / n <= CATEGORICAL_MAX_RATIO:
        return "categorical", 0.85
    if max(num_ratio, date_ratio) > 0.3:
        return "mixed", 1 - max(num_ratio, date_ratio)
    return "text", 0.8


def profile_column(df: pl.DataFrame, col: str, position: int) -> ColumnProfile:
    s = _as_str_series(df, col)
    raw = s.to_list()
    total = len(raw)
    non_null = [v for v in raw if not is_blank(v)]
    null_count = total - len(non_null)

    ctype, conf = infer_type(raw, dtype=df[col].dtype)
    uniq_vals = {str(v).strip() for v in non_null}
    unique_count = len(uniq_vals)

    p = ColumnProfile(
        name=col, position=position, inferred_type=ctype, type_confidence=round(conf, 3),
        total_count=total, null_count=null_count,
        null_pct=round(null_count / total * 100, 2) if total else 0.0,
        unique_count=unique_count,
        unique_pct=round(unique_count / len(non_null) * 100, 2) if non_null else 0.0,
        sample_values=[str(v) for v in non_null[:10]],
    )

    if ctype in ("integer", "float"):
        nums = [x for x in (parse_number(v) for v in non_null) if x is not None]
        if nums:
            ns = pl.Series(nums)
            p.min, p.max = float(ns.min()), float(ns.max())
            p.mean, p.median = float(ns.mean()), float(ns.median())
            p.std = float(ns.std()) if len(nums) > 1 else 0.0
            p.p25, p.p75 = float(ns.quantile(0.25)), float(ns.quantile(0.75))
            p.zero_count = sum(1 for x in nums if x == 0)
            p.negative_count = sum(1 for x in nums if x < 0)

    elif ctype == "date":
        parts = [parse_date_parts(v) for v in non_null]
        ok = [x for x in parts if x]
        if ok:
            lo, hi = min(ok), max(ok)
            p.min_date = f"{lo[0]}-{lo[1]:02d}-{lo[2]:02d}"
            p.max_date = f"{hi[0]}-{hi[1]:02d}-{hi[2]:02d}"
            span = (hi[0] - lo[0]) * 365 + (hi[1] - lo[1]) * 30
            p.granularity = "day" if span < 90 else ("month" if span < 1100 else "year")

    if ctype in ("categorical", "text", "identifier", "boolean"):
        counts = {}
        for v in non_null:
            k = str(v).strip()
            counts[k] = counts.get(k, 0) + 1
        p.top_values = sorted(counts.items(), key=lambda x: -x[1])[:20]
        p.avg_length = round(sum(len(str(v)) for v in non_null) / len(non_null), 1) if non_null else None

    p.quality_issues = _column_issues(p, non_null, total)
    return p


def _column_issues(p: ColumnProfile, non_null: list, total: int) -> list[QualityIssue]:
    issues: list[QualityIssue] = []

    if p.null_pct > 80:
        issues.append(QualityIssue(kind="sparse_column", severity="warning", column=p.name,
                                   affected_count=p.null_count, affected_pct=p.null_pct,
                                   message_ar=f"العمود «{p.name}» فارغ بنسبة {p.null_pct:.0f}%"))
    elif p.null_pct > 5:
        issues.append(QualityIssue(kind="missing_values", severity="warning", column=p.name,
                                   affected_count=p.null_count, affected_pct=p.null_pct,
                                   message_ar=(
                                       f"{count_ar(p.null_count, 'قيمة مفقودة', 'قيمتان مفقودتان', 'قيم مفقودة')}"
                                       f" في «{p.name}» ({p.null_pct:.1f}%)")))

    if p.unique_count == 1 and total > CONSTANT_MIN_ROWS:
        issues.append(QualityIssue(kind="constant_column", severity="info", column=p.name,
                                   affected_count=total, affected_pct=100.0,
                                   message_ar=f"العمود «{p.name}» قيمته ثابتة ولا يضيف معلومة"))

    ws = sum(1 for v in non_null if isinstance(v, str) and v != v.strip())
    if ws:
        issues.append(QualityIssue(kind="whitespace", severity="info", column=p.name,
                                   affected_count=ws, affected_pct=round(ws / total * 100, 2),
                                   message_ar=(
                                       f"{count_ar(ws, 'قيمة', 'قيمتان', 'قيم')}"
                                       f" في «{p.name}» تحتوي مسافات زائدة")))

    indic = sum(1 for v in non_null if isinstance(v, str) and has_indic_digits(v))
    if indic:
        issues.append(QualityIssue(kind="arabic_indic_digits", severity="warning", column=p.name,
                                   affected_count=indic, affected_pct=round(indic / total * 100, 2),
                                   message_ar=(
                                       f"{count_ar(indic, 'قيمة', 'قيمتان', 'قيم')}"
                                       f" في «{p.name}» مكتوبة بأرقام هندية"
                                       " وسيتم تحويلها إلى أرقام إنجليزية")))

    if p.inferred_type == "date":
        hij = sum(1 for v in non_null[:200] if is_hijri_like(v))
        if hij > len(non_null[:200]) * 0.3:
            issues.append(QualityIssue(kind="mixed_types", severity="warning", column=p.name,
                                       affected_count=hij, affected_pct=0.0,
                                       message_ar=f"«{p.name}» يبدو أنه يحتوي تواريخ هجرية"))

    if p.inferred_type == "mixed":
        issues.append(QualityIssue(kind="mixed_types", severity="warning", column=p.name,
                                   affected_count=total, affected_pct=100.0,
                                   message_ar=f"«{p.name}» يخلط بين أنواع بيانات مختلفة"))

    if p.negative_count:
        issues.append(QualityIssue(kind="negative_values", severity="info", column=p.name,
                                   affected_count=p.negative_count,
                                   affected_pct=round(p.negative_count / total * 100, 2),
                                   message_ar=(
                                       f"{count_ar(p.negative_count, 'قيمة سالبة', 'قيمتان سالبتان', 'قيم سالبة')}"
                                       f" في «{p.name}»")))

    # الشذوذ: IQR — الوسم فقط، لا حذف (قاعدة صارمة)
    if p.inferred_type in ("integer", "float") and p.p25 is not None and p.p75 is not None:
        iqr = p.p75 - p.p25
        if iqr > 0:
            lo, hi = p.p25 - 1.5 * iqr, p.p75 + 1.5 * iqr
            nums = [parse_number(v) for v in non_null]
            out = sum(1 for x in nums if x is not None and (x < lo or x > hi))
            if out and out / total < 0.15:
                issues.append(QualityIssue(kind="outliers", severity="info", column=p.name,
                                           affected_count=out, affected_pct=round(out / total * 100, 2),
                                           message_ar=(
                                               f"{count_ar(out, 'قيمة شاذة', 'قيمتان شاذتان', 'قيم شاذة')}"
                                               f" في «{p.name}» (موسومة فقط — لم تُحذف)")))
    return issues


def profile_dataset(df: pl.DataFrame) -> DatasetProfile:
    cols = [profile_column(df, c, i) for i, c in enumerate(df.columns)]
    dups = df.height - df.unique().height

    ds_issues: list[QualityIssue] = []
    if dups:
        ds_issues.append(QualityIssue(kind="duplicate_rows", severity="warning",
                                      affected_count=dups, affected_pct=round(dups / df.height * 100, 2),
                                      message_ar=count_ar(
                                          dups, "صف مكرر بالكامل",
                                          "صفّان مكرران بالكامل",
                                          "صفوف مكررة بالكامل")))

    return DatasetProfile(
        row_count=df.height, column_count=df.width, duplicate_row_count=dups,
        memory_mb=round(df.estimated_size("mb"), 2),
        columns=cols, dataset_issues=ds_issues,
    )


def _is_num(v) -> bool:
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False
