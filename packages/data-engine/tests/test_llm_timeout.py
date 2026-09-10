"""مهلة نداء النموذج — نداء عالق يجب ألا يعلّق الطلب.

بلا مهلة: مزوّد بطيء أو شبكة متعثّرة يُبقي طلب المستخدم معلّقاً إلى الأبد،
وهو أسوأ من إجابة «لم أفهم» بكثير.
"""
from __future__ import annotations

import time

import pytest

from phoenix.ai_explainer import AIExplainer
from phoenix.intent_quality_gate import GateResult, IntentQualityGate
from phoenix.llm_intent_resolver import (LLMTimeout, LLMIntentResolver,
                                         call_with_timeout)
from phoenix.models import Answer, SemanticColumn, SemanticSchema, ToolCall


def _gate(question: str) -> GateResult:
    """قرار Gate حقيقي — أصدق من بناء كائن يدوي قد ينحرف عن العقد."""
    return IntentQualityGate().evaluate(question, _schema())


def _schema() -> SemanticSchema:
    return SemanticSchema(columns=[
        SemanticColumn(column_name="الإجمالي", concept="total_amount",
                       role="measure", confidence=1.0),
    ])


class TestCallWithTimeout:
    def test_a_fast_call_returns_its_value(self):
        assert call_with_timeout(lambda p: f"ok:{p}", "x", 5) == "ok:x"

    def test_a_slow_call_raises_instead_of_hanging(self):
        started = time.perf_counter()
        with pytest.raises(LLMTimeout):
            call_with_timeout(lambda p: time.sleep(10), "x", 0.2)
        # الأهم ليس نوع الاستثناء بل أننا عدنا سريعاً
        assert time.perf_counter() - started < 2.0

    def test_an_error_inside_the_call_is_not_swallowed_as_a_timeout(self):
        def boom(_p):
            raise ValueError("انفجار")
        with pytest.raises(ValueError):
            call_with_timeout(boom, "x", 5)


class TestResolverUnderTimeout:
    def test_a_hanging_model_becomes_unknown_not_a_hang(self):
        """سلوك الرفض بدل التخمين ينطبق على المهلة أيضاً."""
        resolver = LLMIntentResolver(call_fn=lambda p: time.sleep(10),
                                     timeout_seconds=0.2)
        started = time.perf_counter()
        call = resolver.resolve("سؤال غامض جداً", _schema(), _gate("سؤال غامض جداً"))
        assert call.tool == "unknown"
        assert time.perf_counter() - started < 3.0

    def test_the_call_is_still_counted_for_cost(self):
        """نداء انتهت مهلته كلّف مالاً — يجب أن يُحسب."""
        resolver = LLMIntentResolver(call_fn=lambda p: time.sleep(10),
                                     timeout_seconds=0.2)
        resolver.resolve("سؤال", _schema(), _gate("سؤال"))
        assert resolver.llm_calls == 1


class TestExplainerUnderTimeout:
    def test_a_hanging_explainer_falls_back_to_the_deterministic_text(self):
        answer = Answer(question="س", understood_as="ف",
                        tool_calls=[ToolCall(tool="aggregate", arguments={})],
                        answer_ar="النص الحتمي الأصلي")
        explainer = AIExplainer(call_fn=lambda p: time.sleep(10), timeout_seconds=0.2)
        started = time.perf_counter()
        out = explainer.explain("س", answer)
        assert out == "النص الحتمي الأصلي"
        assert time.perf_counter() - started < 3.0
