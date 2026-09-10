"""معالجة النص العربي — بدون هذه الوحدة كل الطبقات فوقها تنهار.

سياسة الأرقام: المدخلات متسامحة (تقبل ١٢٣)، المخرجات دائماً 0-9.
"""
from __future__ import annotations

import re
import unicodedata

# ---------------------------------------------------------- خرائط الأرقام
ARABIC_INDIC = "٠١٢٣٤٥٦٧٨٩"      # U+0660..U+0669
EXTENDED_INDIC = "۰۱۲۳۴۵۶۷۸۹"    # U+06F0..U+06F9 (فارسي/أردو)
WESTERN = "0123456789"

_DIGIT_MAP = {ord(a): w for a, w in zip(ARABIC_INDIC, WESTERN)}
_DIGIT_MAP.update({ord(a): w for a, w in zip(EXTENDED_INDIC, WESTERN)})
_DIGIT_MAP[ord("٫")] = "."   # الفاصلة العشرية العربية
_DIGIT_MAP[ord("٬")] = ","   # فاصل الآلاف العربي
_DIGIT_MAP[ord("،")] = ","   # الفاصلة العربية

INDIC_DIGIT_RE = re.compile(r"[\u0660-\u0669\u06F0-\u06F9]")

# ---------------------------------------------------------- التطبيع
_TATWEEL = "\u0640"
_DIACRITICS = re.compile(r"[\u064B-\u0652\u0670\u0653-\u0655]")
_ALEF = re.compile(r"[\u0622\u0623\u0625\u0671]")      # آ أ إ ٱ
_YAA = re.compile(r"[\u0649\u06CC]")                    # ى ی
_WAW = re.compile(r"[\u0624]")                          # ؤ
_HAMZA = re.compile(r"[\u0626]")                        # ئ
_SPACES = re.compile(r"\s+")

# محارف لا تُرى ولا تُحسب فراغاً: مسافة صفرية العرض، واصل/فاصل صفري،
# علامات اتجاه (LRM/RLM/ALM/عوازل الاتجاه)، وBOM.
#
# لماذا تُحذف: ملفات إكسل العربية الحقيقية مليئة بها (نسخ من الويب، برامج
# محاسبة قديمة، أدوات تضيف علامة اتجاه تلقائياً). أثرها أنّ «شركة النور»
# و«شركة‏ النور» تبدوان **متطابقتين على الشاشة** وتُحسبان كيانين: مجموعان
# لعميل واحد، بلا رسالة ولا مؤشّر. `\s` لا يلتقطها وNFKC لا يحذفها.
_INVISIBLE_CHARS = "​‌‍‎‏؜⁦⁧⁨⁩﻿"
_INVISIBLE = re.compile(f"[{_INVISIBLE_CHARS}]")

CURRENCY_TOKENS = [
    "ر.س", "ريال", "ر.ي", "د.إ", "درهم", "د.ك", "دينار", "ل.س", "ليرة", "ج.م", "جنيه",
    "SAR", "AED", "KWD", "USD", "EGP", "SYP", "$", "€", "£",
]


# رموز الفراغ الشائعة في التقارير العربية والمالية.
# بدونها يُصنَّف عمود أسعار فيه "—" كنص لا كرقم، فتنهار كل التحليلات فوقه.
NULL_TOKENS = {
    "", "-", "--", "—", "–", "...", "…", "/", "\\",
    "n/a", "na", "n.a.", "null", "none", "nil", "#n/a", "#null!", "nan",
    "لا يوجد", "غير متوفر", "غير محدد", "لايوجد", "بدون", "فارغ", "صفر ",
}


def is_blank(v) -> bool:
    """هل القيمة فراغ فعلي (بما فيه رموز الفراغ الشائعة)؟"""
    if v is None:
        return True
    s = str(v).strip()
    return s == "" or s.lower() in NULL_TOKENS


def to_western_digits(s: str) -> str:
    """١٢٣ → 123 . الاتجاه الوحيد المسموح. لا عودة أبداً."""
    return s.translate(_DIGIT_MAP)


def has_indic_digits(s: str) -> bool:
    return bool(INDIC_DIGIT_RE.search(s))


def strip_diacritics(s: str) -> str:
    return _DIACRITICS.sub("", s)


def normalize_ar(s: str) -> str:
    """التطبيع الكامل — يُستخدم للمطابقة والبحث فقط، لا للعرض."""
    if not isinstance(s, str):
        return ""
    s = unicodedata.normalize("NFKC", s)
    s = _INVISIBLE.sub("", s)
    s = s.replace(_TATWEEL, "")
    s = strip_diacritics(s)
    s = _ALEF.sub("\u0627", s)          # → ا
    s = _YAA.sub("\u064A", s)           # → ي
    s = _WAW.sub("\u0648", s)           # → و
    s = _HAMZA.sub("\u064A", s)         # → ي
    s = s.replace("\u0629", "\u0647")   # ة → ه (للمطابقة فقط)
    s = to_western_digits(s)
    s = _SPACES.sub(" ", s).strip().lower()
    return s


