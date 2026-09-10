"""العمود الثابت ليس مقياساً — حارس خطأ الـ45 ضعف (ق-49).

القصة بالضبط: ملف `catalog.json` من مستودع حقيقي يحمل في رأسه `"count": 655`.
القارئ ينسخ حقول الرأس على كل صف (قرار سليم: رقم المستودع معلومة مفيدة)، فصار
`count` عموداً قيمته 655 في 655 صفاً. القاموس رأى الاسم «count» فصنّفه «كمية»
بثقة كاملة، وسبق `qty` في الترتيب، فحُسب الإجمالي = مجموع الأسعار × 655.

النتيجة: **11,196,761,803** بدل **245,140,775.86**. خمسة وأربعون ضعفاً.

وأخطر ما في العيب أنه **لا يبدو عيباً**: الرقم منسّق، والعملة موجودة، والرسوم
تُبنى عليه بلا شكوى. لا يكشفه إلا أن يحسب أحدهم القيمة بيده.

المفارقة أن المُوصِّف كان يعرف: كتب في مشاكل الجودة «العمود قيمته ثابتة ولا
يضيف معلومة» — ثم مضت الطبقة الدلالية وجعلته المقياس الأول. المعرفة كانت
موجودة ولم يستهلكها أحد.
"""
from __future__ import annotations

import sys
from pathlib import Path

import polars as pl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from phoenix import charts, pipeline  # noqa: E402
from phoenix.profiling import profile_dataset  # noqa: E402
from phoenix.semantic import constant_columns, resolve_schema  # noqa: E402

REAL = Path(__file__).resolve().parents[1] / "fixtures" / "real" / "feniqsync_catalog.json"

# محسوبة خارج فينيق، ببايثون مباشرةً على الملف (انظر fixtures/real/README.md)
TRUE_STOCK_VALUE = 245_140_775.86
TRUE_QTY_TOTAL = 23_674.46
WRONG_45X = 11_196_761_803.65


@pytest.fixture(scope="module")
def real_run(tmp_path_factory):
    return pipeline.process(REAL, run_dir=tmp_path_factory.mktemp("real") / "run")


class TestTheRealFileGivesTheRealNumber:
    """أهم اختبار في الملف: رقم واحد، من بيانات حقيقية، يُقارَن بالحقيقة."""

    def _kpis(self, real_run) -> dict:
        engine = real_run.engine()
        try:
            return {k["key"]: k for k in charts.build_kpis(engine)}
        finally:
            engine.close()

    def test_stock_value_matches_hand_calculation(self, real_run):
        kpis = self._kpis(real_run)
        assert "total_amount" in kpis, f"لا مؤشّر قيمة أصلاً: {list(kpis)}"
        total = kpis["total_amount"]["value"]
        assert total == pytest.approx(TRUE_STOCK_VALUE, rel=1e-6), (
            f"قيمة المخزون {total:,.2f} والصحيح {TRUE_STOCK_VALUE:,.2f}"
        )
        assert total != pytest.approx(WRONG_45X, rel=1e-3), "عاد خطأ الـ45 ضعف"

    def test_quantity_total_matches_hand_calculation(self, real_run):
        kpis = self._kpis(real_run)
        assert "stock_qty" in kpis, f"لا مؤشّر رصيد: {list(kpis)}"
        qty = kpis["stock_qty"]["value"]
        assert qty == pytest.approx(TRUE_QTY_TOTAL, rel=1e-6), (
            f"مجموع الرصيد {qty} والصحيح {TRUE_QTY_TOTAL} "
            "(655×655=429,025 يعني أنه جمع عمود العدّ الثابت)"
        )

    def test_the_header_fields_are_marked_constant_not_measures(self, real_run):
        by_name = {c.column_name: c for c in real_run.schema.columns}
        for name in ("warehouse_id", "source", "price_level", "generated_at", "count"):
            assert name in by_name, f"العمود «{name}» اختفى من الـschema"
            assert by_name[name].role == "constant", (
                f"«{name}» دوره {by_name[name].role} وهو ثابت في كل الصفوف")
        assert by_name["count"].concept is None, "عمود العدّ ما زال يحمل مفهوماً"

    def test_the_real_measures_survived(self, real_run):
        """حارسٌ يمسح كل الأعمدة ليس حارساً — لازم يبقى ما يُقاس فعلاً."""
        by_name = {c.column_name: c for c in real_run.schema.columns}
        assert by_name["qty"].role == "measure"
        assert by_name["price"].role == "measure"
        assert by_name["price"].concept == "unit_price"

    def test_the_numeric_constant_is_offered_for_review(self, real_run):
        """الثابت العددي هو ما كان سيصير مقياساً — فيُعرض ليقرّر المستخدم،
        ويُشرح له السبب بدل أن يختفي بصمت."""
        count_col = next(c for c in real_run.schema.columns if c.column_name == "count")
        assert count_col.needs_review, "اختفى بصمت بلا فرصة اعتراض"
        assert "عدد الصفوف" in count_col.evidence_ar or "حجم الملف" in count_col.evidence_ar

    def test_the_text_constants_do_not_nag(self, real_run):
        """«اسم المستودع ثابت» ليس سؤالاً يستحق إزعاج المستخدم."""
        src = next(c for c in real_run.schema.columns if c.column_name == "source")
        assert not src.needs_review


