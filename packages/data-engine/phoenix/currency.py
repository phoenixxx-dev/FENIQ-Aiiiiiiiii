"""العملة — تُكتشف بدليل أو لا تُكتب أصلاً.

لماذا يستحق هذا ملفاً كاملاً؟ لأن الخطأ فيه **صامت وخطير معاً**. كان المحرك
يكتب «ر.س» على كل مبلغ لأنها كانت القيمة الافتراضية في دالة التنسيق. على ملف
من مستودع في اللاذقية يعني ذلك أن كل رقم على الشاشة يحمل عملة بلد آخر — والرقم
صحيح، والعملة كذبة، والمستخدم لا يرى فرقاً حتى يقرأ العملة.

وفي سوريا تحديداً هناك خطر أكبر: أكثر الناس يتعاملون بالدولار **وبالليرة معاً**.
ملف فيه العملتان وجمعناه في رقم واحد يعطي مجموعاً بلا معنى — «120,000 + 45,000,000»
رقمٌ لا يقابل شيئاً في الواقع، ويبدو سليماً تماماً. لذلك:

  1. لا رمز عملة بلا دليل. الرقم بلا رمز صادقٌ؛ الرقم برمز خاطئ كذبة.
  2. لا جمع بين عملتين أبداً. الملف متعدّد العملات يُجمع **لكل عملة على حدة**.
  3. لا تحويل بسعر صرف من عندنا. سعر الصرف قرار المالك لا اجتهاد المحرك.
"""
from __future__ import annotations

import re
from typing import NamedTuple

from .arabic_text import normalize_ar
from .models import DatasetCurrency

# ---------------------------------------------------------------- السجل

class CurrencySpec(NamedTuple):
    code: str
    symbol_ar: str
    aliases: tuple[str, ...]


# الترتيب لا يهم — المطابقة تأخذ **أطول** مرادف مطابق دائماً، حتى لا تبتلع
# «ليرة» كلمةَ «ليرة تركية».
CURRENCIES: tuple[CurrencySpec, ...] = (
    CurrencySpec("SYP", "ل.س", ("ل.س", "ليرة سورية", "ليرة سوري", "سورية", "سوري", "syp")),
    CurrencySpec("USD", "$", ("$", "us$", "usd", "دولار", "دولار امريكي",
                              "دولار أمريكي", "د.أمريكي")),
    CurrencySpec("EUR", "€", ("€", "eur", "يورو", "اورو")),
    CurrencySpec("TRY", "ل.ت", ("₺", "try", "ليرة تركية", "ليرة تركي", "تركية")),
    CurrencySpec("LBP", "ل.ل", ("ل.ل", "lbp", "ليرة لبنانية", "لبنانية")),
    CurrencySpec("SAR", "ر.س", ("ر.س", "sar", "ريال سعودي", "ريال")),
    CurrencySpec("AED", "د.إ", ("د.إ", "aed", "درهم اماراتي", "درهم إماراتي", "درهم")),
    CurrencySpec("JOD", "د.أ", ("د.ا", "jod", "دينار اردني", "دينار أردني")),
    CurrencySpec("IQD", "د.ع", ("د.ع", "iqd", "دينار عراقي")),
    CurrencySpec("KWD", "د.ك", ("د.ك", "kwd", "دينار كويتي")),
    CurrencySpec("EGP", "ج.م", ("ج.م", "egp", "جنيه مصري", "جنيه")),
    CurrencySpec("TND", "د.ت", ("د.ت", "tnd", "دينار تونسي")),
)

# كلمات تحتمل أكثر من عملة ولا يحسمها سياق. لا نخمّن: نتركها بلا قرار.
# «دينار» وحدها أردني أو عراقي أو كويتي — والفرق بينها مئات الأضعاف.
AMBIGUOUS = {"دينار", "dinar", "pound", "lira", "riyal", "dirham"}

# «ليرة» بلا بلد: سورية أو تركية أو لبنانية. الصيغ المؤهَّلة («ليرة تركية»)
# تُلتقط قبلها لأنها أطول، فما يبقى مجرّداً هو ليرة السوق الأساسي. هذا افتراض
# صريح لا صدفة في ترتيب قاموس — وتغييره سطر واحد لو انتقل المنتج لسوق آخر.
HOME_CURRENCY = "SYP"
BARE_HOME_WORDS = {"ليره", "ليرة", "لبره"}

