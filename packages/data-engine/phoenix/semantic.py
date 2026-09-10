"""B3 — الفهم الدلالي للأعمدة. الطبقة التي تحوّل الأداة إلى منتج ذكي.

ثلاث طبقات بالترتيب، تتوقف عند أول ثقة عالية:
  1. القاموس        (اسم العمود بعد التطبيع + fuzzy)
  2. الأنماط        (النوع + الإحصاء + الأنماط)
  3. التحقق التقاطعي (كمية × سعر ≈ إجمالي)  ← يحسم الحالات الغامضة
  [4. LLM — واجهة جاهزة، غير مفعّلة في هذه الشريحة]
"""
from __future__ import annotations

from pathlib import Path

import polars as pl
import yaml

from . import currency
from .arabic_text import normalize_ar, parse_number, similarity
from .models import ColumnProfile, DatasetProfile, SemanticColumn, SemanticSchema

DICT_PATH = Path(__file__).parent / "dictionaries" / "ar.yaml"
FUZZY_THRESHOLD = 0.85
CROSS_VALIDATION_TOLERANCE = 0.02      # 2% سماحية لفروق التقريب
CROSS_VALIDATION_MIN_MATCH = 0.80      # 80% من الصفوف يجب أن تتطابق


def load_dictionary() -> dict:
    return yaml.safe_load(DICT_PATH.read_text(encoding="utf-8"))["concepts"]


# ------------------------------------------------------- الطبقة 1: القاموس
def match_dictionary(col_name: str, ctype: str, concepts: dict) -> SemanticColumn | None:
    norm = normalize_ar(col_name)
    best: tuple[float, str, dict] | None = None

    for concept, spec in concepts.items():
        aliases = spec.get("aliases_ar", []) + spec.get("aliases_en", [])
        for alias in aliases:
            na = normalize_ar(alias)
            if norm == na:
                score = 1.0
            elif norm and (norm in na or na in norm) and abs(len(norm) - len(na)) <= 4:
                score = 0.92
            else:
                sim = similarity(norm, na)
                score = sim if sim >= FUZZY_THRESHOLD else 0.0
            if score and (best is None or score > best[0]):
                best = (score, concept, spec)

    if not best:
        return None

    score, concept, spec = best
    expected = spec.get("expected_types", [])
    # «identifier» ليس نوع بيانات بل حكمٌ إحصائي على التفرّد: عمودٌ نصّي كل
    # قيمه مختلفة. وفي ملف كتالوج كلُّ اسم صنف مختلف بطبيعته — فكان «name_raw»
    # يُعاقَب على أنه اسم منتج لأن المُوصِّف سمّاه «معرّفاً».
    compatible = set(expected)
    if "text" in compatible:
        compatible.add("identifier")
    if expected and ctype not in compatible and ctype not in ("mixed", "empty"):
        score *= 0.6      # الاسم يطابق لكن النوع لا يناسب ⇒ نخفض الثقة

    return SemanticColumn(
        column_name=col_name, concept=concept, role=spec["role"],
        confidence=round(min(score, 1.0), 3), detection_method="dictionary",
        unit=spec.get("unit"),
        evidence_ar=f"اسم العمود يطابق مفهوم «{concept}» في القاموس (تشابه {score:.0%})",
    )


