"""الأسئلة المقترحة — يجب أن تكون قابلة للإجابة فعلاً.

الخطر الحقيقي: اقتراح سؤال لا يفهمه المحرك. المستخدم يضغطه أولَ ما يفتح
الشاشة، فيأتيه «لم أفهم» — أسوأ انطباع أول ممكن.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from phoenix import pipeline
from phoenix.analytics import AnalyticsEngine
from phoenix.ask import RuleRouter, ToolExecutor, suggest_questions

FIX = Path(__file__).resolve().parents[1] / "fixtures"
FILES = ["sales_ar_messy.xlsx",
         "adversarial/01_messy_sales.xlsx",
         "adversarial/02_inventory_paradox.csv",
         "adversarial/06_returns.xlsx",
         "adversarial/07_rep_performance.csv",
         "adversarial/10_BOSS_sales.xlsx"]


@pytest.mark.parametrize("name", FILES)
def test_every_suggestion_is_actually_answerable(name, tmp_path):
    run = pipeline.process(FIX / name, run_dir=tmp_path / "run")
    questions = suggest_questions(run.schema)
    assert questions, f"{name}: بلا اقتراحات إطلاقاً"

    engine = AnalyticsEngine(run.processed_path, run.schema)
    try:
        for q in questions:
            call = RuleRouter().route(q, run.schema)
            answer = ToolExecutor(engine, run.schema).execute(call, q)
            assert answer.metrics, f"{name}: «{q}» لم يُنتج أي مقياس"
            assert answer.metrics[0].evidence is not None, (
                f"{name}: «{q}» أنتج رقماً بلا دليل (قاعدة ذهبية #1)")
            assert "لم أفهم" not in answer.answer_ar, (
                f"{name}: الاقتراح «{q}» لا يفهمه المحرك نفسه")
    finally:
        engine.close()


def test_suggestions_adapt_to_what_the_file_contains(tmp_path):
    """ملف بلا تاريخ لا يُقترح عليه سؤال زمني."""
    run = pipeline.process(FIX / "adversarial/07_rep_performance.csv",
                           run_dir=tmp_path / "run")
    questions = suggest_questions(run.schema)
    assert not any("الشهر" in q for q in questions), (
        "اقتُرح سؤال زمني على ملف بلا عمود تاريخ")
    assert any("مندوب" in q for q in questions), "أُهمل البُعد المتاح فعلاً"


def test_a_file_with_no_measures_still_gets_something_answerable(tmp_path):
    run = pipeline.process(FIX / "adversarial/05_product_identity.json",
                           run_dir=tmp_path / "run")
    questions = suggest_questions(run.schema)
    assert questions == ["كم عدد الصفوف؟"]
