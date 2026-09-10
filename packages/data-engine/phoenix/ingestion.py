"""B1 — الاستقبال: التحقق ← كشف الصيغة ← الترميز ← الشيت ← صف العناوين ← القراءة."""
from __future__ import annotations

import io
import json
import re
import xml.etree.ElementTree as ET
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

import polars as pl

from .arabic_text import arabic_ratio, count_ar, normalize_for_display
from .models import FileInfo

MAX_FILE_MB = 100
MAX_COMPRESSION_RATIO = 100      # حماية من zip bomb
HEADER_SCAN_ROWS = 20


class IngestionError(Exception):
    """خطأ بصياغة عربية مفهومة للمستخدم."""


# ------------------------------------------------------------ 1) التحقق
def validate_file(path: str | Path) -> Path:
    p = Path(path)
    if not p.exists():
        raise IngestionError(f"الملف غير موجود: {p}")
    size_mb = p.stat().st_size / 1024 / 1024
    if size_mb == 0:
        raise IngestionError("الملف فارغ.")
    if size_mb > MAX_FILE_MB:
        raise IngestionError(f"حجم الملف {size_mb:.1f} ميغابايت ويتجاوز الحد المسموح ({MAX_FILE_MB}).")
    return p


def detect_format(p: Path) -> str:
    """لا نثق بالامتداد — نقرأ magic bytes ثم نفحص المحتوى.

    ⚠️ إصلاح عيب حرج: كانت الدالة تُرجع "csv" لأي ملف غير معروف، فيُعامَل ملف
    HTML أو PDF كجدول بيانات وينتج المحرك هراءً ويعلن النجاح. الفشل الصامت أسوأ
    من الرفض الصريح. الآن كل صيغة غير مدعومة تُرفض برسالة واضحة.
    """
    head = p.open("rb").read(8)
    if head[:4] == b"PK\x03\x04":
        _check_zip_bomb(p)
        return "xlsx"
    if head[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return "xls"
    if head[:4] == b"%PDF":
        raise IngestionError("ملفات PDF غير مدعومة. صدّر البيانات بصيغة Excel أو CSV ثم أعد الرفع.")

    sample = p.open("rb").read(8192).decode("utf-8", errors="replace").lstrip()
    low = sample.lower()
    # ⚠️ إصلاح: ملف XML عادي (بلا وسم <table>) كان يُصنَّف "html" بالخطأ لمجرد
    # أنه يبدأ بـ"<?xml"، فيفشل لاحقاً برسالة مربكة ("لا يوجد جدول"). هلأ XML
    # مدعوم فعلياً (سجلات متكررة مسطّحة) — نفس فلسفة دعم JSON بالأسفل.
    if low.startswith("<?xml") and "<table" not in low:
        return _validate_xml_shape(p)
    if low.startswith(("<!doctype html", "<html")) or "<table" in low:
        return "html"
    if low.startswith("<"):
        return _validate_xml_shape(p)
    if low.startswith("{") or low.startswith("["):
        return _validate_json_shape(p)

    # فحص أخير: هل يشبه CSV فعلاً؟ (فاصل متكرر عبر الأسطر)
    lines = [l for l in sample.splitlines()[:10] if l.strip()]
    has_delimiter = any(
        sum(1 for l in lines if d in l) >= max(2, len(lines) * 0.6) for d in ",;\t|"
    )
    if len(lines) >= 2 and not has_delimiter:
        # ملف بعمود واحد (قائمة أسماء عملاء، قائمة أصناف) ليس ملفاً مجهول
        # الصيغة: لا فاصل فيه لأنه لا يحتاج فاصلاً. كان يُرفض برسالة
        # «تعذّر التعرّف على صيغة الملف» — وهي رسالة خاطئة ومربكة تماماً
        # لمن رفع ملفاً سليماً. نميّزه عن النص السردي بطول السطر: قيمة
        # في عمود بضع كلمات، والجملة السردية أطول من ذلك بكثير.
        short_lines = all(len(l.split()) <= 4 and len(l) <= 60 for l in lines)
        if p.suffix.lower() in (".csv", ".tsv", ".txt") and short_lines:
            return "csv"
        raise IngestionError(
            "تعذّر التعرّف على صيغة الملف. الصيغ المدعومة: XLSX، CSV، JSON، XML، "
            "HTML (جداول)."
        )
    return "csv"


def _flatten_json_record(rec: dict) -> dict:
    """يسطّح سجلاً واحداً: كائن جوّاني ← أعمدة بنقطة (address.city)، وقائمة قيم
    بسيطة ← نص مفصول بفاصلة. قائمة كائنات (جدول فرعي كامل) تُرفض صراحةً لأنها
    بيانات بمستوى تفصيل مختلف، ودمجها بصف واحد تخمين مو تسطيح.
    """
    out: dict = {}
    for k, v in rec.items():
        if isinstance(v, dict):
            for k2, v2 in v.items():
                if isinstance(v2, (dict, list)):
                    raise IngestionError(
                        f"تعشيش عميق داخل الحقل «{k}.{k2}» — مستويان فقط مدعومان "
                        "بهذه المرحلة، يلزم تسطيح البيانات أولاً."
                    )
                out[f"{k}.{k2}"] = v2
        elif isinstance(v, list):
            if any(isinstance(x, (dict, list)) for x in v):
                raise IngestionError(
                    f"الحقل «{k}» يحوي قائمة سجلات (جدول فرعي) — هذا مستوى تفصيل "
                    "مختلف عن الصف نفسه، فصدّره كملف منفصل بدل دمجه هنا."
                )
            out[k] = ", ".join("" if x is None else str(x) for x in v)
        else:
            out[k] = v
    return out


def _extract_json_records(p: Path) -> tuple[list[dict], list[str]]:
    """يستخرج سجلات الجدول من JSON بأشكاله الواقعية الفوضوية، ويرجّع معها
    قائمة تنبيهات بكل افتراض اتخذناه (لا تحويل صامت أبداً).

    المدعوم:
      • مصفوفة سجلات مباشرة:            [{...}, {...}]
      • مغلّف: كائن جذر فيه قائمة سجلات  {"period": "...", "sales": [{...}]}
        (وحقول الجذر البسيطة تنضاف كأعمدة ثابتة — معلومة مفيدة لا تُرمى)
      • حقول ناقصة بين سجل وآخر           ← اتحاد الحقول + فراغ مكان الناقص
      • كائن جوّاني بمستوى واحد           ← أعمدة بنقطة
      • قائمة قيم بسيطة داخل حقل          ← نص مفصول بفاصلة
    المرفوض بوضوح: قائمة كائنات داخل السجل (جدول فرعي)، وتعشيش أعمق من مستويين.
    """
    try:
        data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        raise IngestionError("تعذّر قراءة ملف JSON — الصياغة غير صحيحة.")

    warnings: list[str] = []
    constants: dict = {}

    if isinstance(data, dict):
        arrays = {k: v for k, v in data.items()
                  if isinstance(v, list) and v and all(isinstance(x, dict) for x in v)}
        if arrays:
            key = max(arrays, key=lambda k: len(arrays[k]))
            if len(arrays) > 1:
                warnings.append(
                    f"الملف يحوي أكثر من قائمة سجلات ({'، '.join(arrays)}) — "
                    f"حلّلنا الأكبر «{key}» ({len(arrays[key])} سجل) وتجاهلنا الباقي."
                )
            records = arrays[key]
            constants = {k: v for k, v in data.items() if not isinstance(v, (dict, list))}
            if constants:
                warnings.append(
                    "حقول عامة من رأس الملف أُضيفت كأعمدة ثابتة لكل صف: "
                    + "، ".join(constants)
                )
        elif all(not isinstance(v, (dict, list)) for v in data.values()):
            records = [data]
            warnings.append("الملف يمثّل سجلاً واحداً فقط (كائن واحد بلا قائمة سجلات).")
        else:
            raise IngestionError(
                "لم يُعثر على قائمة سجلات (array of objects) داخل هذا الملف — "
                "الصيغة المتوقعة: مصفوفة سجلات، أو كائن يحوي بداخله قائمة سجلات."
            )
    elif isinstance(data, list):
        records = data
    else:
        raise IngestionError(
            "جذر ملف JSON يجب أن يكون مصفوفة سجلات أو كائناً يحوي قائمة سجلات."
        )

    if not records:
        raise IngestionError("الملف فارغ.")
    if not all(isinstance(r, dict) for r in records):
        raise IngestionError("كل عنصر بقائمة السجلات يجب أن يكون كائناً (object) يمثّل صفاً.")

    rows = []
    for r in records:
        row = dict(constants)
        row.update(_flatten_json_record(r))
        rows.append(row)

    keys: list[str] = []
    for row in rows:
        for k in row:
            if k not in keys:
                keys.append(k)
    partial = [k for k in keys if any(k not in row for row in rows)]
    if partial:
        warnings.append(
            "حقول غير موجودة بكل السجلات (تُركت فارغة حيث غابت): " + "، ".join(partial[:8])
        )
    rows = [{k: row.get(k) for k in keys} for row in rows]
    return rows, warnings


def _validate_json_shape(p: Path) -> str:
    _extract_json_records(p)      # يرمي IngestionError واضحة لو الشكل غير مدعوم
    return "json"


def _find_record_elements(root: ET.Element) -> list[ET.Element]:
    """يدور عن أكثر وسم متكرر (مرتين أو أكثر) — إما بين أبناء الجذر مباشرة، أو
    بين أحفاده لمستوى واحد إضافي (مثل <root><items><item/><item/></items></root>).
    هذا يغطي الشكل الأشيع عملياً: جذر واحد يحوي قائمة سجلات بنفس الوسم.
    """
    def most_repeated(parent: ET.Element) -> tuple[str | None, int]:
        counts = Counter(c.tag for c in parent)
        return counts.most_common(1)[0] if counts else (None, 0)

    tag, n = most_repeated(root)
    if n >= 2:
        return [c for c in root if c.tag == tag]
    for child in root:
        tag, n = most_repeated(child)
        if n >= 2:
            return [c for c in child if c.tag == tag]
    return []


def _xml_record_fields(rec: ET.Element) -> dict:
    """سمات العنصر (attributes) + عناصره الأبناء المباشرة (وسم ← نص) كحقول مسطّحة."""
    fields = dict(rec.attrib)
    for child in rec:
        fields[child.tag] = child.text
    return fields


def _validate_xml_shape(p: Path) -> str:
    """يدعم فقط XML بشكل سجلات متكررة مسطّحة (عنصر جذر فيه عناصر بنفس الوسم،
    كل واحد منها سمات/عناصر أبناء بلا تعشيش أعمق). أي شكل تاني يُرفض برسالة
    صريحة تشرح السبب — نفس فلسفة دعم JSON، قاعدة ذهبية #4: لا نخمّن بنية غير واضحة.
    """
    try:
        root = ET.fromstring(p.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        raise IngestionError("تعذّر قراءة ملف XML — الصياغة غير صحيحة.")

    records = _find_record_elements(root)
    if not records:
        raise IngestionError(
            "لم يُعثر على عناصر متكررة تمثّل صفوفاً (سجلات) بهذا الملف — الصيغة "
            "المدعومة بهذه المرحلة: عنصر جذر يحوي عناصر متكررة بنفس الاسم لكل صف "
            "(مثل <sale> عدة مرات)."
        )
    if any(any(len(list(child)) > 0 for child in rec) for rec in records):
        raise IngestionError(
            "ملفات XML المتداخلة (عناصر متشعبة داخل السجل) غير مدعومة بهذه "
            "المرحلة — يلزم تسطيح البيانات أولاً."
        )
    keys0 = _xml_record_fields(records[0]).keys()
    if any(_xml_record_fields(r).keys() != keys0 for r in records[1:]):
        raise IngestionError(
            "سجلات الملف ليست متسقة (حقول مختلفة بين صف وآخر) — غير مدعوم بهذه المرحلة."
        )
    return "xml"


def _check_zip_bomb(p: Path) -> None:
    """فحص نسبة الضغط — وأول من يفتح الملف فعلياً.

    ملف تالف يجتاز فحص magic bytes (يبدأ بـPK) ثم ينفجر هنا برسالة مكتبة
    إنجليزية (BadZipFile). المستخدم لا يعرف ما الـzip، لكنه يفهم أن ملفه
    معطوب وأن عليه إعادة حفظه.
    """
    import zipfile
    try:
        _zip_ratio_check(p)
    except zipfile.BadZipFile as e:
        raise IngestionError(
            "الملف معطوب أو غير مكتمل ولا يمكن فتحه. جرّب فتحه بإكسل "
            "وإعادة حفظه بصيغة XLSX ثم ارفعه من جديد."
        ) from e


def _zip_ratio_check(p: Path) -> None:
    import zipfile
    with zipfile.ZipFile(p) as z:
        packed = sum(i.compress_size for i in z.infolist()) or 1
        unpacked = sum(i.file_size for i in z.infolist())
        if unpacked / packed > MAX_COMPRESSION_RATIO:
            raise IngestionError("الملف مشبوه (نسبة ضغط غير طبيعية) وتم رفضه لأسباب أمنية.")


# ------------------------------------------------------------ 2) الترميز
_META_CHARSET = re.compile(rb'charset=["\']?([\w-]+)', re.I)


def detect_encoding(p: Path) -> str:
    """CSV العربي الخارج من Excel غالباً CP1256 لا UTF-8.

    ⚠️ الترميز المُعلَن صراحةً (BOM أو <meta charset>) يسبق التخمين الإحصائي دائماً.
    بدون هذا كان النص العربي في ملفات HTML يتشوّه إلى «ط§ظ„ظ…ظ†طھط¬».
    """
    raw = p.open("rb").read(64 * 1024)
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"

    m = _META_CHARSET.search(raw[:4096])
    if m:
        declared = m.group(1).decode("ascii", errors="ignore").lower()
        try:
            raw.decode(declared, errors="strict")
            return declared
        except (UnicodeDecodeError, LookupError):
            pass      # الإعلان خاطئ — نكمل بالتخمين
    # UTF-8 صالح + أي حرف غير ASCII ⇒ الملف UTF-8، بلا مقارنة نِسَب.
    #
    # لماذا قاعدة قاطعة لا نقطة في التنافس؟ لأن المقارنة الإحصائية كانت تفشل
    # هنا بفارق أربعة بالألف: ملف عربي بترميز UTF-8 نال 0.2730 مقابل 0.2768
    # لقراءته الخاطئة بـcp1256 — فخسر. السبب أن cp1256 تقرأ أي بايت بلا خطأ،
    # ونصفُ ما تُخرجه من فوضى يقع في نطاق الحروف العربية («ط§ظ„طµظ†ظپ» عربيةٌ
    # إحصائياً!) — فترتفع نسبتها بلا استحقاق.
    #
    # والقاعدة ليست تفضيلاً بل حقيقة في البنية: UTF-8 تفرض بايتات متابعة
    # محدّدة، ونصٌّ عربي مكتوب بـcp1256 لا يصادف تلك البنية عملياً أبداً. فمن
    # يمرّ من فحص UTF-8 الصارم ويحمل حرفاً غير إنكليزي، هو UTF-8.
    #
    # ⚠️ نقرأ 64 كيلوبايت فقط، وقد ينقطع حرف متعدد البايتات عند الحافة —
    # فنحذف حتى ثلاثة بايتات من الذيل قبل الحكم، وإلا رفضنا ملفاً سليماً.
    for cut in range(4):
        chunk = raw[:len(raw) - cut] if cut else raw
        try:
            text = chunk.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if any(ord(ch) > 127 for ch in text):
            return "utf-8"
        break      # نص إنكليزي صرف — لا فرق بين الترميزات، نكمل للافتراضي

    best, best_score = "utf-8", -1.0
    for enc in ("utf-8", "cp1256", "iso-8859-6", "utf-16"):
        try:
            text = raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        # الترميز الصحيح ينتج أعلى نسبة حروف عربية وأقل رموز مكسورة
        score = arabic_ratio(text) - (text.count("\ufffd") / max(len(text), 1)) * 5
        if score > best_score:
            best, best_score = enc, score
    return best


def detect_delimiter(p: Path, encoding: str) -> str:
    sample = p.open("r", encoding=encoding, errors="replace").read(16 * 1024)
    lines = [l for l in sample.splitlines()[:20] if l.strip()]
    if not lines:
        return ","
    best, best_score = ",", -1.0
    for d in [",", ";", "\t", "|"]:
        counts = [l.count(d) for l in lines]
        if not counts or max(counts) == 0:
            continue
        avg = sum(counts) / len(counts)
        variance = sum((c - avg) ** 2 for c in counts) / len(counts)
        score = avg - variance      # اتساق عبر الأسطر أهم من الكثرة
        if score > best_score:
            best, best_score = d, score
    return best


# ------------------------------------------------------------ 3) صف العناوين
def _cell_str(v) -> str:
    return "" if v is None else str(v).strip()


def find_header_row(raw_rows: list[list]) -> tuple[int, float]:
    """يجد صف العناوين الحقيقي وسط الشعارات والعناوين الفوقية.

    منطق التسجيل:
      + امتلاء الصف
      + كون القيم نصية وفريدة
      + اختلاف نوع الصف التالي عنه (العناوين نص، البيانات أرقام/تواريخ)
      - وجود أرقام كثيرة في الصف نفسه
    """
    best_row, best_score = 0, -999.0
    scan = min(HEADER_SCAN_ROWS, len(raw_rows))

    for i in range(scan):
        cells = [_cell_str(c) for c in raw_rows[i]]
        filled = [c for c in cells if c]
        if len(filled) < 2:
            continue

        fill_ratio = len(filled) / max(len(cells), 1)
        unique_ratio = len(set(filled)) / len(filled)
        numeric = sum(1 for c in filled if _looks_numeric(c)) / len(filled)

        score = fill_ratio * 3 + unique_ratio * 2 - numeric * 4

        if i + 1 < len(raw_rows):
            nxt = [_cell_str(c) for c in raw_rows[i + 1] if _cell_str(c)]
            if nxt:
                nxt_numeric = sum(1 for c in nxt if _looks_numeric(c)) / len(nxt)
                score += (nxt_numeric - numeric) * 3      # القفزة النوعية = دليل قوي
                score += (len(nxt) / max(len(filled), 1)) * 0.5

        if score > best_score:
            best_row, best_score = i, score

    confidence = max(0.0, min(1.0, best_score / 8.0))
    return best_row, confidence


def _looks_numeric(s: str) -> bool:
    from .arabic_text import parse_number
    return parse_number(s) is not None


# ------------------------------------------------------------ قارئ HTML
class _TableParser(HTMLParser):
    """يستخرج كل جداول <table> — قارئ عام لا يعرف شيئاً عن ملف بعينه.

    مبرّره: برامج المحاسبة العربية (ومنها الأمين) تُصدّر تقاريرها HTML كثيراً.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._tbl: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "table":
            self._tbl = []
        elif tag == "tr" and self._tbl is not None:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag in ("td", "th") and self._cell is not None:
            self._row.append(re.sub(r"\s+", " ", "".join(self._cell)).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if any(c for c in self._row):
                self._tbl.append(self._row)
            self._row = None
        elif tag == "table" and self._tbl is not None:
            if self._tbl:
                self.tables.append(self._tbl)
            self._tbl = None

    def handle_data(self, data):
        if self._cell is not None and not self._skip:
            self._cell.append(data)


def extract_html_tables(p: Path) -> list[list[list[str]]]:
    enc = detect_encoding(p)
    parser = _TableParser()
    parser.feed(p.read_text(encoding=enc, errors="replace"))
    return parser.tables


def looks_like_report(rows: list[list[str]], html_text: str) -> str | None:
    """يميّز التقرير الجاهز عن البيانات الخام.

    التقرير: أرقام مجمّعة قليلة + نص سردي كثير خارج الجداول.
    البيانات الخام: صفوف كثيرة متكررة البنية.
    """
    data_rows = max(0, len(rows) - 1)
    text_only = re.sub(r"<table.*?</table>", "", html_text, flags=re.S | re.I)
    text_only = re.sub(r"<(script|style).*?</\1>", "", text_only, flags=re.S | re.I)
    narrative = len(re.sub(r"<[^>]+>", " ", text_only).split())
    if data_rows < 30 and narrative > 200:
        return (f"الملف يبدو تقريراً جاهزاً لا بيانات خام: "
                f"{count_ar(data_rows, 'صف بيانات', 'صفّا بيانات', 'صفوف بيانات')} فقط "
                f"مقابل ~{narrative} كلمة نص سردي. التحليل سيقتصر على الجدول المستخرج.")
    return None


# ------------------------------------------------------------ 4) القراءة
def _read_excel_raw(p: Path, sheet: str) -> list[list]:
    import fastexcel
    reader = fastexcel.read_excel(str(p))
    sh = reader.load_sheet_by_name(sheet, header_row=None)
    df = sh.to_polars()
    return [list(r) for r in df.iter_rows()]


def list_sheets(p: Path) -> list[str]:
    """أسماء الأوراق — وأول نقطة تلمس محتوى الملف فعلاً.

    ملف تالف يجتاز فحص magic bytes (يبدأ بـPK) ثم ينفجر هنا برسالة مكتبة
    إنجليزية (BadZipFile). نحوّلها إلى خطأ مفهوم: المستخدم لا يعرف ما
    الـzip، لكنه يفهم أن ملفه معطوب وأن عليه إعادة حفظه.
    """
    import fastexcel
    try:
        return fastexcel.read_excel(str(p)).sheet_names
    except Exception as e:                                        # noqa: BLE001
        raise IngestionError(
            "الملف معطوب أو غير مكتمل ولا يمكن فتحه. جرّب فتحه بإكسل "
            "وإعادة حفظه بصيغة XLSX ثم ارفعه من جديد."
        ) from e


def pick_sheet(p: Path, sheets: list[str]) -> str:
    """أكبر شيت فيه بيانات فعلية — الفارغة تُتجاهل."""
    import fastexcel
    reader = fastexcel.read_excel(str(p))
    best, best_cells = sheets[0], -1
    for s in sheets:
        try:
            sh = reader.load_sheet_by_name(s, header_row=None)
            cells = sh.height * sh.width
        except Exception:
            cells = 0
        if cells > best_cells:
            best, best_cells = s, cells
    return best


def load(path: str | Path, sheet: str | None = None) -> tuple[pl.DataFrame, FileInfo]:
    """المدخل الوحيد لهذه الطبقة. يُرجع DataFrame خام + معلومات الملف."""
    p = validate_file(path)
    fmt = detect_format(p)
    info = FileInfo(
        path=str(p), filename=p.name, size_bytes=p.stat().st_size,
        detected_format=fmt,
    )

    if fmt == "csv":
        enc = detect_encoding(p)
        delim = detect_delimiter(p, enc)
        info.encoding, info.delimiter = enc, delim
        text = p.read_text(encoding=enc, errors="replace")
        raw_rows = [line.split(delim) for line in text.splitlines()[:HEADER_SCAN_ROWS]]
        hrow, conf = find_header_row(raw_rows)
        common = dict(separator=delim, skip_rows=hrow, truncate_ragged_lines=True,
                      try_parse_dates=False)
        df = pl.read_csv(io.StringIO(text), infer_schema_length=10_000,
                         ignore_errors=True, **common)
        # ⚠️ عيب حقيقي: ignore_errors يحوّل ما يعجز عن تحليله إلى null بصمت.
        # عمود كامل بأرقام هندية («٩، ٢٧») كان يُستنتج رقمياً ثم يُفرَّغ
        # كله، فيحذفه _drop_empty — يختفي عمود الكمية من الملف بلا أي رسالة.
        # العلاج: قراءة ثانية نصّية بحتة، ونستعيد منها كل عمود خسر قيماً.
        as_text = pl.read_csv(io.StringIO(text), infer_schema_length=0, **common)
        rescued = [c for c in df.columns
                   if c in as_text.columns
                   and df[c].null_count() > as_text[c].null_count()]
        if rescued:
            df = df.with_columns([as_text[c].alias(c) for c in rescued])
        info.warnings.extend(_ragged_row_warning(text, delim, hrow, df.width))
    elif fmt == "xlsx":
        sheets = list_sheets(p)
        info.sheet_names = sheets
        chosen = sheet or pick_sheet(p, sheets)
        info.selected_sheet = chosen
        raw = _read_excel_raw(p, chosen)
        hrow, conf = find_header_row(raw[:HEADER_SCAN_ROWS])

        header = [normalize_for_display(_cell_str(c)) or f"عمود_{i+1}"
                  for i, c in enumerate(raw[hrow])]
        header = _dedupe(header)
        body = raw[hrow + 1:]
        width = len(header)
        body = [(r + [None] * width)[:width] for r in body]
        df = pl.DataFrame(
            {header[i]: [r[i] for r in body] for i in range(width)},
            strict=False,
        )
    elif fmt == "html":
        enc = detect_encoding(p)
        info.encoding = enc
        tables = extract_html_tables(p)
        if not tables:
            raise IngestionError("لا يوجد أي جدول <table> في هذا الملف — لا بيانات لتحليلها.")
        info.sheet_names = [f"جدول {i+1} ({count_ar(len(t), 'صف', 'صفّان', 'صفوف')})"
                            for i, t in enumerate(tables)]
        idx = max(range(len(tables)), key=lambda i: len(tables[i]) * len(tables[i][0]))
        info.selected_sheet = info.sheet_names[idx]
        raw = tables[idx]

        note = looks_like_report(raw, p.read_text(encoding=enc, errors="replace"))
        if note:
            info.warnings.append(note)

        hrow, conf = find_header_row(raw[:HEADER_SCAN_ROWS])
        header = _dedupe([normalize_for_display(_cell_str(c)) or f"عمود_{i+1}"
                          for i, c in enumerate(raw[hrow])])
        width = len(header)
        body = [(r + [None] * width)[:width] for r in raw[hrow + 1:]]
        df = pl.DataFrame({header[i]: [r[i] for r in body] for i in range(width)}, strict=False)

    elif fmt == "json":
        records, json_warnings = _extract_json_records(p)
        info.warnings.extend(json_warnings)
        hrow, conf = 0, 1.0
        keys = list(records[0].keys())
        header = _dedupe([normalize_for_display(str(k)) or f"عمود_{i+1}"
                          for i, k in enumerate(keys)])
        df = pl.DataFrame(
            {header[i]: [r.get(keys[i]) for r in records] for i in range(len(keys))},
            strict=False,
        )

    elif fmt == "xml":
        # الشكل تحقّقنا منه مسبقاً بـdetect_format: عناصر متكررة مسطّحة ومتّسقة الحقول.
        root = ET.fromstring(p.read_text(encoding="utf-8", errors="replace"))
        records = [_xml_record_fields(r) for r in _find_record_elements(root)]
        hrow, conf = 0, 1.0
        keys = list(records[0].keys())
        header = _dedupe([normalize_for_display(str(k)) or f"عمود_{i+1}"
                          for i, k in enumerate(keys)])
        df = pl.DataFrame(
            {header[i]: [r.get(keys[i]) for r in records] for i in range(len(keys))},
            strict=False,
        )

    else:
        raise IngestionError("صيغة XLS القديمة غير مدعومة في هذه المرحلة. احفظ الملف بصيغة XLSX ثم أعد رفعه.")

    info.header_row, info.header_confidence = hrow, conf
    df = _drop_empty(df)
    if df.height == 0:
        raise IngestionError("لم يُعثر على أي صف بيانات في الملف.")
    return df, info


def _ragged_row_warning(text: str, delim: str, header_row: int,
                        width: int) -> list[str]:
    """يُنبّه إذا كانت بعض الصفوف تحمل حقولاً أكثر من الرؤوس.

    `truncate_ragged_lines=True` يبتر الزائد **بصمت**. سببه الشائع فاصلة
    داخل نص غير محاط باقتباس («الشام، دمشق»)، وأثره أن قيمة تنتقل إلى عمود
    آخر أو تُبتر — أخطر من رفض الملف، لأن الخطأ لا يُرى.

    نستعمل قارئ csv القياسي لا التقسيم النصّي: التقسيم يعدّ الفواصل داخل
    الاقتباسات فيُنذر كذباً على كل ملف فيه نص محاط باقتباس.
    """
    import csv as _csv

    try:
        rows = _csv.reader(io.StringIO(text), delimiter=delim)
        for _ in range(header_row + 1):
            next(rows, None)
        bad = [i for i, r in enumerate(rows, start=header_row + 2) if len(r) > width]
    except Exception:                                             # noqa: BLE001
        return []
    if not bad:
        return []
    where = "، ".join(str(i) for i in bad[:5])
    more = " وغيرها" if len(bad) > 5 else ""
    return [f"{count_ar(len(bad), 'صف يحتوي', 'صفّان يحتويان', 'صفوف تحتوي')} "
            f"حقولاً أكثر من عدد الأعمدة وتم بتر الزائد "
            f"(الصفوف: {where}{more}). السبب الغالب فاصلة داخل نص غير محاط "
            f"بعلامتَي اقتباس."]


def _dedupe(names: list[str]) -> list[str]:
    seen, out = {}, []
    for n in names:
        if n in seen:
            seen[n] += 1
            out.append(f"{n}_{seen[n]}")
        else:
            seen[n] = 0
            out.append(n)
    return out


def _drop_empty(df: pl.DataFrame) -> pl.DataFrame:
    """إزالة الصفوف والأعمدة الفارغة تماماً — ليست تعديلاً على البيانات."""
    keep = [c for c in df.columns
            if not (df[c].null_count() == df.height or
                    (df[c].cast(pl.Utf8, strict=False).fill_null("").str.strip_chars() == "").all())]
    df = df.select(keep) if keep else df
    if df.height:
        mask = pl.fold(
            acc=pl.lit(False),
            function=lambda a, s: a | s,
            exprs=[pl.col(c).cast(pl.Utf8, strict=False).fill_null("").str.strip_chars() != ""
                   for c in df.columns],
        )
        df = df.filter(mask)
    return df