# ------------------------------------------------------- الطبقة 2: الأنماط
def match_pattern(p: ColumnProfile, has_time_already: bool) -> SemanticColumn:
    name, t = p.name, p.inferred_type

    if t in ("date", "datetime"):
        return SemanticColumn(column_name=name, concept=None if has_time_already else "date",
                              role="time", confidence=0.75, detection_method="pattern",
                              evidence_ar="العمود من نوع تاريخ")

    if t == "identifier" or (p.unique_pct > 95 and p.null_pct < 5 and t in ("text", "integer")):
        return SemanticColumn(column_name=name, role="identifier", confidence=0.7,
                              detection_method="pattern",
                              evidence_ar=f"قيم فريدة بنسبة {p.unique_pct:.0f}% ⇒ معرّف وليس مقياساً")

    if t in ("integer", "float"):
        if p.min is not None and p.min >= 0 and t == "integer" and (p.mean or 0) < 1000:
            return SemanticColumn(column_name=name, concept="quantity", role="measure",
                                  confidence=0.6, detection_method="pattern", unit="piece",
                                  evidence_ar=f"أعداد صحيحة موجبة بمتوسط {p.mean:.1f} ⇒ كمية على الأرجح")
        return SemanticColumn(column_name=name, role="measure", confidence=0.65,
                              detection_method="pattern",
                              evidence_ar="عمود عددي ⇒ مقياس قابل للتجميع")

    if t in ("categorical", "boolean"):
        return SemanticColumn(column_name=name, role="dimension", confidence=0.6,
                              detection_method="pattern",
                              evidence_ar=f"{p.unique_count} قيمة متكررة فقط ⇒ بُعد تصنيفي")

    if t == "text":
        return SemanticColumn(column_name=name, role="dimension", confidence=0.4,
                              detection_method="pattern", evidence_ar="عمود نصي")

    return SemanticColumn(column_name=name, role="unknown", confidence=0.2,
                          detection_method="none", evidence_ar="تعذّر تحديد الدور")


# ------------------------------- الطبقة 3: التحقق التقاطعي (كمية × سعر ≈ إجمالي)
def cross_validate_qty_price_total(
    df: pl.DataFrame, profile: DatasetProfile, sample: int = 150,
    exclude: set[str] | None = None,
) -> dict[str, str]:
    """يبحث عن ثلاثية a×b≈c بين الأعمدة العددية.

    هذا أقوى إشارة عملية في ملفات المبيعات العربية، ويحسم وحده معظم الحالات الغامضة.
    يُرجع: {اسم العمود: المفهوم}
    """
    skip = exclude or set()
    numeric = [c.name for c in profile.columns
               if c.inferred_type in ("integer", "float") and c.name not in skip]
    if len(numeric) < 3:
        return {}

    head = df.head(sample)
    vals: dict[str, list[float | None]] = {
        c: [parse_number(v) for v in head[c].cast(pl.Utf8, strict=False).to_list()]
        for c in numeric
    }

    best: tuple[float, str, str, str] | None = None
    for a in numeric:
        for b in numeric:
            if a == b:
                continue
            for c in numeric:
                if c in (a, b):
                    continue
                matches = tested = 0
                for i in range(len(head)):
                    x, y, z = vals[a][i], vals[b][i], vals[c][i]
                    if None in (x, y, z) or z == 0:
                        continue
                    tested += 1
                    if abs(x * y - z) / abs(z) <= CROSS_VALIDATION_TOLERANCE:
                        matches += 1
                if tested >= 20:
                    ratio = matches / tested
                    if ratio >= CROSS_VALIDATION_MIN_MATCH and (best is None or ratio > best[0]):
                        best = (ratio, a, b, c)

    if not best:
        return {}

    ratio, a, b, c = best
    pa = profile.column(a)
    pb = profile.column(b)
    # الأصغر متوسطاً والأقرب لأعداد صحيحة = الكمية، والآخر = السعر
    a_is_qty = (pa.inferred_type == "integer" and pb.inferred_type != "integer") or \
               ((pa.mean or 0) < (pb.mean or 0))
    qty, price = (a, b) if a_is_qty else (b, a)
    return {qty: "quantity", price: "unit_price", c: "total_amount", "_ratio": f"{ratio:.0%}"}


# ---------------------------------------------- الأعمدة الثابتة (وصف لا قياس)

# نفس عتبة المُوصِّف («العمود قيمته ثابتة ولا يضيف معلومة») — تعريف واحد للثبات
# في المحرك كلّه. تحت أحد عشر صفاً قد يتساوى عمودٌ صدفةً لا تصميماً.
from .profiling import CONSTANT_MIN_ROWS  # noqa: E402