class TestTheRuleItself:
    def test_a_constant_column_is_detected(self):
        df = pl.DataFrame({"ثابت": [7] * 20, "متغيّر": list(range(20))})
        assert constant_columns(profile_dataset(df)) == {"ثابت"}

    def test_a_single_row_file_has_no_constants(self):
        """صفٌّ واحد: كل عمود «ثابت» شكلاً، ومجموعه = قيمته، وهو معنى صحيح.
        الحدّ المقابل — بدونه يصير الحارس ماسحة تمسح كل ملف من صف واحد."""
        df = pl.DataFrame({"المبلغ": [500.0], "العميل": ["أحمد"]})
        assert constant_columns(profile_dataset(df)) == set()

    def test_a_tiny_file_is_left_alone(self):
        """صفّان متساويان صدفةٌ لا تصميم. عتبة الثبات (11 صفاً) هي نفسها التي
        يستعملها المُوصِّف — تعريف واحد للثبات في المحرك كلّه."""
        df = pl.DataFrame({"الإجمالي": [999, 999], "الصنف": ["أ", "ب"]})
        assert constant_columns(profile_dataset(df)) == set()

    def test_an_all_null_column_is_not_called_constant(self):
        df = pl.DataFrame({"فارغ": [None] * 20, "رقم": list(range(20))},
                          schema={"فارغ": pl.Utf8, "رقم": pl.Int64})
        assert "فارغ" not in constant_columns(profile_dataset(df))


class TestTheThreeKindsOfConstant:
    """ثبات العمود يعني ثلاثة أشياء مختلفة، فله ثلاث معاملات."""

    def test_a_text_constant_leaves_the_active_roles(self):
        df = pl.DataFrame({"المستودع": ["مستودع النجاح"] * 30,
                           "الصنف": [f"ص{i}" for i in range(30)],
                           "السعر": [10.0 + i for i in range(30)]})
        schema = resolve_schema(df, profile_dataset(df))
        col = next(c for c in schema.columns if c.column_name == "المستودع")
        assert col.role == "constant" and not col.needs_review

    def test_a_record_count_field_is_removed_from_the_measures(self):
        n = 30
        df = pl.DataFrame({"count": [n] * n,
                           "السعر": [10.0 + i for i in range(n)],
                           "الصنف": [f"ص{i}" for i in range(n)]})
        schema = resolve_schema(df, profile_dataset(df))
        col = next(c for c in schema.columns if c.column_name == "count")
        assert col.role == "constant", "حقل عدّ السجلات ما زال مقياساً"
        assert col.needs_review, "أُخرج من الحساب بصمت بلا فرصة اعتراض"

    def test_an_ordinary_numeric_constant_keeps_its_role(self):
        """كمية = 1 في كل صف قد تكون كمية حقيقية موحّدة. لا نحذفها — ننزل
        بثقتها فقط، فتُعرض للمراجعة وتتراجع أمام أي عمود يتغيّر."""
        n = 30
        df = pl.DataFrame({"الكمية": [1] * n,
                           "سعر الوحدة": [100.0 + i for i in range(n)],
                           "العميل": [f"عميل {i}" for i in range(n)]})
        schema = resolve_schema(df, profile_dataset(df))
        qty = next(c for c in schema.columns if c.column_name == "الكمية")
        assert qty.role == "measure" and qty.concept == "quantity"
        assert qty.needs_review, "ثابت عددي بلا تنبيه — المستخدم لن يعرف"

    def test_a_varying_column_wins_over_a_constant_with_the_same_meaning(self):
        """جوهر خطأ الـ45 ضعف في سطر واحد: عمودان يدّعيان «الكمية»، أحدهما
        لا يتغيّر. الذي يتغيّر هو الكمية."""
        n = 30
        df = pl.DataFrame({"count": [7] * n,               # ثابت، وليس عدد صفوف
                           "qty": [float(i % 5) for i in range(n)],
                           "price": [100.0 + i for i in range(n)],
                           "name": [f"ص{i}" for i in range(n)]})
        schema = resolve_schema(df, profile_dataset(df))
        assert schema.by_concept("quantity").column_name == "qty"


