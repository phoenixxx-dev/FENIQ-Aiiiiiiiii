#!/usr/bin/env python3
"""مشغّل حزمة التقييم.

الاستخدام:
    python3 packages/data-engine/evals/run_eval.py            # المسار الحتمي
    PHOENIX_AI_LAYER_ENABLED=1 GEMINI_API_KEY=... \
        python3 packages/data-engine/evals/run_eval.py --ai   # مع طبقة الذكاء

يطبع جدول نتائج ونسبة النجاح، ويُرجع 1 لو نزلت تحت العتبة — صالح للـCI.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from phoenix.ask import RuleRouter, ToolExecutor  # noqa: E402
from phoenix.pipeline import process  # noqa: E402

QUESTIONS = Path(__file__).parent / "questions.yaml"
FIXTURE = ROOT / "fixtures" / "sales_ar_messy.xlsx"
PASS_THRESHOLD = 0.90        # خطة المشروع §7.5


def evaluate(use_ai: bool = False) -> tuple[int, int, list[str]]:
    cases = yaml.safe_load(QUESTIONS.read_text(encoding="utf-8"))["questions"]
    run = process(FIXTURE, run_dir=Path("/tmp/phoenix_eval_run"))
    engine = run.engine()

    asker = None
    if use_ai:
        from phoenix.llm_intent_resolver import LLMIntentResolver
        from phoenix.phoenix_ai import PhoenixAI
        asker = PhoenixAI(engine=engine, schema=run.schema,
                          resolver=LLMIntentResolver(), enabled=True)

    executor = ToolExecutor(engine, run.schema)
    router = RuleRouter()

    passed, failures = 0, []
    try:
        for case in cases:
            q = case["q"]
            if asker is not None:
                answer = asker.ask(q)
            else:
                answer = executor.execute(router.route(q, run.schema), q)

            tool = answer.tool_calls[0].tool if answer.tool_calls else "unknown"
            ok, why = _check(case, tool, answer)
            if ok:
                passed += 1
            else:
                failures.append(f"«{q}» → {tool}: {why}")
    finally:
        engine.close()

    return passed, len(cases), failures


def _check(case: dict, tool: str, answer) -> tuple[bool, str]:
    if case.get("expect_unknown"):
        return (tool == "unknown", "كان يجب رفضه صراحةً لا الإجابة عنه")

    expected = case.get("expected_tool")
    if expected and tool != expected:
        return False, f"توقّعنا {expected}"

    if case.get("expect_number"):
        if not any(ch.isdigit() for ch in answer.answer_ar):
            return False, "إجابة بلا رقم"
        # قاعدة ذهبية #1: كل رقم معروض له دليل محسوب
        if not answer.metrics or answer.metrics[0].evidence is None:
            return False, "رقم بلا دليل"
    return True, ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ai", action="store_true", help="فعّل طبقة الذكاء")
    args = parser.parse_args()

    passed, total, failures = evaluate(use_ai=args.ai)
    rate = passed / total if total else 0.0

    print(f"\nالنتيجة: {passed}/{total} = {rate:.0%}"
          f"  (العتبة {PASS_THRESHOLD:.0%})")
    if failures:
        print("\nالإخفاقات:")
        for f in failures:
            print("  ✗", f)

    if rate < PASS_THRESHOLD:
        print("\n✗ تحت العتبة — لا تعتمد هذا التغيير.")
        return 1
    print("\n✓ فوق العتبة.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
