"""لقطةُ الجرد ليست سجلَّ مبيعات (ق-49).

كتالوج مستودع حقيقي — 655 صنفاً من برنامج الأمين — كان يُقرأ على أنه مبيعات:
«إجمالي المبيعات 245,140,775» على ملفٍ لا يحوي مبيعةً واحدة. الرقم صحيح
حسابياً (رصيد × سعر) والجملة كاذبة تماماً. ومن يبني قراراً على تلك الجملة يظن
أنه باع ربع مليار، وهو في الحقيقة يملك بضاعةً بهذه القيمة على الرف.

الفرق بين الملفّين ليس في عمود موجود بل في أعمدة **غائبة**: لا تاريخ، ولا
عميل، ولا رقم فاتورة. لكن الغياب وحده لا يكفي — ملف مبيعات بسيط بلا تاريخ
يستوفيه أيضاً. فيلزم دليلٌ موجب على أن الملف كتالوج: وحدة قياس، أو مستودع، أو
صلاحية، أو صفةُ صنفٍ ثابتة.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from phoenix import charts, pipeline  # noqa: E402
from phoenix.ask import AskPhoenix, suggest_questions  # noqa: E402

REAL = Path(__file__).resolve().parents[1] / "fixtures" / "real" / "feniqsync_catalog.json"


@pytest.fixture(scope="module")
def catalog(tmp_path_factory):
    return pipeline.process(REAL, run_dir=tmp_path_factory.mktemp("cat") / "run")


class TestTheRealCatalogIsUnderstoodAsStock:
    def test_the_domain_is_inventory(self, catalog):
        assert catalog.schema.domain == "inventory"

    def test_the_quantity_column_means_balance_not_sales(self, catalog):
        qty = next(c for c in catalog.schema.columns if c.column_name == "qty")
        assert qty.concept == "stock_qty", "الرصيد ما زال يُقرأ «كمية مباعة»"

    def test_the_feniqsync_columns_are_understood(self, catalog):
        got = {c.column_name: c.concept for c in catalog.schema.columns}
        expected = {
            "item_id": "product_code",      # GUID الأمين — مفتاح مستقر
            "name_raw": "product_name",     # الاسم كما كتبه المحاسب
            "name_norm": "name_variant",    # أدوات مطابقة لا أسماء للعرض
            "name_core": "name_variant",
            "aliases": "name_variant",
            "unit_raw": "unit",
            "form": "dosage_form",
            "strength": "strength",
            "price": "unit_price",
        }
        wrong = {k: (got.get(k), v) for k, v in expected.items() if got.get(k) != v}
        assert wrong == {}, f"أعمدة لم تُفهم (الفعلي، المتوقع): {wrong}"

    def test_no_sales_wording_anywhere_the_user_reads(self, catalog):
        """أهم فحص هنا: كلمة «مبيعات» على ملف بلا مبيعات."""
        engine = catalog.engine()
        try:
            texts = [k["label_ar"] for k in charts.build_kpis(engine)]
            texts += [c.title_ar for c in charts.suggest_charts(engine)]
            texts += [i.title_ar for i in catalog.insights]
            texts += [i.description_ar for i in catalog.insights]
            texts += suggest_questions(catalog.schema)
            ask = AskPhoenix(engine=engine, schema=catalog.schema)
            texts += [ask.ask(q).answer_ar for q in suggest_questions(catalog.schema)]
        finally:
            engine.close()
        offenders = [t for t in texts if "مبيع" in t]
        assert offenders == [], f"لغة مبيعات على ملف جرد: {offenders[:3]}"

    def test_the_headline_number_is_named_stock_value(self, catalog):
        engine = catalog.engine()
        try:
            kpis = {k["key"]: k["label_ar"] for k in charts.build_kpis(engine)}
        finally:
            engine.close()
        assert kpis.get("total_amount") == "قيمة المخزون", kpis


class TestTheQuestionsAnOwnerActuallyAsks:
    @pytest.mark.parametrize("question", [
        "كم قيمة المخزون؟",
        "كم إجمالي الرصيد؟",
        "شو أغلى 5 أصناف؟",
        "شو أعلى 5 أصناف قيمةً؟",
    ])
    def test_each_question_produces_a_number(self, catalog, question):
        engine = catalog.engine()
        try:
            answer = AskPhoenix(engine=engine, schema=catalog.schema).ask(question)
        finally:
            engine.close()
        assert "لم أفهم" not in answer.answer_ar, answer.answer_ar
        assert any(ch.isdigit() for ch in answer.answer_ar), answer.answer_ar

    def test_every_suggested_question_is_answerable(self, catalog):
        """الاقتراح الذي يعطي «لم أفهم» عند أول ضغطة أسوأ من لا اقتراح."""
        engine = catalog.engine()
        try:
            ask = AskPhoenix(engine=engine, schema=catalog.schema)
            qs = suggest_questions(catalog.schema)
            assert qs, "لا اقتراحات أصلاً"
            bad = [q for q in qs if "لم أفهم" in ask.ask(q).answer_ar]
        finally:
            engine.close()
        assert bad == [], f"اقتراحات لا يفهمها الموجّه: {bad}"

    def test_prices_are_ranked_without_a_meaningless_share(self, catalog):
        """«هذا الصنف يمثّل 1.2% من الأسعار» جملة فارغة: مجموع أسعار الوحدة
        رقمٌ لا وجود له في الواقع، فلا نسبة منه."""
        engine = catalog.engine()
        try:
            answer = AskPhoenix(engine=engine, schema=catalog.schema).ask("شو أغلى 5 أصناف؟")
        finally:
            engine.close()
        assert "%" not in answer.answer_ar, answer.answer_ar


class TestTheInsightsAnOwnerActuallyNeeds:
    def test_dead_stock_is_reported_with_the_true_count(self, catalog):
        found = [i for i in catalog.insights if "بلا رصيد" in i.title_ar]
        assert found, [i.title_ar for i in catalog.insights]
        assert "242" in found[0].title_ar, found[0].title_ar

    def test_priceless_items_are_reported(self, catalog):
        found = [i for i in catalog.insights if "بلا سعر" in i.title_ar]
        assert found and "24" in found[0].title_ar, [i.title_ar for i in catalog.insights]

    def test_the_pareto_line_names_stock_value_not_revenue(self, catalog):
        found = [i for i in catalog.insights if "80%" in i.title_ar]
        assert found, [i.title_ar for i in catalog.insights]
        assert "قيمة المخزون" in found[0].title_ar, found[0].title_ar


class TestASalesFileIsNotMistakenForStock:
    """الحدّ المقابل. بلا هذا يصير الحلّ «كل ملف جرد» — وهو نفس العيب مقلوباً."""

    def test_a_plain_sales_file_stays_generic(self, tmp_path):
        src = tmp_path / "sales.csv"
        rows = ["الصنف,الكمية,سعر الوحدة"]
        rows += [f"صنف {i},{i % 7 + 1},{100 + i}" for i in range(30)]
        src.write_text("\n".join(rows), encoding="utf-8")
        run = pipeline.process(src, run_dir=tmp_path / "run")
        assert run.schema.domain != "inventory"
        assert run.schema.by_concept("quantity") is not None, "الكمية صارت رصيداً بلا دليل"

    def test_a_file_with_dates_is_never_a_snapshot(self, tmp_path):
        src = tmp_path / "dated.csv"
        rows = ["التاريخ,الصنف,الوحدة,الكمية,سعر الوحدة"]
        rows += [f"2026-0{i % 8 + 1}-05,صنف {i},علبة,{i % 7 + 1},{100 + i}" for i in range(30)]
        src.write_text("\n".join(rows), encoding="utf-8")
        run = pipeline.process(src, run_dir=tmp_path / "run")
        assert run.schema.domain != "inventory", "ملف فيه تاريخ ليس لقطة"

    def test_a_sku_alone_is_not_catalog_evidence(self, tmp_path):
        """رمز الصنف موجود في كل سطر بيع أيضاً — فليس دليلاً على شيء."""
        src = tmp_path / "sku.json"
        src.write_text(
            "[" + ",".join(
                '{"sku":"A%d","qty":%d,"price":10}' % (i, i % 5 + 1) for i in range(30)
            ) + "]", encoding="utf-8")
        run = pipeline.process(src, run_dir=tmp_path / "run")
        assert run.schema.domain != "inventory"