_BY_CODE = {c.code: c for c in CURRENCIES}

# كل المرادفات مرتّبة بالطول تنازلياً: «ليرة تركية» قبل «ليرة».
_ALIASES: list[tuple[str, str]] = sorted(
    ((normalize_ar(a), c.code) for c in CURRENCIES for a in c.aliases),
    key=lambda p: -len(p[0]),
)

# رموز لا تُطبَّع كحروف عربية، فتُفحص كما هي
_SYMBOLS = {"$": "USD", "€": "EUR", "₺": "TRY", "£": None}


# سوابق عربية تلتصق بالكلمة: «بالدولار» = بـ + الـ + دولار. بلا نزعها لا
# يطابق أي قاموس اسمَ عمود عربياً طبيعياً مثل «السعر بالدولار».
_CLITICS = ("وبال", "فبال", "وال", "بال", "كال", "فال", "لل", "ال",
            "و", "ب", "ل", "ك", "ف")


def _strip_clitics(word: str) -> str:
    for c in _CLITICS:
        if word.startswith(c) and len(word) - len(c) >= 3:
            return word[len(c):]
    return word


def symbol_of(code: str | None) -> str | None:
    spec = _BY_CODE.get(code or "")
    return spec.symbol_ar if spec else None


def detect_in_text(text: str) -> str | None:
    """يُرجع رمز العملة (SYP/USD…) الموجود داخل نص، أو None.

    يتجاهل الملتبس عمداً: «دينار» بلا بلد قرارٌ لا نملك دليله.
    """
    if not text:
        return None
    for sym, code in _SYMBOLS.items():
        if sym in text and code:
            return code
    norm = normalize_ar(text)
    if not norm:
        return None
    raw_words = [w for w in re.split(r"[^\w؀-ۿ]+", norm) if w]
    # نجرّب الكلمة كما هي **و** بعد نزع السابقة: نزعُ حرف واحد يشوّه كلمات
    # قصيرة («ليرة» ← «يرة»)، والإبقاء وحده يُفوّت «بالدولار». فنحتفظ بالاثنتين.
    words = set(raw_words) | {_strip_clitics(w) for w in raw_words}
    stripped = " ".join(_strip_clitics(w) for w in raw_words)
    for alias, code in _ALIASES:
        if alias in AMBIGUOUS:
            # «دينار» أو «ليرة» وحدها لا تحسم — نتجاوزها ونكمل البحث عن أطول
            # مرادف يذكر البلد. لو لم يوجد، خرجنا بلا قرار، وهو المطلوب.
            continue
        if "." in alias:
            # اختصار كـ«ل.س» — النقطة تكسر تقسيم الكلمات، فيُفحص كنص مباشر
            if alias in norm:
                return code
        elif " " in alias:
            if alias in stripped or alias in norm:
                return code
        elif alias in words:
            return code

    # آخر ما يُجرَّب: «ليرة» مجرّدة ⇒ ليرة السوق الأساسي (انظر HOME_CURRENCY)
    if words & {normalize_ar(w) for w in BARE_HOME_WORDS}:
        return HOME_CURRENCY
    return None


# ------------------------------------------------------------- النتيجة
# النموذج نفسه يعيش في models.py حتى يخرج مع الـschema إلى الـAPI والواجهة
# بلا نسخة ثانية تتباعد عنه.

def _codes_in_series(values: list) -> dict[str, int]:
    counts: dict[str, int] = {}
    for v in values:
        if v is None:
            continue
        code = detect_in_text(str(v))
        if code:
            counts[code] = counts.get(code, 0) + 1
    return counts


def _annotate_columns(df, schema, sample: int) -> dict[str, str]:
    """يعطي كل عمود مالي عملته الخاصة، ويُرجع {اسم العمود: رمز العملة}.

    قائمة أسعار بعمودين — «السعر بالدولار» و«السعر بالليرة» — شائعة جداً في
    سوق يتعامل بالعملتين. لكل عمود عملته، ولا معنى لجمعهما.
    """
    found: dict[str, str] = {}
    for sc in schema.columns:
        if sc.unit != "currency" or sc.column_name not in df.columns:
            continue
        vals = df[sc.column_name].cast(str, strict=False).head(sample).to_list()
        counts = _codes_in_series(vals)
        code = max(counts, key=counts.get) if counts else detect_in_text(sc.column_name)
        if code:
            sc.currency = code
            found[sc.column_name] = code
    return found


