"""فحص يدوي حي لمفتاح Gemini الحقيقي — لا يشغَّل ضمن pytest أبداً.

هذا الملف مقصود أن يُشغَّل يدوياً على جهازك (أو أي بيئة عندها اتصال إنترنت
فعلي بـ generativelanguage.googleapis.com)، وليس ضمن حزمة الاختبار الآلية —
حزمة phoenix تعمل عمداً بلا أي اتصال شبكة حقيقي (انظر رأس tests/test_engine.py
وtests/test_ai_layer_escalation.py). هذا السكربت هو الاستثناء الوحيد المتعمَّد.

الاستخدام:
    export GEMINI_API_KEY="مفتاحك هنا"
    python3 tools/try_gemini_live.py

لا يكتب المفتاح لأي ملف، ولا يُطبع بالمخرَجات — يُقرأ فقط من متغيّر البيئة.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from phoenix.intent_quality_gate import IntentQualityGate
from phoenix.llm_intent_resolver import LLMIntentResolver
from phoenix.models import SemanticColumn, SemanticSchema
from phoenix.tool_call_validator import ToolCallValidator


def _sample_schema() -> SemanticSchema:
    return SemanticSchema(columns=[
        SemanticColumn(column_name="الإجمالي", concept="total_amount",
                       role="measure", confidence=1.0, unit="currency"),
        SemanticColumn(column_name="الكمية", concept="quantity", role="measure", confidence=1.0),
        SemanticColumn(column_name="المنطقة", concept="region", role="dimension", confidence=1.0),
        SemanticColumn(column_name="المندوب", concept="salesperson", role="dimension", confidence=1.0),
        SemanticColumn(column_name="التاريخ", concept="date", role="time", confidence=1.0),
    ])


def main() -> int:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("خطأ: متغيّر البيئة GEMINI_API_KEY غير موجود.")
        print('نفّذ: export GEMINI_API_KEY="مفتاحك"')
        return 1

    try:
        import google.genai  # noqa: F401
    except ImportError:
        print("خطأ: حزمة google-genai غير مثبَّتة. نفّذ: pip install google-genai")
        return 1

    schema = _sample_schema()
    questions = [
        "قارن المبيعات",          # سؤال غامض متعمَّد -> يجب أن يرجع unknown
        "قديش مبيعات اللاذقية؟",  # سؤال فلترة -> يُصعَّد -> يفترض ينتج filtered_aggregate
    ]

    resolver = LLMIntentResolver(api_key=api_key)  # بلا call_fn -> يتصل فعلياً بـGemini
    validator = ToolCallValidator()
    gate = IntentQualityGate()

    print(f"النموذج المستخدَم: {resolver.model}\n")
    ok = True
    for q in questions:
        print(f"── السؤال: {q}")
        gate_result = gate.evaluate(q, schema)
        print(f"   قرار الـGate: {'proceed' if gate_result.proceed else 'escalate'} ({gate_result.reason})")
        if gate_result.proceed:
            print(f"   -> لن يُستدعى LLM (المسار الحتمي كافٍ): {gate_result.tool_call}")
            continue
        try:
            raw_call = resolver.resolve(q, schema, gate_result)
        except Exception as e:  # noqa: BLE001 — فحص يدوي، نريد رؤية أي خطأ فعلي
            print(f"   !! فشل الاتصال الفعلي بـGemini: {type(e).__name__}: {e}")
            ok = False
            continue
        result = validator.validate(raw_call, schema)
        print(f"   رد LLM الخام: {raw_call}")
        print(f"   بعد Validator: valid={result.valid} tool_call={result.tool_call}"
              + (f" | سبب الرفض: {result.reason}" if not result.valid else ""))
    print()
    print("انتهى الفحص." if ok else "انتهى الفحص — مع فشل اتصال واحد على الأقل، راجع الرسائل أعلاه.")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
