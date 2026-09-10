"""ملفات خصومة حقيقية — حارس انحدار للأساس.

هذه ملفات صعبة قدّمها المستخدم لكسر المحرك، وكل تأكيد أدناه يقابل عيباً
حقيقياً ظهر عليها وأُصلح. بلا هذا الملف يمكن أن ترجع العيوب بصمت مع أي
تعديل لاحق على القاموس أو المنطق الدلالي.

الخطة §12.2 تفرض ملفات حقيقية في مجموعة الاختبار — «ملفات مصطنعة = مفاجآت
قاتلة لاحقاً».
"""
from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from phoenix import pipeline

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "adversarial"
ALL_FILES = sorted(p.name for p in FIX.iterdir() if p.is_file())


def run(name: str, tmp_path: Path):
    return pipeline.process(FIX / name, run_dir=tmp_path / name)


def concept_of(schema, column: str) -> str | None:
    col = next((c for c in schema.columns if c.column_name == column), None)
    return col.concept if col else "<لا يوجد عمود>"


class TestNoneOfThemCrash:
    @pytest.mark.parametrize("name", ALL_FILES)
    def test_file_processes_end_to_end(self, name, tmp_path):
        """الحد الأدنى: لا انهيار، ومخرجات كاملة لكل ملف."""
        run_result = run(name, tmp_path)
        assert run_result.processed_path.exists()
        assert run_result.schema.columns, f"{name}: schema فارغة"
        assert pl.read_parquet(run_result.processed_path).height > 0


class TestAmbiguousMoneyIsNotGuessed:
    def test_bare_sales_column_is_left_unresolved(self, tmp_path):
        """«Sales» في ملف مخزون تعني كمية مباعة لا مبلغاً.

        العيب الأصلي: القاموس كان يطابقها بـtotal_amount بثقة عالية، فتُجمَع
        قطعٌ وكأنها ليرات. الحارس يتركها بلا مفهوم عندما لا يوجد أي عمود
        عملة آخر يدعم التفسير المالي.
        """
        r = run("02_inventory_paradox.csv", tmp_path)
        assert concept_of(r.schema, "Sales") is None
        col = next(c for c in r.schema.columns if c.column_name == "Sales")
        assert col.confidence < 0.7, "ثقة عالية على عمود غامض = خطأ بثقة"
        assert col.needs_review, "العمود الغامض يجب أن يُعرض للمستخدم"

    def test_stock_columns_are_units_not_money(self, tmp_path):
        r = run("02_inventory_paradox.csv", tmp_path)
        assert concept_of(r.schema, "OpeningStock") == "opening_stock"
        assert concept_of(r.schema, "ExpectedClosing") == "closing_stock"
        assert concept_of(r.schema, "Purchases") == "purchases"

    def test_opening_debt_is_money_not_stock(self, tmp_path):
        """انحدار وقعنا فيه فعلاً: إضافة المرادف العام «opening» جعلت
        OpeningDebt (مبلغ) تُصنَّف opening_stock (قطع)."""
        r = run("03_receivables.csv", tmp_path)
        assert concept_of(r.schema, "OpeningDebt") == "outstanding_amount"
        assert concept_of(r.schema, "Payments") == "collected_amount"