def normalize_for_display(s: str) -> str:
    """تنظيف خفيف للعرض: يحافظ على الهمزات والتاء المربوطة."""
    if not isinstance(s, str):
        return s
    s = unicodedata.normalize("NFKC", s)
    # تُحذف حتى في العرض: المستخدم لا يراها أصلاً، ووجودها يجعل قيمتين
    # متطابقتين بعينه مختلفتين في كل تجميع وفرز وبحث
    s = _INVISIBLE.sub("", s)
    s = s.replace(_TATWEEL, "")
    s = to_western_digits(s)
    return _SPACES.sub(" ", s).strip()


# ---------------------------------------------------------- تمييز العدد

def count_ar(n: int | float, one: str, two: str, few: str) -> str:
    """صيغة المعدود الصحيحة بالعربية.

    العربية ليست كالإنجليزية (مفرد/جمع فقط): المعدود يتغيّر أربع مرات —
    مفرد، مثنى، جمع (3–10)، ثم **مفرد مجدَّداً** من 11 فصاعداً.

    كنّا نكتب المفرد دائماً: «حذف 3 صف مكرر»، «9 نقطة»، «5 فئة». الأرقام
    الكبيرة كانت تصحّ صدفةً (11+ مفرد فعلاً) فبدا النص سليماً، بينما كل
    عدد صغير — وهو الأكثر ظهوراً في الاكتشافات وسجل التنظيف — كان خطأً
    لغوياً ظاهراً لأي قارئ عربي.

        count_ar(1, "خلية", "خليتان", "خلايا")   → "خلية"
        count_ar(2, ...)                          → "خليتان"
        count_ar(9, ...)                          → "9 خلايا"
        count_ar(120, ...)                        → "120 خلية"
    """
    n = int(n)
    if n == 1:
        return one
    if n == 2:
        return two
    if 3 <= n <= 10:
        return f"{fmt_number(n)} {few}"
    return f"{fmt_number(n)} {one}"


def arabic_ratio(s: str) -> float:
    if not s:
        return 0.0
    ar = sum(1 for ch in s if "\u0600" <= ch <= "\u06FF")
    return ar / len(s)


# ---------------------------------------------------------- تحليل الأرقام
_NUM_CLEAN = re.compile(r"[^\d.\-]")