CONSTANT_CONFIDENCE = 0.65   # < 0.7 ⇒ يظهر في «يحتاج مراجعة»


def constant_columns(profile: DatasetProfile) -> set[str]:
    """أعمدة لا تتغيّر قيمتها بين صف وآخر.

    عمود كهذا **لا يقيس شيئاً**: مجموعه يساوي دائماً القيمة × عدد الصفوف، وهو
    أثرٌ لحجم الملف لا حقيقة في البيانات. مصدره الشائع رأس الملف — رقم المستودع،
    تاريخ التصدير، عدد السجلات — تُنسخ على كل صف عند القراءة.

    العتبة (11 صفاً فأكثر) مقصودة: في ملف من صفّين، تساوي القيمتين صدفةٌ واردة
    جداً لا تصميم، وحذف العمود عندها خسارة لا وقاية.
    """
    if profile.row_count <= CONSTANT_MIN_ROWS:
        return set()
    return {c.name for c in profile.columns
            if c.unique_count == 1 and (c.total_count - c.null_count) > 0}


def _looks_like_a_record_count(p: ColumnProfile, row_count: int) -> bool:
    """عمود ثابت قيمته = عدد الصفوف بالضبط ⇒ حقل «عدد السجلات» من رأس الملف.

    هذه أدقّ إشارة في العيب كلّه: `count: 655` داخل ملف من 655 صفاً. لا صدفة
    فيها، ولا تُخطئ عمود كمية حقيقياً (كمية = 1 في 40 صفاً تبقى كمية).
    """
    if p.inferred_type not in ("integer", "float"):
        return False
    v = p.min if p.min is not None else None
    return v is not None and float(v) == float(row_count)


def _mark_constants(resolved: list[SemanticColumn], consts: set[str],
                    profile: DatasetProfile) -> None:
    """ثلاث معاملات مختلفة، لأن ثبات العمود يعني ثلاثة أشياء مختلفة:

    1. **ثابت نصّي** (رقم مستودع، مصدر، تاريخ تصدير) — وصفٌ للملف بلا لبس.
       يخرج من الأدوار الفاعلة بلا سؤال؛ لا أحد يريد رسماً بفئة واحدة.
    2. **حقل عدّ سجلات** (قيمته = عدد الصفوف) — هذا بالضبط ما أنتج خطأ الـ45
       ضعف. يخرج من المقاييس، ويُعرض للمراجعة لأن إخراجه قرارٌ يخص الأرقام.
    3. **ثابت عددي آخر** (كمية = 1 في كل صف، سعر موحّد) — يبقى على دوره: قد
       يكون قياساً حقيقياً لا يتغيّر. لكن ثقته تنزل، فيُعرض للمراجعة **ويتراجع
       تلقائياً أمام أي عمود آخر يحمل نفس المفهوم ويتغيّر فعلاً**. هذا وحده كان
       يكفي لمنع العيب: `qty` (تتغيّر) تسبق `count` (ثابتة) عند تساوي الثقة.
    """
    for sc in resolved:
        if sc.user_overridden or sc.column_name not in consts:
            continue
        p = profile.column(sc.column_name)
        if p is None:
            continue
        numeric = p.inferred_type in ("integer", "float")
        value = p.top_values[0][0] if p.top_values else None
        shown = f"«{value}» " if value is not None else ""
        base = f"القيمة {shown}نفسها في كل الصفوف."

        if not numeric:
            sc.concept, sc.unit = None, None
            sc.role = "constant"
            sc.confidence = 1.0
            sc.detection_method = "constant"
            sc.evidence_ar = (
                base + " هذا وصفٌ للملف كلّه (رقم مستودع، مصدر بيانات، تاريخ "
                "تصدير) لا بُعدٌ يُصنَّف به."
            )
        elif _looks_like_a_record_count(p, profile.row_count):
            sc.concept, sc.unit = None, None
            sc.role = "constant"
            sc.confidence = CONSTANT_CONFIDENCE
            sc.detection_method = "constant"
            sc.evidence_ar = (
                base + f" وهي تساوي عدد صفوف الملف ({profile.row_count}) — أي أنه "
                "حقل «عدد السجلات» من رأس الملف لا كمية في البيانات. جمعه يعطي "
                "القيمة × عدد الصفوف، رقمٌ يتبع حجم الملف لا ما فيه. إن كان فعلاً "
                "كمية، صحّحه."
            )
        else:
            sc.confidence = min(sc.confidence, CONSTANT_CONFIDENCE)
            sc.evidence_ar = (
                (sc.evidence_ar + " " if sc.evidence_ar else "") + base +
                " عمودٌ لا يتغيّر لا يميّز صفاً عن آخر، فمجموعه = القيمة × عدد "
                "الصفوف. تُرك على دوره لأنه قد يكون قياساً موحّداً فعلاً، لكن أي "
                "عمود آخر بنفس المعنى ويتغيّر يسبقه."
            )


