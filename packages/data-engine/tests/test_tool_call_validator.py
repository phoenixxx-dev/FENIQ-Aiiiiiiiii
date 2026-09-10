"""اختبارات مستقلة لـToolCall Validator — Phoenix AI Layer v1، القسم 6.

ملف جديد بالكامل، منفصل عن tests/test_engine.py وtests/test_intent_quality_gate.py
— لا تعديل ولا حذف على أي اختبار موجود سابقاً.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from phoenix.models import SemanticColumn, SemanticSchema, ToolCall
from phoenix.tool_call_validator import ToolCallValidator, validate


def _sales_schema() -> SemanticSchema:
    return SemanticSchema(columns=[
        SemanticColumn(column_name="الإجمالي", concept="total_amount",
                       role="measure", confidence=1.0, unit="currency"),
        SemanticColumn(column_name="الكمية", concept="quantity",
                       role="measure", confidence=1.0),
        SemanticColumn(column_name="المنطقة", concept="region",
                       role="dimension", confidence=1.0),
        SemanticColumn(column_name="المندوب", concept="salesperson",
                       role="dimension", confidence=1.0),
        SemanticColumn(column_name="رقم الفاتورة", concept="invoice_no",
                       role="identifier", confidence=1.0),
        SemanticColumn(column_name="التاريخ", concept="date",
                       role="time", confidence=1.0),
    ])


@pytest.fixture
def schema() -> SemanticSchema:
    return _sales_schema()


@pytest.fixture
def validator() -> ToolCallValidator:
    return ToolCallValidator()


# ------------------------------------------------------- بوابة 1: أداة موجودة

class TestUnknownToolGate:
    def test_nonexistent_tool_is_rejected(self, validator, schema):
        r = validator.validate(ToolCall(tool="delete_everything", arguments={}), schema)
        assert r.valid is False
        assert r.rejected_gate == "unknown_tool"
        assert r.tool_call.tool == "unknown"

    def test_tool_names_are_discovered_from_actual_toolexecutor(self, validator, schema):
        """لا قائمة أدوات مكتوبة يدوياً — كل أداة موجودة فعلاً بـ_t_* تُقبل."""
        for tool in ("aggregate", "count", "top_n", "timeseries",
                     "compare_periods", "low_stock", "stagnant_items"):
            from phoenix.tool_call_validator import _discover_tool_names
            assert tool in _discover_tool_names()


class TestUnknownPassesThrough:
    def test_unknown_tool_call_passes_without_further_checks(self, validator, schema):
        """tool=unknown هو إشارة الرفض القياسية نفسها — يمر بلا فحص إضافي."""
        call = ToolCall(tool="unknown", arguments={})
        r = validator.validate(call, schema)
        assert r.valid is True
        assert r.tool_call.tool == "unknown"
        assert r.rejected_gate is None


# --------------------------------------------------- بوابة 2: concepts موجودة

class TestUnknownConceptGate:
    def test_measure_not_in_schema_is_rejected(self, validator, schema):
        call = ToolCall(tool="aggregate", arguments={"measure": "profit_margin"})
        r = validator.validate(call, schema)
        assert r.valid is False
        assert r.rejected_gate == "unknown_concept"

    def test_dimension_not_in_schema_is_rejected(self, validator, schema):
        call = ToolCall(tool="top_n", arguments={
            "measure": "total_amount", "dimension": "warehouse", "n": 5,
        })
        r = validator.validate(call, schema)
        assert r.valid is False
        assert r.rejected_gate == "unknown_concept"


# ---------------------------------------------------- بوابة 3: arguments صالحة

class TestInvalidArgumentsGate:
    def test_missing_required_key_is_rejected(self, validator, schema):
        r = validator.validate(ToolCall(tool="aggregate", arguments={}), schema)
        assert r.valid is False
        assert r.rejected_gate == "invalid_arguments"

    def test_required_concept_key_cannot_be_none(self, validator, schema):
        r = validator.validate(ToolCall(tool="aggregate", arguments={"measure": None}), schema)
        assert r.valid is False
        assert r.rejected_gate == "invalid_arguments"

    def test_unknown_extra_key_is_rejected(self, validator, schema):
        call = ToolCall(tool="aggregate", arguments={"measure": "total_amount", "bogus": 1})
        r = validator.validate(call, schema)
        assert r.valid is False
        assert r.rejected_gate == "invalid_arguments"

    def test_invalid_agg_value_is_rejected(self, validator, schema):
        call = ToolCall(tool="aggregate", arguments={"measure": "total_amount", "agg": "median"})
        r = validator.validate(call, schema)
        assert r.valid is False
        assert r.rejected_gate == "invalid_arguments"

    def test_n_out_of_range_is_rejected(self, validator, schema):
        call = ToolCall(tool="top_n", arguments={
            "measure": "total_amount", "dimension": "region", "n": 500,
        })
        r = validator.validate(call, schema)
        assert r.valid is False
        assert r.rejected_gate == "invalid_arguments"

    def test_compare_periods_needs_exactly_two_months(self, validator, schema):
        call = ToolCall(tool="compare_periods", arguments={
            "measure": "total_amount", "months": [1],
        })
        r = validator.validate(call, schema)
        assert r.valid is False
        assert r.rejected_gate == "invalid_arguments"

    def test_count_with_no_concept_is_valid(self, validator, schema):
        """count(concept=None) يعني عدّ كل الصفوف — سلوك موجود فعلياً بالكود."""
        r = validator.validate(ToolCall(tool="count", arguments={"concept": None}), schema)
        assert r.valid is True


# -------------------------------------------- بوابة 4: فلاتر غير قابلة للتنفيذ

class TestUnsupportedFilterGate:
    def test_filter_key_is_rejected_even_with_otherwise_valid_call(self, validator, schema):
        call = ToolCall(tool="aggregate", arguments={
            "measure": "total_amount",
            "filter": {"dimension": "region", "value": "اللاذقية"},
        })
        r = validator.validate(call, schema)
        assert r.valid is False
        assert r.rejected_gate == "unsupported_filter"
        assert r.tool_call.tool == "unknown"

    def test_filters_plural_key_is_also_rejected(self, validator, schema):
        call = ToolCall(tool="top_n", arguments={
            "measure": "total_amount", "dimension": "region", "n": 5,
            "filters": [{"column": "region", "op": "=", "value": "اللاذقية"}],
        })
        r = validator.validate(call, schema)
        assert r.valid is False
        assert r.rejected_gate == "unsupported_filter"


# --------------------------------------------------- بوابة 5: منع اختراع metrics

class TestInventedMetricGate:
    def test_dimension_column_used_as_measure_is_rejected(self, validator, schema):
        """«المنطقة» عمود بُعد (role=dimension)، لا يصلح كـmeasure."""
        call = ToolCall(tool="aggregate", arguments={"measure": "region"})
        r = validator.validate(call, schema)
        assert r.valid is False
        assert r.rejected_gate == "invented_metric"

    def test_measure_column_used_as_dimension_is_rejected(self, validator, schema):
        call = ToolCall(tool="top_n", arguments={
            "measure": "total_amount", "dimension": "quantity", "n": 5,
        })
        r = validator.validate(call, schema)
        assert r.valid is False
        assert r.rejected_gate == "invented_metric"

    def test_count_accepts_identifier_concept(self, validator, schema):
        """invoice_no دوره identifier — مقبول لأداة count تحديداً (_t_count)."""
        r = validator.validate(ToolCall(tool="count", arguments={"concept": "invoice_no"}), schema)
        assert r.valid is True

    def test_count_accepts_dimension_concept(self, validator, schema):
        r = validator.validate(ToolCall(tool="count", arguments={"concept": "region"}), schema)
        assert r.valid is True

    def test_count_rejects_measure_concept(self, validator, schema):
        """measure ليس بُعداً ولا معرّفاً — لا يصلح لعدّ القيم المميزة به."""
        r = validator.validate(ToolCall(tool="count", arguments={"concept": "total_amount"}), schema)
        assert r.valid is False
        assert r.rejected_gate == "invented_metric"


# ----------------------------------------------------------------- حالات نجاح

class TestValidCalls:
    @pytest.mark.parametrize("call", [
        ToolCall(tool="aggregate", arguments={"measure": "total_amount", "agg": "sum"}),
        ToolCall(tool="aggregate", arguments={"measure": "total_amount"}),
        ToolCall(tool="top_n", arguments={
            "measure": "total_amount", "dimension": "region", "n": 5, "ascending": False,
        }),
        ToolCall(tool="timeseries", arguments={"measure": "total_amount", "granularity": "month"}),
        ToolCall(tool="compare_periods", arguments={"measure": "total_amount", "months": [1, 2]}),
        ToolCall(tool="low_stock", arguments={"dimension": "region", "count_only": True}),
        ToolCall(tool="low_stock", arguments={}),
        ToolCall(tool="stagnant_items", arguments={"dimension": "region", "measure": "quantity"}),
    ])
    def test_well_formed_calls_pass(self, validator, schema, call):
        r = validator.validate(call, schema)
        assert r.valid is True, r.reason
        assert r.tool_call == call
        assert r.rejected_gate is None


class TestRejectionNeverInventsArguments:
    """عند الرفض، الناتج ToolCall(tool='unknown') فارغ تماماً — لا تصحيح صامت
    ولا محاولة 'تخمين' وسائط بديلة (قسم 6، آخر فقرة بالمواصفة)."""

    def test_rejected_call_has_empty_arguments(self, validator, schema):
        call = ToolCall(tool="aggregate", arguments={"measure": "not_a_real_concept"})
        r = validator.validate(call, schema)
        assert r.tool_call.arguments == {}


class TestModuleLevelFunctionMatchesWrapper:
    def test_validate_function_matches_class_wrapper(self, schema):
        call = ToolCall(tool="aggregate", arguments={"measure": "total_amount"})
        r1 = validate(call, schema)
        r2 = ToolCallValidator().validate(call, schema)
        assert r1.valid == r2.valid
        assert r1.tool_call == r2.tool_call


class TestValidatorNoExecution:
    """يضمن أن الـValidator طبقة فحص بحتة — لا اتصال بمحرك التحليل إطلاقاً."""

    def test_validator_module_does_not_import_analytics_engine(self):
        import phoenix.tool_call_validator as validator_module
        source = Path(validator_module.__file__).read_text(encoding="utf-8")
        import_lines = [ln for ln in source.splitlines()
                        if ln.strip().startswith(("import ", "from "))]
        assert not any("analytics" in ln.lower() for ln in import_lines)
        assert not hasattr(validator_module, "AnalyticsEngine")