def parse_number(value) -> float | None:
    """'1,250.00 ر.س' → 1250.0 | '١٢' → 12.0 | '(500)' → -500.0"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        return None

    s = to_western_digits(s)
    negative = s.startswith("(") and s.endswith(")")   # صيغة محاسبية
    if negative:
        s = s[1:-1]
    if "%" in s:
        s = s.replace("%", "")

    for tok in CURRENCY_TOKENS:
        s = s.replace(tok, "")

    s = s.replace("\u200f", "").replace("\u200e", "").strip()

    # حماية حرجة: "INV-1151" ليس رقماً. أي حرف أبجدي متبقٍ بعد إزالة العملة ⇒ ليس رقماً.
    # بدون هذا الفحص تُصنَّف أعمدة أرقام الفواتير كمقاييس عددية.
    if any(ch.isalpha() for ch in s):
        return None

    s = _NUM_CLEAN.sub("", s)
    if s in ("", "-", ".", "-."):
        return None
    if s.count("-") > 1 or ("-" in s and not s.startswith("-")):
        return None      # "1151-2" أو "1-2-3" ليست أرقاماً
    try:
        n = float(s)
    except ValueError:
        return None
    return -n if negative else n


# ---------------------------------------------------------- تحليل التواريخ
_AR_MONTHS = {
    "كانون الثاني": 1, "يناير": 1, "شباط": 2, "فبراير": 2, "اذار": 3, "مارس": 3,
    "نيسان": 4, "ابريل": 4, "ايار": 5, "مايو": 5, "حزيران": 6, "يونيو": 6,
    "تموز": 7, "يوليو": 7, "اب": 8, "اغسطس": 8, "ايلول": 9, "سبتمبر": 9,
    "تشرين الاول": 10, "اكتوبر": 10, "تشرين الثاني": 11, "نوفمبر": 11,
    "كانون الاول": 12, "ديسمبر": 12,
}

_DATE_PATTERNS = [
    (re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$"), (1, 2, 3)),
    (re.compile(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})$"), (3, 2, 1)),
]


def parse_date_parts(value) -> tuple[int, int, int] | None:
    """يُرجع (سنة, شهر, يوم) ميلادية أو None."""
    if value is None:
        return None
    s = to_western_digits(str(value).strip())
    if not s:
        return None

    for pat, (yi, mi, di) in _DATE_PATTERNS:
        m = pat.match(s)
        if m:
            y, mo, d = int(m.group(yi)), int(m.group(mi)), int(m.group(di))
            if mo > 12 and d <= 12:      # التباس dd/mm مقابل mm/dd
                mo, d = d, mo
            if 1900 <= y <= 2200 and 1 <= mo <= 12 and 1 <= d <= 31:
                return (y, mo, d)

    norm = normalize_ar(s)
    for name, mo in _AR_MONTHS.items():
        if name in norm:
            nums = re.findall(r"\d+", norm)
            if len(nums) >= 2:
                d, y = int(nums[0]), int(nums[-1])
                if 1 <= d <= 31 and 1900 <= y <= 2200:
                    return (y, mo, d)
    return None


def is_hijri_like(value) -> bool:
    """التواريخ الهجرية تبدأ بسنة 13xx/14xx — نكتشفها لنبلّغ المستخدم."""
    parts = re.findall(r"\d{4}", to_western_digits(str(value)))
    return any(1300 <= int(p) <= 1500 for p in parts)


# ---------------------------------------------------------- التنسيق (0-9)
def fmt_number(n: float | int | None, decimals: int | None = None) -> str:
    if n is None:
        return "—"
    if decimals is None:
        decimals = 0 if float(n).is_integer() else 2
    return f"{n:,.{decimals}f}"


def fmt_compact(n: float | int | None) -> str:
    if n is None:
        return "—"
    a = abs(n)
    if a >= 1_000_000:
        return f"{n/1_000_000:,.1f} مليون"
    if a >= 1_000:
        return f"{n/1_000:,.1f} ألف"
    return fmt_number(n)


def fmt_currency(n: float | None, symbol: str | None = None) -> str:
    """رمز العملة **لا يُفترَض**. كانت القيمة الافتراضية «ر.س»، فظهرت عملة
    السعودية على مبالغ مستودع في اللاذقية — رقمٌ صحيح بعملة كاذبة، وهو أسوأ من
    رقم بلا عملة لأن أحداً لا يشكّ فيه. من يعرف العملة يمرّرها (انظر currency.py)."""
    if n is None:
        return "—"
    num = fmt_number(n, 2)
    return f"{num} {symbol}" if symbol else num


def fmt_percent(n: float | None, decimals: int = 1) -> str:
    return "—" if n is None else f"{n:,.{decimals}f}%"


def similarity(a: str, b: str) -> float:
    from difflib import SequenceMatcher
    return SequenceMatcher(None, normalize_ar(a), normalize_ar(b)).ratio()


# ------------------------------------------------- التطبيع داخل استعلام Polars

# الأزواج التي يطبّقها normalize_ar على الحروف. نبقيها معلنة هنا حتى تبقى
# النسختان (بايثون وPolars) مشتقّتين من مصدر واحد ولا تتباعدا.
_NORMALIZE_PAIRS: list[tuple[str, str]] = (
    [(ch, "") for ch in (_TATWEEL,)]
    + [("آ", "ا"), ("أ", "ا"), ("إ", "ا"),
       ("ٱ", "ا")]                      # آ أ إ ٱ → ا
    + [("ى", "ي"), ("ی", "ي")]  # ى ی → ي
    + [("ؤ", "و")]                        # ؤ → و
    + [("ئ", "ي")]                        # ئ → ي
    + [("ة", "ه")]                        # ة → ه
    + [(a, w) for a, w in zip(ARABIC_INDIC, WESTERN)]
    + [(a, w) for a, w in zip(EXTENDED_INDIC, WESTERN)]
)


_NORMALIZE_SRC = [a for a, _ in _NORMALIZE_PAIRS]
_NORMALIZE_DST = [b for _, b in _NORMALIZE_PAIRS]


def normalize_expr(expr):
    """يطبّق تطبيع البحث على عمود Polars — نفس ما يفعله `normalize_ar` بالنص.

    ⚠️ ضرورة لا تحسين: البحث كان يطبّع المُدخَل فقط («شركة» ← «شركه») ويقارنه
    بقيم غير مطبَّعة، فأي كلمة فيها ة أو أ أو ى **لا تُطابق شيئاً أبداً** —
    ومعظم الأسماء العربية فيها واحدة منها.

    الاستيراد داخل الدالة: هذه الوحدة نصّية بحتة ولا يجب أن تفرض polars على
    من يستوردها لغرض آخر.
    """
    import polars as pl

    out = expr.cast(pl.Utf8, strict=False)
    # نفس محارف `_INVISIBLE` في النسخة النصّية — لو حُذفت من جهة وبقيت في
    # الأخرى لاختلف البحث عن التخزين وعادت مشكلة «كيانان بالعين واحد»
    out = out.str.replace_all(f"[{_INVISIBLE_CHARS}]", "")
    out = out.str.replace_all(r"[ً-ْٰٓ-ٕ]", "")   # الحركات
    # replace_many تمريرة واحدة بدل ثلاثين. على 300 ألف صف: 0.23s بدل 0.66s
    # لكل عمود — والبحث يمرّ على كل الأعمدة، فالفرق يتضاعف.
    out = out.str.replace_many(_NORMALIZE_SRC, _NORMALIZE_DST)
    return out.str.replace_all(r"\s+", " ").str.strip_chars().str.to_lowercase()