# ------------------------------------------------------------ الدمج النهائي
def resolve_schema(df: pl.DataFrame, profile: DatasetProfile) -> SemanticSchema:
    concepts = load_dictionary()
    resolved: list[SemanticColumn] = []
    has_time = False

    for p in profile.columns:
        sc = match_dictionary(p.name, p.inferred_type, concepts)
        if sc is None or sc.confidence < 0.7:
            pattern = match_pattern(p, has_time)
            # ⚠️ في الجداول الصغيرة (مثل جدول مخزون من 10 أصناف) تكون كل القيم
            # العددية فريدة، فتصنّفها قاعدة التفرّد «معرّفاً» وتطغى على القاموس.
            # القاموس أوثق من الإحصاء عندما تكون العيّنة صغيرة.
            dict_wins = (sc is not None and pattern.role == "identifier"
                         and p.total_count < 100 and sc.confidence >= 0.6)
            if not dict_wins and (sc is None or pattern.confidence > sc.confidence):
                sc = pattern
        if sc.role == "time":
            has_time = True
        resolved.append(sc)

    # الأعمدة الثابتة تُستبعد من التحقق التقاطعي قبل أن يبدأ: عمودٌ لا يتغيّر
    # لا يصلح طرفاً في «كمية × سعر = إجمالي» مهما بدت النسبة مقنعة.
    consts = constant_columns(profile)

    # الطبقة 3 تصحّح ما سبقها — أدلّة البيانات أقوى من أدلّة الأسماء
    cross = cross_validate_qty_price_total(df, profile, exclude=consts)
    ratio = cross.pop("_ratio", None)
    if cross:
        by_name = {c.column_name: c for c in resolved}
        for col_name, concept in cross.items():
            sc = by_name.get(col_name)
            if not sc or sc.user_overridden:
                continue
            if sc.concept != concept or sc.confidence < 0.9:
                sc.concept = concept
                sc.role = "measure"
                sc.confidence = 0.95
                sc.detection_method = "cross_validation"
                sc.unit = "piece" if concept == "quantity" else "currency"
                sc.evidence_ar = (
                    f"تحقّق حسابي: كمية × سعر ≈ إجمالي متطابق في {ratio} من الصفوف المفحوصة"
                )

    _guard_ambiguous_money_columns(resolved, concepts)
    _guard_priced_in_a_currency(resolved, profile)

    schema = SemanticSchema(columns=resolved, domain=_detect_domain(resolved))

    # العملة تُقرأ من **البيانات الخام** قبل التنظيف: خطوة parse_numbers تمسح
    # «ل.س» من داخل القيم، فلو أجّلناها لضاع الدليل الأقوى على العملة.
    #
    # وتُقرأ كذلك **قبل** وسم الأعمدة الثابتة: في ملف بعملة واحدة يكون عمود
    # العملة ثابتاً بطبيعته («ل.س» في كل صف). لو وسمناه أولاً لأضعنا معنى أهم
    # عمود في الملف بحجة أنه لا يتغيّر — والثبات هنا هو المعلومة نفسها.
    schema.currency = currency.detect(df, profile, schema)

    # آخر ما يُطبَّق: لا قاموس ولا نمط ولا تحقّق تقاطعي يجعل ثابتاً مقياساً.
    _mark_constants(resolved, consts, profile)
    schema.domain = _detect_domain(resolved)
    _guard_inventory_quantity(resolved, schema.domain)
    return schema


