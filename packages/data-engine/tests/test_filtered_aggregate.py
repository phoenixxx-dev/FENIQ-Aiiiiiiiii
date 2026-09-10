"""اختبارات مستقلة لقدرة Data Engine الجديدة: فلترة مقياس بقيمة بُعد محددة.

قسم 10 بالمواصفة المعمارية — القدرة الوحيدة المؤكَّدة الناقصة فعلياً بالكود.
إضافتان فقط، بلا تعديل على أي شيء موجود سابقاً:
- AnalyticsEngine.aggregate_filtered() في analytics.py
- ToolExecutor._t_filtered_aggregate() في ask.py

ملف جديد بالكامل، منفصل عن tests/test_engine.py — لا تعديل ولا حذف على أي
اختبار موجود سابقاً. يستخدم نفس fixture الحقيقية (run) الموجودة أصلاً بـ
test_engine.py عبر نفس آلية process() لتفادي تكرار منطق بناء البيانات.
"""
from __future__ import annotations

import subprocess
import sys
from datetime import date
from pathlib import Path

import polars as pl
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from phoenix.ask import AskPhoenix, ToolExecutor
from phoenix.models import ToolCall
from phoenix.pipeline import process
from phoenix.tool_call_validator import ToolCallValidator

FIXTURE = ROOT / "fixtures" / "sales_ar_messy.xlsx"


@pytest.fixture(scope="session", autouse=True)
def ensure_fixture():
    if not FIXTURE.exists():
        subprocess.run([sys.executable, str(ROOT / "tools" / "make_fixture.py")], check=True)


@pytest.fixture(scope="session")
def run(tmp_path_factory):
    return process(FIXTURE, run_dir=tmp_path_factory.mktemp("run"))


def _a_real_region(run) -> str:
    """يلتقط اسم منطقة حقيقية موجودة فعلاً بالبيانات — بلا افتراض قيمة معيّنة."""
    eng = run.engine()
    try:
        rows, _ = eng.top_n("total_amount", "region", n=1)
        assert rows, "لا توجد بيانات منطقة بالفكستشر — لا يمكن اختبار الفلترة"
        return rows[0]["label"]
    finally:
        eng.close()


# ============================================================ AnalyticsEngine
class TestAggregateFiltered:
    def test_matches_independent_calculation(self, run):
        """الرقم المفلتر يجب أن يطابق حساب Polars المستقل لنفس القيمة تماماً."""
        region = _a_real_region(run)
        eng = run.engine()
        try:
            result = eng.aggregate_filtered("total_amount", "region", region, "sum")
            df = pl.read_parquet(run.processed_path)
            expected = df.filter(pl.col("المنطقه") == region)["الاجمالي"].sum()
            assert abs(result.value - expected) < 0.01
        finally:
            eng.close()

    def test_carries_evidence_with_filter_applied(self, run):
        region = _a_real_region(run)
        eng = run.engine()
        try:
            result = eng.aggregate_filtered("total_amount", "region", region, "sum")
            assert result.evidence.filters_applied
            assert result.evidence.filters_applied[0]["value"] == region
            assert result.evidence.rows_in_scope > 0
            assert result.evidence.rows_in_scope < result.evidence.rows_total
        finally:
            eng.close()

    def test_nonexistent_value_returns_zero_rows_not_error(self, run):
        """قيمة غير موجودة بالبيانات -> rows_in_scope=0، لا استثناء ولا تخمين."""
        eng = run.engine()
        try:
            result = eng.aggregate_filtered("total_amount", "region", "مدينة_غير_موجودة_إطلاقاً")
            assert result.evidence.rows_in_scope == 0
            assert result.value == 0.0
        finally:
            eng.close()

    def test_avg_aggregation_also_supported(self, run):
        region = _a_real_region(run)
        eng = run.engine()
        try:
            result = eng.aggregate_filtered("total_amount", "region", region, "avg")
            df = pl.read_parquet(run.processed_path)
            expected = df.filter(pl.col("المنطقه") == region)["الاجمالي"].mean()
            assert abs(result.value - expected) < 0.01
        finally:
            eng.close()


