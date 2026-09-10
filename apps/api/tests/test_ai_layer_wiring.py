"""توصيل طبقة الذكاء بالـAPI — بلا أي نداء شبكة حقيقي.

الأهم هنا: **إثبات أن الافتراضي آمن**. الطبقة مغلقة ما لم يوجد مفتاح فعلي،
والمسار المفهوم (الحتمي) لا يمر على الـLLM إطلاقاً حتى وهي مفعّلة.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "packages" / "data-engine"))

from apps.api.src.config import Settings  # noqa: E402
from phoenix.pipeline import process  # noqa: E402
from phoenix.phoenix_ai import PhoenixAI  # noqa: E402
from phoenix.llm_intent_resolver import LLMIntentResolver  # noqa: E402

FIXTURE = ROOT / "packages" / "data-engine" / "fixtures" / "sales_ar_messy.xlsx"


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    return process(FIXTURE, run_dir=tmp_path_factory.mktemp("ai_wiring"))


class TestSafeDefaults:
    def test_ai_disabled_by_default(self):
        assert Settings(_env_file=None).ai_layer_enabled is False

    def test_enabling_without_key_stays_off(self):
        """تفعيل بلا مفتاح لا يعني نصف تشغيل — يبقى مغلقاً تماماً."""
        s = Settings(_env_file=None, ai_layer_enabled=True, gemini_api_key="")
        assert s.ai_ready is False

    def test_key_without_flag_stays_off(self):
        s = Settings(_env_file=None, ai_layer_enabled=False, gemini_api_key="k")
        assert s.ai_ready is False

    def test_both_required(self):
        s = Settings(_env_file=None, ai_layer_enabled=True, gemini_api_key="k")
        assert s.ai_ready is True


class TestNoLLMOnUnderstoodQuestions:
    def test_clear_question_never_calls_llm(self, run):
        """سؤال مفهوم يُنفَّذ حتمياً — أي نداء LLM هنا تكلفة بلا فائدة."""
        called: list[str] = []

        def spy(prompt: str) -> str:
            called.append(prompt)
            return '{"tool": "unknown", "arguments": {}}'

        eng = run.engine()
        try:
            phoenix = PhoenixAI(
                engine=eng, schema=run.schema,
                resolver=LLMIntentResolver(call_fn=spy), enabled=True,
            )
            answer = phoenix.ask("كم إجمالي المبيعات؟")
        finally:
            eng.close()

        assert called == [], "استُدعي الـLLM لسؤال مفهوم أصلاً"
        assert any(ch.isdigit() for ch in answer.answer_ar)

    def test_unclear_question_escalates_to_llm(self, run):
        """السؤال الغامض هو الحالة الوحيدة التي تستحق التصعيد."""
        called: list[str] = []

        def spy(prompt: str) -> str:
            called.append(prompt)
            return '{"tool": "unknown", "arguments": {}}'

        eng = run.engine()
        try:
            phoenix = PhoenixAI(
                engine=eng, schema=run.schema,
                resolver=LLMIntentResolver(call_fn=spy), enabled=True,
            )
            phoenix.ask("شو رأيك بالوضع بشكل عام يا فينيق")
        finally:
            eng.close()

        assert called, "سؤال غامض لم يُصعَّد إطلاقاً"

    def test_disabled_layer_makes_no_call_even_when_unclear(self, run):
        called: list[str] = []

        def spy(prompt: str) -> str:
            called.append(prompt)
            return '{"tool": "unknown", "arguments": {}}'

        eng = run.engine()
        try:
            phoenix = PhoenixAI(
                engine=eng, schema=run.schema,
                resolver=LLMIntentResolver(call_fn=spy), enabled=False,
            )
            answer = phoenix.ask("شو رأيك بالوضع بشكل عام يا فينيق")
        finally:
            eng.close()

        assert called == []
        assert "لم أفهم" in answer.answer_ar


class TestFailSafe:
    def test_llm_failure_does_not_crash_the_request(self, run):
        """انقطاع المزوّد لا يُسقط الطلب — يرجع «لم أفهم» بدل خطأ 500."""
        def broken(prompt: str) -> str:
            raise RuntimeError("المزوّد غير متاح")

        eng = run.engine()
        try:
            phoenix = PhoenixAI(
                engine=eng, schema=run.schema,
                resolver=LLMIntentResolver(call_fn=broken), enabled=True,
            )
            answer = phoenix.ask("شو رأيك بالوضع بشكل عام يا فينيق")
        finally:
            eng.close()

        assert answer.answer_ar

    def test_llm_inventing_a_tool_is_rejected(self, run):
        """حارس الأدوات: اسم أداة مخترَع لا يُنفَّذ أبداً."""
        eng = run.engine()
        try:
            phoenix = PhoenixAI(
                engine=eng, schema=run.schema,
                resolver=LLMIntentResolver(
                    call_fn=lambda p: '{"tool": "drop_everything", "arguments": {}}'),
                enabled=True,
            )
            answer = phoenix.ask("شو رأيك بالوضع بشكل عام يا فينيق")
        finally:
            eng.close()

        assert answer.tool_calls[0].tool != "drop_everything"