# ------------------------------------------- الطبقة 4: كلمات مزدوجة المعنى
def _guard_ambiguous_money_columns(resolved: list[SemanticColumn], concepts: dict) -> None:
    """«Sales»/«مبيعات» ممكن تعني مبلغاً مالياً أو عدد وحدات مباعة — القاموس وحده
    ما يكفي ليحسم. عيب حقيقي شُوهد فعلياً: عمود "Sales" بدفتر مخزون (قيم صغيرة =
    عدد قطع) صُنِّف كمبلغ مالي بثقة كاملة وأُضيفت له عملة "ر.س" رغم أنه عدد وحدات.

    لو ما في أي عمود آخر بنفس الملف اتصنّف فعلاً «currency» (دليل مستقل يقوّي
    الافتراض إنو الملف فيه أرقام مالية أصلاً)، ولا التحقق التقاطعي (كمية×سعر=إجمالي)
    أكّد هالعمود تحديداً، فالأصح تركه بلا تصنيف بدل جواب واثق بعملة خاطئة
    (القاعدة الذهبية #5: الصمت أخطر من الرفض... لكنه أرحم من جواب خاطئ بثقة).
    """
    for sc in resolved:
        if sc.detection_method != "dictionary" or sc.concept != "total_amount":
            continue
        spec = concepts.get("total_amount", {})
        ambiguous = {normalize_ar(a) for a in
                     spec.get("ambiguous_aliases_ar", []) + spec.get("ambiguous_aliases_en", [])}
        if normalize_ar(sc.column_name) not in ambiguous:
            continue
        other_currency_exists = any(c is not sc and c.unit == "currency" for c in resolved)
        if other_currency_exists:
            continue
        sc.concept = None
        sc.unit = None
        sc.confidence = 0.4
        sc.evidence_ar = (
            f"اسم العمود «{sc.column_name}» يطابق كلمة مزدوجة المعنى (قد تعني مبلغاً "
            "مالياً أو عدد وحدات)، ولا يوجد بالملف أي دليل آخر (عمود مالي مؤكَّد، أو "
            "تحقق كمية×سعر=إجمالي) يحسم أنه مبلغ — تُرك بلا تصنيف تفادياً لجواب واثق خاطئ."
        )