class TestEntityMergeMatchesGroundTruth:
    """الملف يحمل حقيقته: CanonicalID يقول أي الأسماء هي نفس المنتج فعلاً."""

    def test_spacing_case_and_hyphen_variants_collapse(self, tmp_path):
        """قبل الإصلاح: 0/7. الشرطات والمسافات كانت تمنع التجميع تماماً."""
        r = run("05_product_identity.json", tmp_path)
        df = pl.read_parquet(r.processed_path)

        names = dict(zip(df["RowID"].to_list(), df["RawName"].to_list()))
        # P001: "Panadol Extra 500mg" / "PANADOL EXTRA 500 MG" / "Panadol-Extra 500mg"
        assert names["D001"] == names["D002"] == names["D003"]
        # P003: يختلفان بالمسافات فقط
        assert names["D006"] == names["D007"]

    def test_unit_notation_variants_are_a_known_limit_not_a_silent_error(self, tmp_path):
        """«Augmentin 1g» و«Augmentin 1000 mg» نفس المنتج في الواقع.

        المحرك لا يوحّدهما: التوحيد النصي لا يفهم أن 1g = 1000mg، وتوسيع
        العتبة لالتقاطهما كان سيدمج منتجات مختلفة فعلاً. نثبّت السلوك هنا
        عمداً — حتى إن تغيّر يوماً يكون تغييراً واعياً لا مفاجأة، والمستخدم
        يملك أصلاً واجهة تصحيح لدمجهما بنفسه.
        """
        r = run("05_product_identity.json", tmp_path)
        df = pl.read_parquet(r.processed_path)
        names = dict(zip(df["RowID"].to_list(), df["RawName"].to_list()))
        assert names["D004"] != names["D005"]

        # ومع ذلك: 6 من 7 صفوف مجمَّعة صحيحاً — لا انحدار عن هذا الحد
        grouped = df.group_by("CanonicalID").agg(pl.col("RawName").n_unique().alias("n"))
        assert grouped["n"].sum() <= 4, "تراجع مستوى التجميع عمّا كان"


class TestIrregularJson:
    def test_header_fields_and_split_columns_are_handled_and_announced(self, tmp_path):
        """JSON من نظام غير منتظم: حقول رأس عامة + نفس الحقل بمفتاحين.

        الخطر الحقيقي هنا صامت: انقسام «qty»/«quantity» على عمودين يجعل
        الإجمالي ناقصاً بلا أي رسالة خطأ.
        """
        r = run("08_irregular_json.json", tmp_path)
        df = pl.read_parquet(r.processed_path)

        assert "warehouse" in df.columns and "period" in df.columns
        assert "quantity" not in df.columns, "العمودان لم يُدمجا"
        # A102 وحده كُتبت كميته تحت المفتاح الآخر — لو فشل الدمج لبقيت فارغة
        row = df.filter(pl.col("sku") == "A102")
        assert row["qty"].item() == 30, "قيمة العمود الثاني ضاعت في الدمج"
        # A103 كميته null في الملف الأصلي فعلاً — فراغ صحيح لا عيب دمج
        assert df["qty"].null_count() == 1
        # الدمج قرار غير بديهي — يجب أن يُعلَن للمستخدم لا أن يُنفَّذ بصمت
        joined = " ".join(r.file_info.warnings)
        assert "qty" in joined and "دمج" in joined


class TestXmlIsSupported:
    def test_xml_records_are_read_as_rows(self, tmp_path):
        r = run("09_sales.xml", tmp_path)
        df = pl.read_parquet(r.processed_path)
        assert df.height >= 3
        assert concept_of(r.schema, "qty") == "quantity"
        assert concept_of(r.schema, "unit_price") == "unit_price"


class TestDerivedTotal:
    @pytest.mark.parametrize("name", ["01_messy_sales.xlsx", "06_returns.xlsx",
                                      "09_sales.xml", "08_irregular_json.json"])
    def test_missing_total_is_derived_and_marked_as_derived(self, name, tmp_path):
        """ملف بلا عمود إجمالي: نشتقّه من الكمية × السعر، ونُعلن أنه مشتقّ."""
        r = run(name, tmp_path)
        col = next((c for c in r.schema.columns if c.concept == "total_amount"), None)
        assert col is not None, f"{name}: لا يوجد إجمالي ولا مشتقّ"
        assert col.detection_method == "derived"
        df = pl.read_parquet(r.processed_path)
        assert df[col.column_name].sum() > 0


class TestEveryNumberHasEvidence:
    @pytest.mark.parametrize("name", ["01_messy_sales.xlsx", "10_BOSS_sales.xlsx",
                                      "07_rep_performance.csv"])
    def test_insights_carry_evidence_or_are_quality_notes(self, name, tmp_path):
        """قاعدة ذهبية #1: كل رقم من المحرك يحمل دليله."""
        r = run(name, tmp_path)
        for ins in r.insights:
            if ins.type == "quality":
                continue          # ملاحظات جودة تصف الملف لا تحسب رقماً
            assert ins.evidence is not None, f"{name}: اكتشاف بلا دليل — {ins.title_ar}"