# ============================================================== ToolExecutor
class TestFilteredAggregateTool:
    def test_answer_contains_the_engine_number(self, run):
        region = _a_real_region(run)
        eng = run.engine()
        try:
            expected = eng.aggregate_filtered("total_amount", "region", region).value
            executor = ToolExecutor(eng, run.schema)
            call = ToolCall(tool="filtered_aggregate",
                            arguments={"measure": "total_amount", "dimension": "region",
                                       "value": region})
            answer = executor.execute(call, f"كم مبيعات {region}؟")
            assert answer.metrics[0].value == expected
            assert region in answer.answer_ar
        finally:
            eng.close()

    def test_zero_rows_is_stated_explicitly_as_zero(self, run):
        eng = run.engine()
        try:
            executor = ToolExecutor(eng, run.schema)
            call = ToolCall(tool="filtered_aggregate",
                            arguments={"measure": "total_amount", "dimension": "region",
                                       "value": "مدينة_غير_موجودة_إطلاقاً"})
            answer = executor.execute(call, "سؤال تجريبي")
            assert "0" in answer.answer_ar or "لا يوجد أي صف" in answer.answer_ar
        finally:
            eng.close()

    def test_rule_router_never_produces_this_tool(self, run):
        """تأكيد أن RuleRouter لم يُعدَّل — لا مسار حتمي اليوم يصل لهذه الأداة
        إلا عبر LLM مستقبلي يمر عبر Validator (قسم 11: RuleRouter بلا تعديل)."""
        eng = run.engine()
        try:
            p = AskPhoenix(eng, run.schema)
            for q in p.suggested_questions() + ["كم مبيعات اللاذقية؟", "قديش مبيعات دمشق"]:
                assert p.router.route(q, run.schema).tool != "filtered_aggregate"
        finally:
            eng.close()


# ================================================================ Validator
class TestFilteredAggregateValidation:
    def test_well_formed_call_is_valid(self, run):
        region = _a_real_region(run)
        call = ToolCall(tool="filtered_aggregate",
                        arguments={"measure": "total_amount", "dimension": "region", "value": region})
        r = ToolCallValidator().validate(call, run.schema)
        assert r.valid is True, r.reason

    def test_missing_value_key_is_rejected(self, run):
        call = ToolCall(tool="filtered_aggregate",
                        arguments={"measure": "total_amount", "dimension": "region"})
        r = ToolCallValidator().validate(call, run.schema)
        assert r.valid is False
        assert r.rejected_gate == "invalid_arguments"

    def test_non_string_value_is_rejected(self, run):
        call = ToolCall(tool="filtered_aggregate",
                        arguments={"measure": "total_amount", "dimension": "region", "value": 123})
        r = ToolCallValidator().validate(call, run.schema)
        assert r.valid is False
        assert r.rejected_gate == "invalid_arguments"

    def test_dimension_used_as_measure_still_rejected_as_invented_metric(self, run):
        call = ToolCall(tool="filtered_aggregate",
                        arguments={"measure": "region", "dimension": "region", "value": "x"})
        r = ToolCallValidator().validate(call, run.schema)
        assert r.valid is False
        assert r.rejected_gate == "invented_metric"

    def test_generic_filter_key_on_other_tools_still_rejected(self, run):
        """التأكد أن البوابة الرابعة ما زالت تمنع الفلترة العشوائية على أدوات
        أخرى — الفلترة الصريحة مسموحة فقط عبر filtered_aggregate بعقدها."""
        call = ToolCall(tool="aggregate",
                        arguments={"measure": "total_amount", "filter": {"region": "x"}})
        r = ToolCallValidator().validate(call, run.schema)
        assert r.valid is False
        assert r.rejected_gate == "unsupported_filter"


# ==================================================== compare_periods مفلترة
# قسم 10 بالمواصفة: "filtered comparisons مبنية فوق نفس القدرة أعلاه —
# compare_periods الحالية يمكن تمديدها (لا إعادة بنائها)".