def _guard_priced_in_a_currency(resolved: list[SemanticColumn],
                                profile: DatasetProfile) -> None:
    """«السعر بالدولار» ثمنٌ بالدولار، لا سعرُ صرف الدولار.

    الفرق حرفُ جرّ واحد: «سعر الدولار» = كم تساوي العملة، و«السعر بالدولار» =
    الثمن مقوَّماً بها. والقاموس الضبابي لا يرى هذا الفرق (التشابه بين
    العبارتين فوق 0.85)، فصنّف عمود أسعار على أنه سعر صرف — ومعه ضاعت عملته
    وضاع كونه مبلغاً أصلاً.

    القاعدة لا تعدّد الأسماء: أي اسم عمود يبدأ برأسٍ مالي (سعر/مبلغ/قيمة/إجمالي)
    ثم يذكر عملةً مسبوقةً بـ«بـ» ⇒ هو مبلغ بتلك العملة.
    """
    heads = ("سعر", "السعر", "مبلغ", "المبلغ", "قيمه", "القيمه", "اجمالي", "الاجمالي", "تكلفه", "التكلفه")
    for sc in resolved:
        # نتدخّل حيث لم يصل القاموس لجواب مالي: «سعر صرف» بالخطأ، أو بلا مفهوم
        # أصلاً (يقع كثيراً حين تكون كل الأسعار مختلفة فتُقرأ «معرّفاً»).
        if sc.user_overridden or sc.concept not in (None, "exchange_rate"):
            continue
        p = profile.column(sc.column_name)
        if p is None or p.inferred_type not in ("integer", "float"):
            continue
        norm = normalize_ar(sc.column_name)
        words = norm.split()
        if not words or words[0] not in heads:
            continue
        if not any(w.startswith("ب") and currency.detect_in_text(w) for w in words[1:]):
            continue
        sc.concept = "total_amount" if words[0].endswith("جمالي") else "unit_price"
        sc.role = "measure"
        sc.unit = "currency"
        sc.evidence_ar = (
            f"«{sc.column_name}» يبدأ باسم مالي ثم يذكر العملة مسبوقةً بـ«بـ» ⇒ "
            "مبلغ مقوَّم بتلك العملة، لا سعر صرفها."
        )


def _is_snapshot(names: set[str], roles: set[str]) -> bool:
    """لقطةُ جرد لا سجلَّ حركات.

    الفرق ليس في الأعمدة الموجودة بل في الغائبة: ملف الحركات فيه زمنٌ ومَن
    ومستند (تاريخ، عميل، رقم فاتورة). فإن غابت كلها وبقي «صنف + سعر + كمية»
    فما بين يديك رصيدُ لحظةٍ واحدة، لا شيءٌ حدث عبر الزمن.

    وهذا التمييز يغيّر المعنى لا العرض: نفس العمود «الكمية» يعني في الأول
    كميةً مباعة، وفي الثاني رصيداً موجوداً — ولا يجوز أن يُسمّى الاثنان سواء.
    """
    transactional = {"date", "invoice_no", "customer", "salesperson", "payment_method"}
    if names & transactional or "time" in roles:
        return False

    # ⚠️ غيابُ الحركات ليس دليلاً على الجرد. ملف مبيعات بسيط بلا تاريخ («الصنف،
    # الكمية، سعر الوحدة») يستوفي شرط الغياب وحده، فكان يُسمّى جرداً — فيقرأ
    # صاحبه «قيمة المخزون» فوق أرقام مبيعاته. فنشترط **دليلاً موجباً** على أن
    # الملف كتالوج: هويةُ صنفٍ مستقرة، أو وحدة قياس، أو مستودع، أو رصيد.
    #
    # ورمزُ الصنف (SKU) ليس دليلاً: هو في كل سطر بيع أيضاً. الدليل ما يظهر في
    # الكتالوج ونادراً في سطر الحركة — رصيدٌ مسمّى، مستودع، وحدة قياس، صلاحية،
    # حدّ إعادة طلب، أو صفةُ صنفٍ ثابتة (شكل دوائي، عيار).
    catalog_evidence = {"barcode", "warehouse", "unit", "stock_qty",
                        "expiry_date", "dosage_form", "strength", "reorder_level"}
    if not (names & catalog_evidence):
        return False

    has_item = bool(names & {"product_name", "product_code", "barcode"})
    has_amount = bool(names & {"unit_price", "total_amount", "quantity", "stock_qty"})
    return has_item and has_amount


def _detect_domain(cols: list[SemanticColumn]) -> str:
    names = {c.concept for c in cols}
    roles = {c.role for c in cols}
    if "expiry_date" in names and (names & {"supplier", "dosage_form", "strength"}):
        return "pharmacy"
    if _is_snapshot(names, roles):
        return "inventory"
    if "stock_qty" in names and "total_amount" not in names:
        return "warehouse"
    return "generic"


