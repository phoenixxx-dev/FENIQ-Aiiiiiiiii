"""اختبارات مستقلة لـIntent Quality Gate — المرحلة الأولى من Phoenix AI Layer v1.

هذا ملف جديد بالكامل، منفصل عن tests/test_engine.py — لا تعديل ولا حذف على
أي اختبار موجود سابقاً.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from phoenix.ask import RuleRouter
from phoenix.intent_quality_gate import IntentQualityGate, evaluate
from phoenix.models import ColumnProfile, DatasetProfile, SemanticColumn, SemanticSchema


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
        SemanticColumn(column_name="التاريخ", concept="date",
                       role="time", confidence=1.0),
    ])


def _col_profile(name: str, top_values: list[tuple[str, int]]) -> ColumnProfile:
    return ColumnProfile(
        name=name, position=0, inferred_type="categorical", type_confidence=1.0,
        total_count=100, null_count=0, null_pct=0.0,
        unique_count=len(top_values), unique_pct=float(len(top_values)),
        top_values=top_values,
    )


def _sales_profile_with_regions() -> DatasetProfile:
    """DatasetProfile يحتوي top_values واقعية لعمود المنطقة، تشمل «اللاذقية»."""
    return DatasetProfile(
        row_count=100, column_count=5, duplicate_row_count=0, memory_mb=0.1,
        columns=[
            _col_profile("الإجمالي", []),
            _col_profile("الكمية", []),
            _col_profile("المنطقة", [("اللاذقية", 40), ("طرطوس", 35), ("جبلة", 25)]),
            _col_profile("المندوب", [("أوس", 30), ("ليث", 28)]),
            _col_profile("التاريخ", []),
        ],
    )


@pytest.fixture
def schema() -> SemanticSchema:
    return _sales_schema()


@pytest.fixture
def profile() -> DatasetProfile:
    return _sales_profile_with_regions()


@pytest.fixture
def gate() -> IntentQualityGate:
    return IntentQualityGate()


class TestGateNoExecution:
    """يضمن أن الـGate طبقة فحص بحتة — لا SQL ولا Analytics إطلاقاً."""

    def test_gate_module_does_not_import_analytics_engine(self):
        """يفحص أسطر import الفعلية فقط، لا أي ذكر بالتعليقات/التوثيق."""
        import phoenix.intent_quality_gate as gate_module
        source = Path(gate_module.__file__).read_text(encoding="utf-8")
        import_lines = [ln for ln in source.splitlines()
                        if ln.strip().startswith(("import ", "from "))]
        assert not any("analytics" in ln.lower() for ln in import_lines)
        assert not hasattr(gate_module, "AnalyticsEngine")


class TestProceed:
    def test_direct_question_proceeds(self, gate, schema):
        """سؤال واضح لا إشارات مُهمَلة ولا اشتباه كيان -> proceed."""
        r = gate.evaluate("كم إجمالي المبيعات؟", schema)
        assert r.tool_call.tool == "aggregate"
        assert r.proceed is True
        assert r.escalate is False
        assert r.dropped_signals == ()
        assert r.suspicion_score == 0

    def test_ranking_with_dimension_proceeds(self, gate, schema):
        r = gate.evaluate("أعلى المناطق مبيعات", schema)
        assert r.tool_call.tool == "top_n"
        assert r.tool_call.arguments["dimension"] == "region"
        assert r.proceed is True
        assert r.escalate is False


class TestUnknown:
    def test_unresolved_comparison_is_unknown_and_escalates(self, gate, schema):
        """مقارنة بلا شهرين كافيين -> resolve_intent يرجع unknown مباشرة."""
        r = gate.evaluate("قارن المبيعات", schema)
        assert r.tool_call.tool == "unknown"
        assert r.escalate is True
        assert r.proceed is False
        assert "tool=unknown" in r.reason


class TestDroppedSignal:
    def test_sales_by_region_drops_dimension_signal(self, gate, schema):
        """المبيعات والمناطق: dimension=region يُكتشف لكن aggregate(sum)
        لا يمثّله بأي مكان بالـarguments -> dropped signal -> escalate."""
        r = gate.evaluate("المبيعات والمناطق", schema)
        assert r.tool_call.tool == "aggregate"
        assert "dimension" not in r.tool_call.arguments
        assert r.signals.dimension == "region"
        assert "dimension" in r.dropped_signals
        assert r.escalate is True
        assert r.proceed is False

    def test_dropped_signal_absent_when_dimension_used(self, gate, schema):
        """نفس الإشارة (dimension) لكن مُمثَّلة فعلياً بـtop_n -> لا dropped."""
        r = gate.evaluate("أعلى المناطق مبيعات", schema)
        assert "dimension" not in r.dropped_signals


class TestIgnoredEntity:
    def test_laziqiyah_matches_top_values_and_is_ignored(self, gate, schema, profile):
        """قديش مبيعات اللاذقية؟: «اللاذقية» موجودة فعلياً بـtop_values لعمود
        المنطقة، ولا توجد أي أداة فلترة حالياً تستخدمها -> suspicion."""
        r = gate.evaluate("قديش مبيعات اللاذقية؟", schema, profile=profile)
        assert "اللاذقية" in r.ignored_entities
        assert r.suspicion_score >= 1
        assert r.escalate is True
        assert r.proceed is False

    def test_without_profile_no_ignored_entity_check(self, gate, schema):
        """بلا DatasetProfile (غير متاح)، فحص الكيانات لا يُنفَّذ إطلاقاً —
        لا false escalate بسبب غياب البيانات."""
        r = gate.evaluate("قديش مبيعات اللاذقية؟", schema, profile=None)
        assert r.ignored_entities == ()
        assert r.suspicion_score == 0


class TestUsedEntityIsNotSuspicious:
    def test_entity_literally_present_in_arguments_is_not_flagged(self, gate, schema, profile):
        """لو كيان مطابق top_values استُخدم فعلياً بأي مكان بالـToolCall
        (محاكاة قدرة فلترة مستقبلية عبر router مخصّص للاختبار)، يجب ألا
        يُحتسب كـsuspicion."""

        class _FakeFilteredRouter(RuleRouter):
            def resolve_intent(self, signals, schema):
                call = super().resolve_intent(signals, schema)
                # محاكاة: لو أداة فلترة كانت موجودة، الكيان كان سيظهر حرفياً
                # بالـarguments. نحاكي هذا الشكل هنا فقط لاختبار منطق الاستثناء.
                new_args = dict(call.arguments)
                new_args["filter_value"] = "اللاذقية"
                return call.model_copy(update={"arguments": new_args})

        fake_gate = IntentQualityGate(router=_FakeFilteredRouter())
        r = fake_gate.evaluate("قديش مبيعات اللاذقية؟", schema, profile=profile)
        assert "اللاذقية" not in r.ignored_entities
        assert r.suspicion_score == 0


class TestNoFalsePositive:
    def test_unrelated_question_with_no_entity_mention_has_no_suspicion(self, gate, schema, profile):
        r = gate.evaluate("كم إجمالي المبيعات؟", schema, profile=profile)
        assert r.ignored_entities == ()
        assert r.suspicion_score == 0

    def test_region_word_without_specific_value_has_no_entity_suspicion(self, gate, schema, profile):
        """«المنطقة» كلمة مفهوم عامة، ليست قيمة كيان فعلية بـtop_values —
        يجب ألا تُطابَق كـentity (تُعالَج كـdropped signal بمسار منفصل)."""
        r = gate.evaluate("المبيعات والمناطق", schema, profile=profile)
        assert r.ignored_entities == ()
        assert r.suspicion_score == 0
        # بالمقابل، تُعلَّم كـdropped signal (فحص مختلف تماماً)
        assert "dimension" in r.dropped_signals


class TestNoConfidenceField:
    def test_gate_result_has_no_numeric_confidence_field(self, gate, schema):
        """التكليف صريح: لا نضيف confidence رقمياً بهذه المرحلة."""
        r = gate.evaluate("كم إجمالي المبيعات؟", schema)
        assert not hasattr(r, "confidence")


class TestModuleLevelFunction:
    def test_evaluate_function_matches_class_wrapper(self, schema):
        """evaluate() المستقلة تعطي نفس نتيجة IntentQualityGate().evaluate()."""
        r1 = evaluate("كم إجمالي المبيعات؟", schema)
        r2 = IntentQualityGate().evaluate("كم إجمالي المبيعات؟", schema)
        assert r1.tool_call.tool == r2.tool_call.tool
        assert r1.proceed == r2.proceed
