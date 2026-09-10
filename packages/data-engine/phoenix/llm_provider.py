"""حدّ الشبكة الوحيد داخل المحرك.

القاعدة الذهبية #4 تمنع أي نداء HTTP داخل `data-engine`. الاستثناء الوحيد
المُعلَن هو نداء نموذج اللغة، وقد جُمِع كلّه هنا — في ملف واحد — لسببين:

1. **تبديل المزوّد = تعديل ملف واحد** (متطلّب الخطة §1). قبل هذا الملف كان
   عميل Gemini يُبنى في موضعين منفصلين (`llm_intent_resolver` و`ai_explainer`)،
   أي أنّ تبديل المزوّد كان يتطلّب تعديل ملفّين — والوعد المكتوب في الخطة
   كان غير صحيح عملياً.
2. **الحارس يصير قابلاً للفرض.** ما دامت الشبكة في ملف مُسمّى، يستطيع
   `test_golden_rules.py` أن يرفض استيراد أي عميل شبكة في أي وحدة أخرى.
   قبل ذلك كانت قائمة المنع تذكر `httpx`/`requests` فقط، فمرّ `google.genai`
   بلا اعتراض: الحارس كان يحرس الأسماء لا الحدّ.

لا شيء هنا يُنفَّذ في الاختبارات: كل مستهلك يحقن `call_fn` وهمياً. الاستيراد
كسول عمداً كي لا يفشل تحميل المحرك على بيئة بلا حزمة `google-genai`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class LLMProvider(Protocol):
    """العقد الذي يراه المحرك. أي مزوّد جديد ينفّذ هذه الدالة وحدها."""

    def complete(self, prompt: str, *, model: str,
                 json_schema: dict | None = None) -> str: ...


@dataclass
class GeminiProvider:
    """المزوّد الافتراضي.

    `json_schema` يفصل الاستعمالين: تحليل النية يطلب JSON مُقيَّداً بمخطّط،
    والشرح اللغوي يطلب نصاً حرّاً. الفرق بينهما إعداد واحد لا مسار مستقل.
    """

    api_key: str | None = None

    def complete(self, prompt: str, *, model: str,
                 json_schema: dict | None = None) -> str:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=self.api_key) if self.api_key else genai.Client()
        config = None
        if json_schema is not None:
            config = types.GenerateContentConfig(
                response_mime_type="application/json",
                response_json_schema=json_schema,
            )
        response = client.models.generate_content(
            model=model, contents=prompt, config=config)
        return response.text


def default_provider(api_key: str | None = None) -> LLMProvider:
    """نقطة التبديل. استبدال Gemini بمزوّد آخر = تغيير هذا السطر وحده."""
    return GeminiProvider(api_key=api_key)