def _guard_inventory_quantity(resolved: list[SemanticColumn], domain: str) -> None:
    """في لقطة جرد، «الكمية» رصيدٌ لا مبيعات.

    عمود اسمه `qty` داخل كتالوج مستودع لا يعني «الكمية المباعة» — يعني الموجود
    على الرف. والفرق ليس تسميةً: يبني عليه المنتج جملةً كاملة يقولها للمستخدم
    («إجمالي المبيعات 245 مليون» على ملفٍ لا مبيعات فيه أصلاً)، ويقود لسؤال
    «كم بعنا؟» فيجيب برقم الجرد.
    """
    if domain not in ("inventory", "warehouse", "pharmacy"):
        return
    if any(c.concept == "stock_qty" for c in resolved):
        return
    for sc in resolved:
        if sc.concept == "quantity" and not sc.user_overridden:
            sc.concept = "stock_qty"
            sc.role = "measure"
            sc.evidence_ar = (
                f"«{sc.column_name}» كميةٌ في ملفٍ بلا تاريخ ولا فاتورة ولا عميل — "
                "أي لقطة جرد لا سجلّ حركات. فهي رصيدٌ موجود، لا كمية مباعة."
            )


class UnknownConceptError(ValueError):
    """مفهوم غير موجود في القاموس — نرفضه بدل تخزين قيمة لا يفهمها المحرك."""


def apply_overrides(schema: SemanticSchema,
                    overrides: dict[str, dict]) -> SemanticSchema:
    """يطبّق تصحيحات المستخدم على الـschema المستنتجة.

    الخطة (§5.4) تفرض واجهة تصحيح: أي عمود بثقة منخفضة يُعرض للمستخدم، وقراره
    **يعلو على استنتاج المحرك**. لذلك التصحيح هنا:
      • يضبط الثقة على 1.0 وطريقة الكشف على "user"
      • يرفع user_overridden، فيخرج العمود من قائمة «يحتاج مراجعة»
      • يستنتج الدور والوحدة من القاموس إن لم يحدّدهما المستخدم

    التحقق من صحة المفهوم يتم هنا لا في طبقة الـAPI: القاموس يعيش في المحرك،
    وطبقة الـAPI لا يجوز أن تعرف عنه شيئاً.
    """
    if not overrides:
        return schema

    concepts = load_dictionary()
    known = set(concepts.keys())
    updated: list[SemanticColumn] = []

    for col in schema.columns:
        patch = overrides.get(col.column_name)
        if not patch:
            updated.append(col)
            continue

        concept = patch.get("concept")
        if concept is not None and concept not in known:
            raise UnknownConceptError(concept)

        spec = concepts.get(concept or "", {}) if concept else {}
        role = patch.get("role") or (spec.get("role") if concept else "unknown")
        unit = patch.get("unit", spec.get("unit") if concept else None)

        updated.append(col.model_copy(update={
            "concept": concept,
            "role": role or "unknown",
            "unit": unit,
            "confidence": 1.0,
            "detection_method": "user",
            "user_overridden": True,
            "evidence_ar": "تصحيح من المستخدم.",
        }))

    return SemanticSchema(columns=updated, domain=_detect_domain(updated),
                          currency=schema.currency)


def available_concepts() -> list[dict]:
    """قائمة المفاهيم التي يفهمها المحرك — تغذّي قائمة الاختيار في الواجهة.

    مصدرها القاموس نفسه، فلا تتباعد الواجهة عن المحرك أبداً.
    """
    out = []
    for name, spec in load_dictionary().items():
        aliases = spec.get("aliases_ar") or []
        out.append({
            "concept": name,
            "role": spec.get("role", "unknown"),
            "unit": spec.get("unit"),
            # أول مرادف عربي = التسمية المعروضة؛ القاموس هو مصدر النص العربي
            "label_ar": aliases[0] if aliases else name,
        })
    return sorted(out, key=lambda c: c["concept"])
