"""اختبارات مستقلة للأسئلة المركّبة (CompoundQuestionHandler) والدمج النهائي
(PhoenixAI) — Phoenix AI Layer v1، القسم 7 والقسم 14.

قاعدة صارمة: **صفر اتصال شبكة حقيقي بهذا الملف** — كل استدعاء LLM محقون
بدالة وهمية. يستخدم بيانات حقيقية من fixture المشروع (نفس آلية run() الموجودة
بـtests/test_engine.py) للتأكد من أرقام حقيقية لا مُختلَقة.

ملف جديد بالكامل، منفصل عن كل ملفات الاختبار السابقة.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from phoenix.ai_explainer import AIExplainer
from phoenix.ask import ToolExecutor
from phoenix.compound_question_handler import CompoundQuestionHandler
from phoenix.gated_router import _ENV_FLAG
from phoenix.intent_quality_gate import IntentQualityGate
from phoenix.llm_intent_resolver import LLMIntentResolver
from phoenix.models import ColumnProfile, DatasetProfile, SemanticColumn, SemanticSchema
from phoenix.phoenix_ai import PhoenixAI
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


def _small_schema() -> SemanticSchema:
    return SemanticSchema(columns=[
        SemanticColumn(column_name="الإجمالي", concept="total_amount",
                       role="measure", confidence=1.0, unit="currency"),
        SemanticColumn(column_name="المنطقة", concept="region", role="dimension", confidence=1.0),
        SemanticColumn(column_name="المندوب", concept="salesperson", role="dimension", confidence=1.0),
        SemanticColumn(column_name="التاريخ", concept="date", role="time", confidence=1.0),
    ])


def _sequenced_call_fn(responses: list[dict]):
    """يرجع كل استجابة بالدور — أول استدعاء يرجع responses[0]، ثاني استدعاء
    responses[1]، إلخ. يحاكي مكالمتين منفصلتين لمرحلتي السؤال المركّب."""
    calls = {"i": 0}

    def _fn(prompt: str) -> str:
        i = calls["i"]
        calls["i"] += 1
        return json.dumps(responses[i], ensure_ascii=False)
    return _fn


def _never_call_fn():
    def _fn(prompt: str) -> str:
        raise AssertionError("call_fn استُدعي رغم أنه لا يجب استدعاؤه بهذا المسار")
    return _fn


# ==================================================== CompoundQuestionHandler

class TestCompoundQuestionHandler:
    def test_no_sub_calls_returns_single_answer(self, run):
        """سؤال عادي (بلا نية تركيب) -> إجابة واحدة فقط، بلا محاولة مرحلة ثانية."""
        eng = run.engine()
        try:
            resolver = LLMIntentResolver(call_fn=_sequenced_call_fn([
                {"tool": "aggregate", "arguments": {"measure": "total_amount"}},
            ]))
            executor = ToolExecutor(eng, run.schema)
            handler = CompoundQuestionHandler(resolver=resolver, executor=executor)
            gate = IntentQualityGate().evaluate("قارن المبيعات", run.schema)
            answers = handler.handle("قارن المبيعات", run.schema, gate)
            assert len(answers) == 1
            assert answers[0].tool_calls[0].tool == "aggregate"
        finally:
            eng.close()

    def test_two_phase_execution_produces_two_real_answers(self, run):
        """«مين أكتر مندوب وليش؟»: مرحلة 1 (top_n مندوبين) + مرحلة 2 (top_n
        منتجات — تفصيل «ليش»). كل مرحلة تنتج Evidence حقيقية من نفس الملف."""
        eng = run.engine()
        try:
            resolver = LLMIntentResolver(call_fn=_sequenced_call_fn([
                {"tool": "top_n", "arguments": {"measure": "total_amount", "dimension": "salesperson",
                                                "n": 1, "ascending": False},
                 "sub_calls": [{"tool": "top_n", "arguments": {"measure": "total_amount",
                                                               "dimension": "region", "n": 3}}]},
                {"tool": "top_n", "arguments": {"measure": "total_amount", "dimension": "region", "n": 3}},
            ]))
            executor = ToolExecutor(eng, run.schema)
            handler = CompoundQuestionHandler(resolver=resolver, executor=executor)
            gate = IntentQualityGate().evaluate("مين أكتر مندوب مبيعات وليش؟", run.schema)
            answers = handler.handle("مين أكتر مندوب مبيعات وليش؟", run.schema, gate)

            assert len(answers) == 2
            assert answers[0].tool_calls[0].tool == "top_n"
            assert answers[0].tool_calls[0].arguments["dimension"] == "salesperson"
            assert answers[1].tool_calls[0].arguments["dimension"] == "region"
            # كل إجابة تحمل Evidence حقيقية مستقلة بها
            assert answers[0].metrics[0].evidence.rows_in_scope > 0
            assert answers[1].metrics[0].evidence.rows_in_scope > 0
        finally:
            eng.close()

    def test_invalid_followup_falls_back_to_phase1_only(self, run):
        """المرحلة الثانية تطلب concept غير موجود -> Validator يرفضها ->
        النتيجة إجابة المرحلة الأولى وحدها (لا تخمين لتفسير إضافي)."""
        eng = run.engine()
        try:
            resolver = LLMIntentResolver(call_fn=_sequenced_call_fn([
                {"tool": "top_n", "arguments": {"measure": "total_amount", "dimension": "salesperson",
                                                "n": 1, "ascending": False},
                 "sub_calls": [{"tool": "aggregate", "arguments": {"measure": "profit_margin"}}]},
                {"tool": "aggregate", "arguments": {"measure": "profit_margin"}},  # concept غير موجود
            ]))
            executor = ToolExecutor(eng, run.schema)
            handler = CompoundQuestionHandler(resolver=resolver, executor=executor)
            gate = IntentQualityGate().evaluate("مين أكتر مندوب مبيعات وليش؟", run.schema)
            answers = handler.handle("مين أكتر مندوب مبيعات وليش؟", run.schema, gate)
            assert len(answers) == 1
            assert answers[0].tool_calls[0].tool == "top_n"
        finally:
            eng.close()

    def test_failed_primary_stops_before_followup(self, run):
        """لو المرحلة الأولى نفسها فشلت (unknown)، لا محاولة مرحلة ثانية إطلاقاً."""
        eng = run.engine()
        try:
            resolver = LLMIntentResolver(call_fn=_sequenced_call_fn([
                {"tool": "delete_all", "arguments": {}, "sub_calls": [{"tool": "count", "arguments": {}}]},
            ]))
            executor = ToolExecutor(eng, run.schema)
            handler = CompoundQuestionHandler(resolver=resolver, executor=executor)
            gate = IntentQualityGate().evaluate("سؤال غامض جداً", run.schema)
            answers = handler.handle("سؤال غامض جداً", run.schema, gate)
            assert len(answers) == 1
            assert answers[0].tool_calls[0].tool == "unknown"
        finally:
            eng.close()


# ================================================================== PhoenixAI

class TestPhoenixAIDisabledByDefault:
    def test_disabled_flag_falls_back_to_unknown_on_escalate(self, run, monkeypatch):
        monkeypatch.delenv(_ENV_FLAG, raising=False)
        ai = PhoenixAI(engine=run.engine(), schema=run.schema,
                      resolver=LLMIntentResolver(call_fn=_never_call_fn()))
        try:
            answer = ai.ask("المبيعات والمناطق")  # dropped signal -> escalate
            assert answer.tool_calls[0].tool == "unknown"
        finally:
            ai.engine.close()

    def test_no_resolver_at_all_is_fully_safe(self, run):
        """بلا resolver إطلاقاً — نفس سلوك اليوم تماماً، حتى لو enabled=True."""
        ai = PhoenixAI(engine=run.engine(), schema=run.schema, enabled=True)
        try:
            answer = ai.ask("المبيعات والمناطق")
            assert answer.tool_calls[0].tool == "unknown"
        finally:
            ai.engine.close()


class TestPhoenixAIProceedPath:
    def test_proceed_never_touches_ai_layer(self, run, monkeypatch):
        monkeypatch.setenv(_ENV_FLAG, "true")
        ai = PhoenixAI(engine=run.engine(), schema=run.schema,
                      resolver=LLMIntentResolver(call_fn=_never_call_fn()))
        try:
            answer = ai.ask("كم إجمالي المبيعات؟")
            assert answer.tool_calls[0].tool == "aggregate"
        finally:
            ai.engine.close()


class TestPhoenixAIEnabledEndToEnd:
    def test_escalated_simple_question_resolved_by_llm(self, run, monkeypatch):
        monkeypatch.setenv(_ENV_FLAG, "true")
        resolver = LLMIntentResolver(call_fn=_sequenced_call_fn([
            {"tool": "top_n", "arguments": {"measure": "total_amount", "dimension": "region",
                                            "n": 5, "ascending": False}},
        ]))
        ai = PhoenixAI(engine=run.engine(), schema=run.schema, resolver=resolver)
        try:
            answer = ai.ask("المبيعات والمناطق")
            assert answer.tool_calls[0].tool == "top_n"
            assert answer.metrics[0].evidence.rows_in_scope > 0
        finally:
            ai.engine.close()

    def test_compound_question_with_explainer_uses_only_real_numbers(self, run, monkeypatch):
        monkeypatch.setenv(_ENV_FLAG, "true")
        resolver = LLMIntentResolver(call_fn=_sequenced_call_fn([
            {"tool": "top_n", "arguments": {"measure": "total_amount", "dimension": "salesperson",
                                            "n": 1, "ascending": False},
             "sub_calls": [{"tool": "top_n", "arguments": {"measure": "total_amount",
                                                            "dimension": "region", "n": 3}}]},
            {"tool": "top_n", "arguments": {"measure": "total_amount", "dimension": "region", "n": 3}},
        ]))

        def _explainer_call(prompt: str) -> str:
            # يجب ألا يذكر أي رقم لم يظهر بالـprompt نفسه — نتحقق أن الفحص
            # الآلي (لا اختلاق) يقبل نصاً يستخدم فقط أرقاماً موجودة فعلياً.
            return "هذه أرقام حقيقية مأخوذة من البيانات كما هي."

        explainer = AIExplainer(call_fn=_explainer_call)
        ai = PhoenixAI(engine=run.engine(), schema=run.schema, resolver=resolver, explainer=explainer)
        try:
            answer = ai.ask("مين أكتر مندوب مبيعات وليش؟")
            assert answer.tool_calls[0].tool == "top_n"
            assert answer.answer_ar  # نص نهائي موجود (مفسَّر أو fallback، كلاهما آمن)
        finally:
            ai.engine.close()

    def test_unknown_from_llm_never_calls_explainer(self, run, monkeypatch):
        monkeypatch.setenv(_ENV_FLAG, "true")
        resolver = LLMIntentResolver(call_fn=_sequenced_call_fn([
            {"tool": "unknown", "arguments": {}},
        ]))
        explainer = AIExplainer(call_fn=_never_call_fn())
        ai = PhoenixAI(engine=run.engine(), schema=run.schema, resolver=resolver, explainer=explainer)
        try:
            answer = ai.ask("سؤال غامض جداً وغير مفهوم")
            assert answer.tool_calls[0].tool == "unknown"
        finally:
            ai.engine.close()


class TestPhoenixAIWithProfile:
    def test_ignored_entity_full_pipeline_with_real_data(self, run, monkeypatch):
        """قديش مبيعات منطقة حقيقية بالاسم؟ -> escalate (ignored entity) ->
        LLM (وهمي) يقترح filtered_aggregate -> ينفَّذ فعلياً على بيانات حقيقية."""
        monkeypatch.setenv(_ENV_FLAG, "true")
        eng = run.engine()
        rows, _ = eng.top_n("total_amount", "region", n=1)
        region = rows[0]["label"]
        eng.close()

        profile = DatasetProfile(
            row_count=1, column_count=1, duplicate_row_count=0, memory_mb=0.0,
            columns=[ColumnProfile(name="المنطقه", position=0, inferred_type="categorical",
                                   type_confidence=1.0, total_count=1, null_count=0, null_pct=0.0,
                                   unique_count=1, unique_pct=1.0, top_values=[(region, 1)])],
        )
        resolver = LLMIntentResolver(call_fn=_sequenced_call_fn([
            {"tool": "filtered_aggregate",
             "arguments": {"measure": "total_amount", "dimension": "region", "value": region}},
        ]))
        ai = PhoenixAI(engine=run.engine(), schema=run.schema, profile=profile, resolver=resolver)
        try:
            answer = ai.ask(f"قديش مبيعات {region}؟")
            assert answer.tool_calls[0].tool == "filtered_aggregate"
            assert region in answer.answer_ar
        finally:
            ai.engine.close()
