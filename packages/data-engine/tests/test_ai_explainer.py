"""اختبارات مستقلة لـ AIExplainer — Phoenix AI Layer v1، القسم 8.

قاعدة صارمة: **صفر اتصال شبكة حقيقي بهذا الملف** — كل اختبار يحقن `call_fn`
وهمياً. ملف جديد بالكامل، منفصل عن كل ملفات الاختبار السابقة.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from phoenix.ai_explainer import AIExplainer, _allowed_numbers, _numbers_in_text
from phoenix.models import Answer, Evidence, MetricResult, ToolCall


def _answer(value=1234.5, rows_in_scope=50, rows_total=100, answer_ar=None,
           understood_as="إجمالي المبيعات", filters_applied=None) -> Answer:
    ev = Evidence(
        metric_name="sum(total_amount)", sql="SELECT sum(...)", source_columns=["الإجمالي"],
        rows_in_scope=rows_in_scope, rows_total=rows_total,
        filters_applied=filters_applied or [],
    )
    metric = MetricResult(value=value, formatted_ar=f"{value:,.1f} ر.س", unit="currency", evidence=ev)
    return Answer(
        question="كم إجمالي المبيعات؟", understood_as=understood_as,
        tool_calls=[ToolCall(tool="aggregate", arguments={"measure": "total_amount"})],
        answer_ar=answer_ar or f"إجمالي المبيعات يساوي **{value:,.1f} ر.س** محسوباً من {rows_in_scope} صف.",
        metrics=[metric],
    )


def _never_call_fn():
    def _fn(prompt: str) -> str:
        raise AssertionError("call_fn استُدعي رغم أنه لا يجب استدعاء LLM بهذا المسار")
    return _fn


# ---------------------------------------------------------------- دوال مساعدة

class TestNumberHelpers:
    def test_extracts_numbers_with_thousands_separator(self):
        assert 1234.5 in _numbers_in_text("القيمة 1,234.5 ر.س")

    def test_extracts_plain_integer(self):
        assert 50.0 in _numbers_in_text("من 50 صف")

    def test_allowed_numbers_includes_evidence_and_answer_ar(self):
        a = _answer(value=999.0, rows_in_scope=10, rows_total=20)
        allowed = _allowed_numbers([a])
        assert 999.0 in allowed
        assert 10.0 in allowed
        assert 20.0 in allowed

    def test_allowed_numbers_includes_nested_list_values(self):
        ev = Evidence(metric_name="top_n", sql="...", source_columns=[],
                      rows_in_scope=5, rows_total=5)
        metric = MetricResult(value=[{"label": "أ", "value": 42.0}, {"label": "ب", "value": 17.0}],
                              formatted_ar="نتيجتان", evidence=ev)
        a = Answer(question="q", understood_as="u", tool_calls=[], answer_ar="نص", metrics=[metric])
        allowed = _allowed_numbers([a])
        assert 42.0 in allowed and 17.0 in allowed


# --------------------------------------------------------------------- الحالة الآمنة (صفر صفوف)

class TestZeroRowsShortcut:
    def test_zero_rows_never_calls_llm_and_returns_deterministic_text(self):
        a = _answer(value=0.0, rows_in_scope=0, rows_total=100,
                    answer_ar="لا يوجد أي صف مطابق لـ «س» — الإجمالي هو 0.")
        explainer = AIExplainer(call_fn=_never_call_fn())
        result = explainer.explain("كم مبيعات س؟", a)
        assert result == a.answer_ar


class TestNoMetricsShortcut:
    def test_unknown_answer_never_calls_llm(self):
        a = Answer(question="ما هي عاصمة اليابان؟", understood_as="لم يُفهم السؤال",
                  tool_calls=[ToolCall(tool="unknown", arguments={})], confidence=0.0,
                  answer_ar="لم أفهم السؤال بدقة.")
        explainer = AIExplainer(call_fn=_never_call_fn())
        assert explainer.explain("ما هي عاصمة اليابان؟", a) == a.answer_ar


# --------------------------------------------------------------------- المسار الطبيعي

class TestHappyPath:
    def test_valid_explanation_using_only_allowed_numbers_is_returned(self):
        a = _answer(value=1234.5, rows_in_scope=50, rows_total=100)
        explainer = AIExplainer(call_fn=lambda p: "المبيعات بلغت 1,234.5 ر.س من أصل 100 صف، شملت 50 منها.")
        result = explainer.explain("كم إجمالي المبيعات؟", a)
        assert "1,234.5" in result or "1234.5" in result


# --------------------------------------------------------------------- منع الأرقام المختلَقة

class TestHallucinationPrevention:
    def test_invented_number_rejects_and_falls_back(self):
        a = _answer(value=1234.5, rows_in_scope=50, rows_total=100)
        explainer = AIExplainer(call_fn=lambda p: "المبيعات ارتفعت بنسبة 250% مقارنة بالشهر الماضي.")
        result = explainer.explain("كم إجمالي المبيعات؟", a)
        assert result == a.answer_ar  # fallback حرفي، لا الرقم المختلَق 250

    def test_number_present_in_evidence_is_accepted(self):
        a = _answer(value=1234.5, rows_in_scope=50, rows_total=100)
        explainer = AIExplainer(call_fn=lambda p: "من أصل 100 صف بالبيانات، شملت الحسابات 50 صفاً فقط.")
        result = explainer.explain("كم إجمالي المبيعات؟", a)
        assert result != a.answer_ar  # قُبل النص لأن كل أرقامه موجودة فعلياً بالمُدخل


# ----------------------------------------------------------- منع الاستنتاج السببي غير المبرَّر

class TestCausalClaimPrevention:
    def test_causal_claim_on_single_answer_is_rejected(self):
        a = _answer(value=1234.5, rows_in_scope=50, rows_total=100)
        explainer = AIExplainer(
            call_fn=lambda p: "المبيعات 1,234.5 ر.س من 50 صف لأن الطلب زاد."
        )
        result = explainer.explain("كم إجمالي المبيعات؟", a)
        assert result == a.answer_ar

    def test_causal_claim_allowed_with_second_real_answer(self):
        """قسم 8: الاستنتاج السببي مسموح فقط لو مبني على Evidence إضافية فعلية
        من ToolCall ثانٍ منفَّذ فعلياً — أي أكثر من Answer واحد."""
        a1 = _answer(value=1234.5, rows_in_scope=50, rows_total=100)
        a2 = _answer(value=900.0, rows_in_scope=40, rows_total=100,
                    understood_as="إجمالي الشهر الماضي",
                    answer_ar="إجمالي الشهر الماضي يساوي 900.0 ر.س من 40 صف.")
        explainer = AIExplainer(
            call_fn=lambda p: "ارتفعت المبيعات من 900.0 إلى 1,234.5 لأن عدد الصفوف زاد من 40 إلى 50."
        )
        result = explainer.explain("قارن الشهرين", [a1, a2])
        assert "1,234.5" in result or "1234.5" in result


# ----------------------------------------------------------------------- شبكة/أعطال

class TestNetworkFailSafe:
    def test_call_fn_exception_falls_back_safely(self):
        def _raise(prompt: str) -> str:
            raise RuntimeError("network down")
        a = _answer()
        explainer = AIExplainer(call_fn=_raise)
        assert explainer.explain("كم إجمالي المبيعات؟", a) == a.answer_ar

    def test_empty_response_falls_back(self):
        a = _answer()
        explainer = AIExplainer(call_fn=lambda p: "   ")
        assert explainer.explain("كم إجمالي المبيعات؟", a) == a.answer_ar

    def test_no_eager_network_import(self):
        import phoenix.ai_explainer as mod
        source = Path(mod.__file__).read_text(encoding="utf-8")
        top_level_imports = [ln for ln in source.splitlines()
                             if ln.startswith(("import ", "from "))]
        assert not any("google" in ln for ln in top_level_imports)
