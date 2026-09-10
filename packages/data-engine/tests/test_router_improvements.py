"""تحسينات الموجّه الحتمي التي كشفتها حزمة التقييم.

كل اختبار هنا يحرس سؤالاً كان يُرفض أو يُفهم خطأً، وصار يُجاب حتمياً.
قيمتها العملية: كل سؤال هنا لا يحتاج LLM إطلاقاً — أرخص وأسرع وأوثق.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from phoenix.ask import RuleRouter  # noqa: E402
from phoenix.intent_quality_gate import IntentQualityGate  # noqa: E402
from phoenix.pipeline import process  # noqa: E402

FIXTURE = ROOT / "fixtures" / "sales_ar_messy.xlsx"


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    return process(FIXTURE, run_dir=tmp_path_factory.mktemp("router"))


def route(run, q: str):
    return RuleRouter().route(q, run.schema)


class TestGroupByDimension:
    def test_sales_by_region_groups_instead_of_totalling(self, run):
        """«المبيعات حسب المنطقة» كانت تُفهم إجمالياً عاماً فتضيع المقارنة
        بين المناطق — وهي بالضبط ما يسأل عنه المستخدم."""
        call = route(run, "المبيعات حسب المنطقة")
        assert call.tool == "top_n"
        assert call.arguments["dimension"] == "region"

    def test_group_by_now_proceeds_without_escalation(self, run):
        """المكسب الحقيقي: السؤال صار مفهوماً حتمياً، فلا يُصعَّد للـLLM."""
        result = IntentQualityGate().evaluate("المبيعات حسب المنطقة", run.schema)
        assert result.proceed

    def test_per_customer_phrasing(self, run):
        call = route(run, "المبيعات لكل عميل")
        assert call.tool == "top_n"
        assert call.arguments["dimension"] == "customer"

    def test_ranking_still_wins_over_plain_grouping(self, run):
        """«أعلى 3 عملاء» ترتيب صريح — يبقى بعدد 3 لا بعدد التجميع الافتراضي."""
        call = route(run, "أعلى 3 عملاء")
        assert call.tool == "top_n"
        assert call.arguments["n"] == 3


class TestGranularity:
    @pytest.mark.parametrize(
        "question,expected",
        [
            ("أعطيني المبيعات اليومية", "day"),
            ("المبيعات الأسبوعية", "week"),
            ("المبيعات حسب الشهر", "month"),
            ("كم بعنا بالسنة", "year"),
        ],
    )
    def test_granularity_is_read_from_question(self, run, question, expected):
        """كانت السلسلة الزمنية شهرية دائماً مهما قال المستخدم."""
        call = route(run, question)
        assert call.tool == "timeseries"
        assert call.arguments["granularity"] == expected


class TestComparison:
    def test_numeric_months(self, run):
        """«قارن مبيعات 5 و 6» شائع جداً، وكان يُرفض لأننا نبحث عن أسماء الشهور."""
        call = route(run, "قارن مبيعات 5 و 6")
        assert call.tool == "compare_periods"
        assert sorted(call.arguments["months"])[:2] == [5, 6]

    def test_relative_period(self, run):
        call = route(run, "قديش بعنا هالشهر مقارنة بالشهر يلي قبلو")
        assert call.tool == "compare_periods"
        assert call.arguments.get("last_two") is True

    def test_numbers_outside_comparison_are_not_months(self, run):
        """حماية من الإفراط: «أعلى 5 منتجات» رقمها عدد النتائج لا شهر مايو."""
        call = route(run, "أعلى 5 منتجات مبيعاً")
        assert call.tool == "top_n"
        assert call.arguments["n"] == 5

    def test_comparison_without_any_period_still_refuses(self, run):
        """كلمة «قارن» بلا فترة ولا إشارة نسبية تبقى مرفوضة — لا تخمين."""
        assert route(run, "قارن").tool == "unknown"


class TestLowStockDialect:
    def test_colloquial_low_stock(self, run):
        """«وين المخزون ناقص» عامية شائعة كانت تُرفض."""
        call = route(run, "وين المخزون ناقص")
        assert call.tool == "low_stock"


class TestNoRegression:
    @pytest.mark.parametrize(
        "question,tool",
        [
            ("كم إجمالي المبيعات؟", "aggregate"),
            ("كم عدد الصفوف؟", "count"),
            ("مين أكتر 5 منتجات مبيعاً؟", "top_n"),
            ("شو الأصناف الراكدة؟", "stagnant_items"),
            ("مرحبا كيفك", "unknown"),
            ("احكيلي نكتة", "unknown"),
        ],
    )
    def test_existing_behaviour_unchanged(self, run, question, tool):
        assert route(run, question).tool == tool
