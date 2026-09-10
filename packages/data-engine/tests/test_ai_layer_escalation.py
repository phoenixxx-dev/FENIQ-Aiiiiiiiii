"""اختبارات مستقلة لطبقة تصعيد الذكاء الاصطناعي — Phoenix AI Layer v1.

يغطي: LLMIntentResolver، AIQuestionRouter، GatedRouter (نقطة الدمج الوحيدة).

قاعدة صارمة: **صفر اتصال شبكة حقيقي بهذا الملف**. كل اختبار يحقن `call_fn`
وهمياً بدل الاتصال الفعلي بـGemini — تماماً كما تعمل بقية حزمة الاختبار
بالمشروع بلا أي خدمة خارجية (انظر رأس tests/test_engine.py).

ملف جديد بالكامل، منفصل عن كل ملفات الاختبار السابقة — لا تعديل ولا حذف على
أي اختبار موجود.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from phoenix.ai_question_router import AIQuestionRouter
from phoenix.gated_router import GatedRouter, _ENV_FLAG
from phoenix.intent_quality_gate import IntentQualityGate
from phoenix.llm_intent_resolver import LLMIntentResolver, build_llm_package
from phoenix.models import ColumnProfile, DatasetProfile, SemanticColumn, SemanticSchema
from phoenix.tool_call_validator import ToolCallValidator


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


@pytest.fixture
def schema() -> SemanticSchema:
    return _sales_schema()


def _json_call_fn(payload: dict):
    """يبني call_fn وهمياً يرجع نفس الـpayload كنص JSON، بلا أي شبكة."""
    return lambda prompt: json.dumps(payload, ensure_ascii=False)


def _raising_call_fn(exc: Exception = RuntimeError("network down")):
    def _fn(prompt: str) -> str:
        raise exc
    return _fn


def _never_call_fn():
    """يفشل الاختبار فوراً لو استُدعي — يثبت أن LLM لم يُستدعَ إطلاقاً."""
    def _fn(prompt: str) -> str:
        raise AssertionError("call_fn استُدعي رغم أنه لا يجب استدعاء LLM بهذا المسار")
    return _fn


# =========================================================== LLMIntentResolver

class TestBuildLlmPackage:
    def test_package_has_no_actual_data_rows(self, schema):
        """الحزمة يجب أن تحتوي بنية الـschema فقط — لا قيم بيانات فعلية."""
        gate = IntentQualityGate().evaluate("قارن المبيعات", schema)
        pkg = build_llm_package("قارن المبيعات", schema, gate)
        assert pkg["question"] == "قارن المبيعات"
        assert all("column_name" in c and "concept" in c for c in pkg["schema"])
        assert "gate_diagnosis" in pkg and "available_tools" in pkg

    def test_available_tools_match_actual_toolexecutor(self, schema):
        gate = IntentQualityGate().evaluate("قارن المبيعات", schema)
        pkg = build_llm_package("قارن المبيعات", schema, gate)
        names = {t["tool"] for t in pkg["available_tools"]}
        assert {"aggregate", "top_n", "compare_periods", "filtered_aggregate"} <= names

    def test_gate_diagnosis_reflects_dropped_signal(self, schema):
        gate = IntentQualityGate().evaluate("المبيعات والمناطق", schema)
        pkg = build_llm_package("المبيعات والمناطق", schema, gate)
        assert "dimension" in pkg["gate_diagnosis"]["dropped_signals"]


class TestLlmIntentResolverHappyPath:
    def test_valid_json_becomes_tool_call(self, schema):
        resolver = LLMIntentResolver(call_fn=_json_call_fn(
            {"tool": "top_n", "arguments": {"measure": "total_amount", "dimension": "region",
                                            "n": 5, "ascending": False}}))
        gate = IntentQualityGate().evaluate("المبيعات والمناطق", schema)
        call = resolver.resolve("المبيعات والمناطق", schema, gate)
        assert call.tool == "top_n"
        assert call.arguments["dimension"] == "region"

    def test_model_returning_unknown_is_respected(self, schema):
        resolver = LLMIntentResolver(call_fn=_json_call_fn({"tool": "unknown", "arguments": {}}))
        gate = IntentQualityGate().evaluate("قارن المبيعات", schema)
        call = resolver.resolve("قارن المبيعات", schema, gate)
        assert call.tool == "unknown"


class TestLlmIntentResolverFailSafe:
    """قسم 9: invalid LLM ToolCall / ambiguous -> unknown فوري، بلا استثناء يخرج."""

    def test_malformed_json_becomes_unknown(self, schema):
        resolver = LLMIntentResolver(call_fn=lambda prompt: "هذا مو JSON إطلاقاً {{{")
        gate = IntentQualityGate().evaluate("قارن المبيعات", schema)
        call = resolver.resolve("قارن المبيعات", schema, gate)
        assert call.tool == "unknown"
        assert call.arguments == {}

    def test_json_array_instead_of_object_becomes_unknown(self, schema):
        resolver = LLMIntentResolver(call_fn=lambda prompt: json.dumps([1, 2, 3]))
        gate = IntentQualityGate().evaluate("قارن المبيعات", schema)
        assert resolver.resolve("قارن المبيعات", schema, gate).tool == "unknown"

    def test_missing_required_keys_becomes_unknown(self, schema):
        resolver = LLMIntentResolver(call_fn=_json_call_fn({"tool": "aggregate"}))
        gate = IntentQualityGate().evaluate("قارن المبيعات", schema)
        assert resolver.resolve("قارن المبيعات", schema, gate).tool == "unknown"

    def test_network_exception_becomes_unknown_not_a_crash(self, schema):
        resolver = LLMIntentResolver(call_fn=_raising_call_fn())
        gate = IntentQualityGate().evaluate("قارن المبيعات", schema)
        call = resolver.resolve("قارن المبيعات", schema, gate)
        assert call.tool == "unknown"

    def test_no_eager_network_import(self):
        """يضمن أن استيراد google-genai كسول (داخل الدالة) لا على مستوى
        الوحدة — الوحدة يجب أن تُستورَد حتى لو google-genai غير مثبَّتة."""
        import phoenix.llm_intent_resolver as mod
        source = Path(mod.__file__).read_text(encoding="utf-8")
        top_level_imports = [ln for ln in source.splitlines()
                             if ln.startswith(("import ", "from "))]
        assert not any("google" in ln for ln in top_level_imports)


# ============================================================= AIQuestionRouter

class TestAIQuestionRouter:
    def test_valid_llm_call_passes_through_validator(self, schema):
        resolver = LLMIntentResolver(call_fn=_json_call_fn(
            {"tool": "top_n", "arguments": {"measure": "total_amount", "dimension": "region",
                                            "n": 5, "ascending": False}}))
        router = AIQuestionRouter(resolver=resolver)
        gate = IntentQualityGate().evaluate("المبيعات والمناطق", schema)
        call = router.route_escalated("المبيعات والمناطق", schema, gate)
        assert call.tool == "top_n"

    def test_llm_inventing_metric_is_rejected_by_validator(self, schema):
        """الـLLM يحاول استخدام عمود بُعد كـmeasure -> Validator يرفض -> unknown."""
        resolver = LLMIntentResolver(call_fn=_json_call_fn(
            {"tool": "aggregate", "arguments": {"measure": "region"}}))
        router = AIQuestionRouter(resolver=resolver)
        gate = IntentQualityGate().evaluate("قارن المبيعات", schema)
        call = router.route_escalated("قارن المبيعات", schema, gate)
        assert call.tool == "unknown"

    def test_llm_requesting_nonexistent_tool_is_rejected(self, schema):
        resolver = LLMIntentResolver(call_fn=_json_call_fn(
            {"tool": "delete_all_data", "arguments": {}}))
        router = AIQuestionRouter(resolver=resolver)
        gate = IntentQualityGate().evaluate("قارن المبيعات", schema)
        assert router.route_escalated("قارن المبيعات", schema, gate).tool == "unknown"

    def test_no_retry_on_rejection(self, schema):
        """رفض واحد من Validator يجب ألا يستدعي resolver مرة ثانية."""
        calls = {"n": 0}

        def counting_fn(prompt: str) -> str:
            calls["n"] += 1
            return json.dumps({"tool": "aggregate", "arguments": {"measure": "region"}})

        resolver = LLMIntentResolver(call_fn=counting_fn)
        router = AIQuestionRouter(resolver=resolver)
        gate = IntentQualityGate().evaluate("قارن المبيعات", schema)
        router.route_escalated("قارن المبيعات", schema, gate)
        assert calls["n"] == 1


# ==================================================================== GatedRouter

class TestGatedRouterFeatureFlag:
    def test_disabled_by_default_env_unset(self, schema, monkeypatch):
        monkeypatch.delenv(_ENV_FLAG, raising=False)
        router = GatedRouter(
            ai_question_router=AIQuestionRouter(resolver=LLMIntentResolver(call_fn=_never_call_fn())),
        )
        call = router.route("قارن المبيعات", schema)  # unknown من RuleRouter نفسه أصلاً
        assert call.tool == "unknown"

    def test_disabled_flag_never_calls_llm_even_on_escalate(self, schema, monkeypatch):
        """المبيعات والمناطق تُصعَّد (dropped signal) — بلا تفعيل، LLM يجب
        ألا يُستدعى إطلاقاً، والنتيجة unknown."""
        monkeypatch.delenv(_ENV_FLAG, raising=False)
        router = GatedRouter(
            enabled=False,
            ai_question_router=AIQuestionRouter(resolver=LLMIntentResolver(call_fn=_never_call_fn())),
        )
        call = router.route("المبيعات والمناطق", schema)
        assert call.tool == "unknown"

    def test_enabled_without_ai_router_still_fails_safe(self, schema):
        router = GatedRouter(enabled=True, ai_question_router=None)
        call = router.route("المبيعات والمناطق", schema)
        assert call.tool == "unknown"

    def test_env_var_enables_flag(self, schema, monkeypatch):
        monkeypatch.setenv(_ENV_FLAG, "true")
        resolver = LLMIntentResolver(call_fn=_json_call_fn(
            {"tool": "top_n", "arguments": {"measure": "total_amount", "dimension": "region",
                                            "n": 5, "ascending": False}}))
        router = GatedRouter(ai_question_router=AIQuestionRouter(resolver=resolver))
        call = router.route("المبيعات والمناطق", schema)
        assert call.tool == "top_n"

    def test_explicit_enabled_true_overrides_env(self, schema, monkeypatch):
        monkeypatch.delenv(_ENV_FLAG, raising=False)
        resolver = LLMIntentResolver(call_fn=_json_call_fn(
            {"tool": "top_n", "arguments": {"measure": "total_amount", "dimension": "region",
                                            "n": 5, "ascending": False}}))
        router = GatedRouter(enabled=True, ai_question_router=AIQuestionRouter(resolver=resolver))
        call = router.route("المبيعات والمناطق", schema)
        assert call.tool == "top_n"


class TestGatedRouterProceedPath:
    def test_proceed_never_calls_llm(self, schema, monkeypatch):
        """سؤال واضح لا يحتاج تصعيداً -> LLM يجب ألا يُستدعى إطلاقاً، توفيراً
        للتكلفة وثقة أعلى بالمسار الحتمي (قسم 4، حالة 1)."""
        monkeypatch.setenv(_ENV_FLAG, "true")
        router = GatedRouter(
            ai_question_router=AIQuestionRouter(resolver=LLMIntentResolver(call_fn=_never_call_fn())),
        )
        call = router.route("كم إجمالي المبيعات؟", schema)
        assert call.tool == "aggregate"

    def test_proceed_result_matches_rule_router_exactly(self, schema, monkeypatch):
        monkeypatch.delenv(_ENV_FLAG, raising=False)
        router = GatedRouter()
        call = router.route("أعلى المناطق مبيعات", schema)
        assert call.tool == "top_n"
        assert call.arguments["dimension"] == "region"


class TestGatedRouterWithProfile:
    def test_ignored_entity_escalates_and_llm_can_resolve_it(self, schema, monkeypatch):
        """قديش مبيعات اللاذقية؟ -> suspicion (ignored entity) -> escalate ->
        LLM (وهمي هنا) يقترح filtered_aggregate -> يمر عبر Validator بنجاح."""
        monkeypatch.setenv(_ENV_FLAG, "true")
        profile = DatasetProfile(
            row_count=100, column_count=5, duplicate_row_count=0, memory_mb=0.1,
            columns=[
                ColumnProfile(name="المنطقة", position=0, inferred_type="categorical",
                             type_confidence=1.0, total_count=100, null_count=0, null_pct=0.0,
                             unique_count=3, unique_pct=3.0,
                             top_values=[("اللاذقية", 40), ("طرطوس", 35)]),
            ],
        )
        resolver = LLMIntentResolver(call_fn=_json_call_fn(
            {"tool": "filtered_aggregate",
             "arguments": {"measure": "total_amount", "dimension": "region", "value": "اللاذقية"}}))
        router = GatedRouter(profile=profile,
                             ai_question_router=AIQuestionRouter(resolver=resolver))
        call = router.route("قديش مبيعات اللاذقية؟", schema)
        assert call.tool == "filtered_aggregate"
        assert call.arguments["value"] == "اللاذقية"
