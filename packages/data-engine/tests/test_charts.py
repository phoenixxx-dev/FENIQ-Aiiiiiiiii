"""اختبارات اختيار الرسوم — بلا متصفح ولا لقطات شاشة.

الفائدة: قرار «أي رسم يناسب هذه البيانات» صار قابلاً للفحص كأي منطق آخر.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from phoenix import charts  # noqa: E402
from phoenix.charts import choose_chart  # noqa: E402
from phoenix.pipeline import process  # noqa: E402

FIXTURE = ROOT / "fixtures" / "sales_ar_messy.xlsx"


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    return process(FIXTURE, run_dir=tmp_path_factory.mktemp("charts"))


class TestChooseChart:
    """الدالة نقية — كل حالة من خطة المشروع §5.8 لها اختبار."""

    def test_time_with_many_points_is_line(self):
        assert choose_chart("time", points=24) == "line"

    def test_time_with_few_points_is_bar(self):
        assert choose_chart("time", points=5) == "bar"

    def test_time_boundary_is_bar_at_twelve(self):
        """12 نقطة بالضبط = سنة شهرية ⇒ أعمدة لا تزال أوضح."""
        assert choose_chart("time", points=12) == "bar"
        assert choose_chart("time", points=13) == "line"

    def test_small_dimension_is_horizontal_bar(self):
        """أفقي عمداً: أسماء المنتجات العربية طويلة."""
        assert choose_chart("dimension", cardinality=6) == "bar_horizontal"

    def test_top_ten_stays_horizontal_for_arabic_labels(self):
        """عيب مقروئية حقيقي شوهد على الجوال: أعلى-10 منتجات بأعمدة رأسية تُدار
        تسمياتها العربية الطويلة 90° فتصير غير مقروءة. أعلى-10 هي الحالة
        الافتراضية، فيجب أن تبقى أفقية."""
        assert choose_chart("dimension", cardinality=10) == "bar_horizontal"
        assert choose_chart("dimension", cardinality=12) == "bar_horizontal"

    def test_large_dimension_is_vertical_bar(self):
        """فوق 12 فئة لا تكفي المساحة الرأسية لأعمدة أفقية مقروءة."""
        assert choose_chart("dimension", cardinality=13) == "bar"
        assert choose_chart("dimension", cardinality=30) == "bar"

    def test_part_of_whole_small_is_donut(self):
        assert choose_chart("dimension", cardinality=4, part_of_whole=True) == "donut"

    def test_part_of_whole_too_many_slices_is_not_donut(self):
        """دائري بـ9 شرائح غير مقروء ⇒ نرفضه."""
        assert choose_chart("dimension", cardinality=9, part_of_whole=True) != "donut"

    def test_two_measures_is_scatter(self):
        assert choose_chart("measure", measures=2) == "scatter"

    def test_single_measure_is_histogram(self):
        assert choose_chart("measure", measures=1) == "histogram"

    def test_unknown_falls_back_to_table(self):
        """لا نخترع رسماً لبيانات لا نفهم شكلها — الجدول أصدق."""
        assert choose_chart("none") == "table"


class TestBuiltCharts:
    def test_suggest_charts_on_real_file(self, run):
        eng = run.engine()
        try:
            specs = charts.suggest_charts(eng)
            assert specs, "ملف مبيعات حقيقي يجب أن ينتج رسماً واحداً على الأقل"
            for s in specs:
                assert s.data, f"رسم بلا بيانات: {s.title_ar}"
                assert s.title_ar and s.reason_ar
                assert s.evidence is not None, "كل رسم يحمل دليله (قاعدة ذهبية #1)"
                assert len(s.data) <= charts.MAX_POINTS
        finally:
            eng.close()

    def test_chart_data_keys_match_axes(self, run):
        """الواجهة تقرأ x وy حرفياً — أي اختلاف يكسر الرسم صامتاً."""
        eng = run.engine()
        try:
            for s in charts.suggest_charts(eng):
                assert s.x.field in s.data[0]
                for axis in s.y:
                    assert axis.field in s.data[0]
        finally:
            eng.close()

    def test_month_period_label_has_no_time_part(self, run):
        eng = run.engine()
        try:
            spec = charts.build_timeseries_chart(eng, "total_amount", "month")
            if spec:
                assert all(len(str(p["x"])) == 7 for p in spec.data), spec.data[:3]
        finally:
            eng.close()

    def test_kpis_every_number_has_evidence(self, run):
        eng = run.engine()
        try:
            kpis = charts.build_kpis(eng)
            assert kpis
            for k in kpis:
                assert k["evidence"] is not None, f"مؤشر بلا دليل: {k['label_ar']}"
                assert k["formatted_ar"]
        finally:
            eng.close()

    def test_kpi_label_not_duplicated(self, run):
        eng = run.engine()
        try:
            labels = [k["label_ar"] for k in charts.build_kpis(eng)]
            assert "إجمالي الإجمالي" not in labels
            assert len(labels) == len(set(labels)), "مؤشرات مكررة"
        finally:
            eng.close()

    def test_no_charts_when_no_measure(self, tmp_path):
        """ملف بلا أي مقياس عددي: لا نخترع رسماً فارغاً."""
        p = tmp_path / "names.csv"
        p.write_text("الاسم,المدينة\nأحمد,اللاذقية\nليث,حمص\n", encoding="utf-8")
        r = process(p, run_dir=tmp_path / "run")
        eng = r.engine()
        try:
            assert charts.suggest_charts(eng) == []
        finally:
            eng.close()