class TestCompareperiodsFiltered:
    def test_matches_independent_calculation(self, run):
        """مقارنة شباط/حزيران مقيّدة بمنطقة حقيقية واحدة، يجب أن تطابق Polars."""
        region = _a_real_region(run)
        eng = run.engine()
        try:
            r = eng.compare_periods(
                "total_amount", ("2026-02-01", "2026-02-28"), ("2026-06-01", "2026-06-30"),
                filter_concept="region", filter_value=region,
            )
            df = pl.read_parquet(run.processed_path)
            sub = df.filter(pl.col("المنطقه") == region)
            sub = sub.with_columns(pl.col("التاريخ").cast(pl.Date))
            expected_v1 = sub.filter(
                (pl.col("التاريخ") >= date(2026, 2, 1)) & (pl.col("التاريخ") <= date(2026, 2, 28))
            )["الاجمالي"].sum() or 0.0
            expected_v2 = sub.filter(
                (pl.col("التاريخ") >= date(2026, 6, 1)) & (pl.col("التاريخ") <= date(2026, 6, 30))
            )["الاجمالي"].sum() or 0.0
            assert abs(r["period_1"]["value"] - expected_v1) < 0.01
            assert abs(r["period_2"]["value"] - expected_v2) < 0.01
        finally:
            eng.close()

    def test_unfiltered_call_unchanged(self, run):
        """بلا filter_concept، السلوك يجب أن يبقى مطابقاً تماماً للسابق (بلا فلتر)."""
        eng = run.engine()
        try:
            r = eng.compare_periods("total_amount", ("2026-02-01", "2026-02-28"),
                                    ("2026-06-01", "2026-06-30"))
            assert r["evidence"].filters_applied == []
        finally:
            eng.close()

    def test_tool_executor_produces_filtered_answer_mentioning_value(self, run):
        region = _a_real_region(run)
        eng = run.engine()
        try:
            executor = ToolExecutor(eng, run.schema)
            call = ToolCall(tool="compare_periods", arguments={
                "measure": "total_amount", "months": [2, 6],
                "dimension": "region", "value": region,
            })
            answer = executor.execute(call, f"قارن مبيعات {region} بين شباط وحزيران")
            assert region in answer.answer_ar
            assert answer.confidence == 1.0
        finally:
            eng.close()

    def test_tool_executor_rejects_dimension_without_value(self, run):
        """dimension بلا value (أو العكس) خطأ منطقي — يجب أن يُرفض بوضوح لا أن
        يُنفَّذ بصمت وكأنه بلا فلتر."""
        eng = run.engine()
        try:
            executor = ToolExecutor(eng, run.schema)
            call = ToolCall(tool="compare_periods", arguments={
                "measure": "total_amount", "months": [2, 6], "dimension": "region",
            })
            answer = executor.execute(call, "سؤال تجريبي")
            assert answer.confidence == 0.0
        finally:
            eng.close()


class TestCompareperiodsFilteredValidation:
    def test_well_formed_filtered_call_is_valid(self, run):
        region = _a_real_region(run)
        call = ToolCall(tool="compare_periods", arguments={
            "measure": "total_amount", "months": [2, 6], "dimension": "region", "value": region,
        })
        r = ToolCallValidator().validate(call, run.schema)
        assert r.valid is True, r.reason

    def test_unfiltered_call_still_valid(self, run):
        """التأكد أن التمديد لم يكسر الاستدعاء القديم بلا فلتر."""
        call = ToolCall(tool="compare_periods",
                        arguments={"measure": "total_amount", "months": [2, 6]})
        r = ToolCallValidator().validate(call, run.schema)
        assert r.valid is True, r.reason

    def test_dimension_without_value_is_rejected(self, run):
        call = ToolCall(tool="compare_periods", arguments={
            "measure": "total_amount", "months": [2, 6], "dimension": "region",
        })
        r = ToolCallValidator().validate(call, run.schema)
        assert r.valid is False
        assert r.rejected_gate == "invalid_arguments"

    def test_value_without_dimension_is_rejected(self, run):
        call = ToolCall(tool="compare_periods", arguments={
            "measure": "total_amount", "months": [2, 6], "value": "الرياض",
        })
        r = ToolCallValidator().validate(call, run.schema)
        assert r.valid is False
        assert r.rejected_gate == "invalid_arguments"

    def test_invented_dimension_for_comparison_is_rejected(self, run):
        call = ToolCall(tool="compare_periods", arguments={
            "measure": "total_amount", "months": [2, 6],
            "dimension": "total_amount", "value": "x",
        })
        r = ToolCallValidator().validate(call, run.schema)
        assert r.valid is False
        assert r.rejected_gate == "invented_metric"