class TestTheUserOutranksTheEngine:
    def test_a_user_correction_beats_the_rule(self):
        """قرار المستخدم يعلو على استنتاج المحرك — الخطة §5.4."""
        from phoenix.semantic import apply_overrides
        n = 30
        df = pl.DataFrame({"count": [n] * n, "الصنف": [f"ص{i}" for i in range(n)]})
        schema = resolve_schema(df, profile_dataset(df))
        assert schema.by_concept("quantity") is None
        fixed = apply_overrides(schema, {"count": {"concept": "quantity"}})
        col = next(c for c in fixed.columns if c.column_name == "count")
        assert col.role == "measure" and col.concept == "quantity"

    def test_a_correction_is_not_undone_on_reprocessing(self):
        """التصحيح يُعاد تطبيقه بعد إعادة الحساب — وإلا عاد الحارس فمحاه."""
        from phoenix.semantic import apply_overrides
        n = 30
        df = pl.DataFrame({"count": [n] * n, "الصنف": [f"ص{i}" for i in range(n)]})
        fixed = apply_overrides(resolve_schema(df, profile_dataset(df)),
                                {"count": {"concept": "quantity"}})
        prof = profile_dataset(df)
        from phoenix.semantic import _mark_constants, constant_columns
        _mark_constants(list(fixed.columns), constant_columns(prof), prof)
        col = next(c for c in fixed.columns if c.column_name == "count")
        assert col.role == "measure", "الحارس داس على تصحيح المستخدم"


class TestTheGuardCatchesTheOriginalBug:
    """حارسٌ لا يفشل على العيب الذي وُجد لأجله ليس حارساً.

    نعيد بناء العيب حرفياً: عمود ثابت اسمه يطابق القاموس، وقيمته تساوي عدد
    الصفوف — ونتأكد أن القاعدة تمنعه من أن يصير الكمية."""

    def test_a_record_count_field_never_becomes_the_quantity(self):
        n = 40
        df = pl.DataFrame({
            "count": [n] * n,
            "qty": [float(i % 7) for i in range(n)],
            "price": [100.0 + i for i in range(n)],
            "name": [f"صنف {i}" for i in range(n)],
        })
        schema = resolve_schema(df, profile_dataset(df))
        qty = schema.by_concept("quantity")
        assert qty is not None, "ضاعت الكمية الحقيقية أيضاً"
        assert qty.column_name == "qty", (
            f"الكمية أُخذت من «{qty.column_name}» — وهو حقل عدّ ثابت")

    def test_cross_validation_cannot_use_a_constant(self):
        """a×b≈c تصدُق أحياناً على ثابت بالصدفة (سعر ثابت × كمية = إجمالي).
        نمنعه قبل أن يبدأ لا بعده."""
        from phoenix.semantic import cross_validate_qty_price_total
        n = 60
        price = 25.0
        df = pl.DataFrame({
            "سعر_ثابت": [price] * n,
            "الكمية": [float(i + 1) for i in range(n)],
            "الإجمالي": [price * (i + 1) for i in range(n)],
        })
        prof = profile_dataset(df)
        without = cross_validate_qty_price_total(df, prof)
        assert without, "المعادلة نفسها لا تُكتشف — الاختبار يفحص فراغاً"
        with_exclude = cross_validate_qty_price_total(
            df, prof, exclude=constant_columns(prof))
        assert with_exclude == {}, "الثابت دخل التحقق التقاطعي رغم الاستبعاد"