def detect(df, profile, schema, *, sample: int = 400) -> DatasetCurrency:
    """يستنتج عملة الملف من الأدلّة المتاحة، بالترتيب:
        عمود عملة صريح (لكل صف) ⇒ رموز داخل قيم الأعمدة المالية ⇒ أسماء الأعمدة.
    وإن لم يوجد دليل: لا عملة، ولا رمز على أي رقم.

    لا تأخذ الدالةُ أي افتراض جغرافي. «المنتج عربي إذن العملة ريال» هو بالضبط
    نوع الاستنتاج الذي أنتج العيب.
    """
    per_column = _annotate_columns(df, schema, sample)

    # 1) عمود عملة صريح — الأقوى، وهو وحده ما يكشف اختلاطاً **داخل الصفوف**
    cur_col = schema.by_concept("currency")
    if cur_col is not None and cur_col.column_name in df.columns:
        raw = df[cur_col.column_name].cast(str, strict=False).to_list()
        counts = _codes_in_series(raw)
        codes = sorted(counts, key=lambda c: -counts[c])
        if codes:
            if len(codes) > 1:
                pretty = "، ".join(symbol_of(c) or c for c in codes)
                return DatasetCurrency(
                    source="column", column=cur_col.column_name, codes=codes,
                    evidence_ar=(f"عمود «{cur_col.column_name}» يحمل أكثر من عملة ({pretty}) "
                                 "— لذلك لا يُجمع المبلغ في رقم واحد، بل لكل عملة مجموعها."),
                )
            return DatasetCurrency(
                code=codes[0], symbol_ar=symbol_of(codes[0]), source="column",
                column=cur_col.column_name, codes=codes,
                evidence_ar=f"عمود «{cur_col.column_name}» يذكر العملة صراحةً في كل صف.",
            )

    # 2) الأعمدة المالية نفسها — قيمها أو أسماؤها
    if per_column:
        codes = sorted(set(per_column.values()))
        if len(codes) > 1:
            pretty = "، ".join(f"«{n}» ({symbol_of(c) or c})" for n, c in per_column.items())
            return DatasetCurrency(
                source="column_name", codes=codes,
                evidence_ar=(f"أعمدة مالية بعملات مختلفة: {pretty} — كل عمود بعملته، "
                             "ولا يُجمعان في رقم واحد."),
            )
        name = next(iter(per_column))
        return DatasetCurrency(
            code=codes[0], symbol_ar=symbol_of(codes[0]),
            source="values" if _codes_in_series(
                df[name].cast(str, strict=False).head(sample).to_list()) else "column_name",
            codes=codes,
            evidence_ar=f"العملة مذكورة في «{name}».",
        )

    return DatasetCurrency(
        evidence_ar=("لا دليل على العملة في هذا الملف — تُعرض المبالغ بلا رمز. "
                     "حدّد العملة إن أردت ظهورها."),
    )


def available_currencies() -> list[dict]:
    """قائمة العملات التي يعرفها المحرك — تغذّي قائمة الاختيار في الواجهة.
    مصدرها السجل نفسه، فلا تتباعد الواجهة عن المحرك."""
    return [{"code": c.code, "symbol_ar": c.symbol_ar} for c in CURRENCIES]


class UnknownCurrencyError(ValueError):
    """رمز عملة غير معروف — نرفضه بدل تخزين رمز لا يفهمه المحرك."""


def apply_override(schema, code: str | None):
    """عملةٌ يحدّدها المستخدم تعلو على أي استنتاج — الخطة §5.4.

    وهي المخرج الوحيد من حالة «لا دليل»: بدلها كان المستخدم عالقاً بين رقم بلا
    عملة وعملةٍ نخترعها له. ونصرّح بمصدرها ("user") حتى يبقى الفرق ظاهراً بين
    ما عرفناه من الملف وما قاله هو.
    """
    if not code:
        return schema
    if code not in _BY_CODE:
        raise UnknownCurrencyError(code)
    schema.currency = DatasetCurrency(
        code=code, symbol_ar=symbol_of(code), source="user", codes=[code],
        evidence_ar=f"العملة محدَّدة من المستخدم ({symbol_of(code)})، لا مستنتجة من الملف.",
    )
    for c in schema.columns:
        if c.unit == "currency":
            c.currency = code
    return schema
