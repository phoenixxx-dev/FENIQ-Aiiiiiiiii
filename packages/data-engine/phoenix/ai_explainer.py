"""AIExplainer — Phoenix AI Layer v1، القسم 8 من المواصفة المعمارية.

يُستدعى فقط بعد تنفيذ كامل عبر ToolExecutor. يستقبل حصراً: السؤال الأصلي +
Answer (أو قائمة Answer) + Evidence الكاملة المرفقة بها — ولا شيء غير هذا.
لا وصول لـSchema، لا AnalyticsEngine، لا بيانات خام (القيد مضمون بنيوياً: هذا
الصنف لا يأخذ schema ولا engine كمعاملات إطلاقاً).

منع الأرقام المُختلَقة (hallucination) بطبقتين، تماماً كما ينص القسم 8:
1. تقييد الـprompt نفسه: لا تذكر رقماً لم يظهر بالمُدخل.
2. فحص آلي بعد الاستجابة: كل رقم بالنص الناتج يُقارَن مقابل كل رقم موجود فعلياً
   بالمُدخل (Evidence + MetricResult + النص الحتمي answer_ar نفسه). أي رقم غير
   مطابق = رفض الصياغة بالكامل والرجوع لـanswer_ar الحتمي كـfallback آمن.

نفس فلسفة LLMIntentResolver: استدعاء النموذج معزول خلف `call_fn` قابل للحقن —
الاختبارات لا تتصل بأي شبكة حقيقية إطلاقاً.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .arabic_text import to_western_digits
from .llm_intent_resolver import (DEFAULT_MODEL, LLM_TIMEOUT_SECONDS,
                                   call_with_timeout)
from . import llm_provider
from .models import Answer

# ---------------------------------------------------------------------------
# جمع الأرقام "المسموحة" — كل رقم ظهر فعلياً بالمُدخل (Evidence/MetricResult/
# النص الحتمي answer_ar) يُعتبر مصدراً شرعياً يمكن لصياغة AIExplainer تكراره.

_NUM_RE = re.compile(r"\d[\d,\.]*")


def _parse_number_token(tok: str) -> float | None:
    cleaned = tok.strip(".,").replace(",", "")
    if not cleaned:
        return None
    try:
        return round(float(cleaned), 2)
    except ValueError:
        return None


def _numbers_in_text(text: str) -> set[float]:
    out: set[float] = set()
    for tok in _NUM_RE.findall(to_western_digits(text)):
        n = _parse_number_token(tok)
        if n is not None:
            out.add(n)
    return out


def _collect_numeric_leaves(obj: Any, out: set[float]) -> None:
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        out.add(round(float(obj), 2))
    elif isinstance(obj, dict):
        for v in obj.values():
            _collect_numeric_leaves(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _collect_numeric_leaves(v, out)


def _allowed_numbers(answers: list[Answer]) -> set[float]:
    allowed: set[float] = set()
    for a in answers:
        allowed |= _numbers_in_text(a.answer_ar)
        allowed |= _numbers_in_text(a.understood_as)
        for m in a.metrics:
            _collect_numeric_leaves(m.value, allowed)
            ev = m.evidence
            allowed.add(round(float(ev.rows_in_scope), 2))
            allowed.add(round(float(ev.rows_total), 2))
            for f in ev.filters_applied:
                v = f.get("value")
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    allowed.add(round(float(v), 2))
    return allowed


# ---------------------------------------------------------------------------
# منع الاستنتاج السببي غير المسموح (قسم 8، آخر فقرة): "أي استنتاج سببي حقيقي
# ('لأن...' 'بسبب...') ممنوع كلياً ما لم يكن مبنياً على Evidence إضافية فعلية
# من ToolCall ثانٍ منفَّذ فعلياً" — أي إلا إذا كان هناك أكثر من Answer واحد
# (الأسئلة المركّبة، قسم 7). بهذه المرحلة لا تنفيذ فعلي للمرحلة الثانية بعد،
# فالقيد يُطبَّق دائماً على حالة Answer واحدة.

_CAUSAL_MARKERS = ("لأن", "بسبب", "نتيجة ل", "سبب ذلك", "والسبب", "يعود إلى", "يرجع إلى")


def _has_unjustified_causal_claim(text: str, answers: list[Answer]) -> bool:
    if len(answers) > 1:
        return False  # Evidence إضافية فعلية متوفرة من إجابة ثانية حقيقية
    return any(marker in text for marker in _CAUSAL_MARKERS)


# ---------------------------------------------------------------------------
# استدعاء النموذج (نص حر هذه المرة، لا JSON — الناتج شرح لغوي لا ToolCall)


def _default_gemini_text_call(prompt: str, model: str, api_key: str | None) -> str:
    """نص حرّ بلا مخطّط JSON — نفس المزوّد، إعداد مختلف.

    الشبكة في `llm_provider` وحده (القاعدة الذهبية #4).
    """
    return llm_provider.default_provider(api_key).complete(prompt, model=model)


_PROMPT_TEMPLATE = """أنت تصوغ شرحاً عربياً واضحاً بالاعتماد حصراً على الأرقام \
المُعطاة أدناه. ممنوع منعاً باتاً:
- ذكر أي رقم لم يظهر حرفياً بالمُعطيات أدناه.
- أي استنتاج سببي ("لأن..." "بسبب...") لا يعتمد على معطى صريح أدناه.
- اختلاق أي معلومة غير موجودة بالمُعطيات.

إن كانت الصفوف المشمولة قليلة جداً مقارنة بإجمالي البيانات، اذكر ذلك صراحة.

السؤال الأصلي: {question}

النص الحتمي الأصلي (مضمون الدقة، مصدره المحرك مباشرة):
{answer_ar}

المعطيات الكاملة (لا تستخدم أي رقم خارج هذه القائمة):
{evidence_json}

أعد فقرة عربية قصيرة وواضحة فقط، بلا أي مقدمات أو تعليقات إضافية."""


# ---------------------------------------------------------------------------
# إخفاء أسماء الكيانات قبل الإرسال
#
# الوعد المكتوب: «بيانات المستخدم لا تغادر إلى النموذج». كان الوعد أوسع من
# الواقع في مسار واحد: جواب «أعلى 5 عملاء» يحمل أسماء العملاء داخل
# answer_ar، والشرح يُرسل answer_ar كما هو — فأسماء زبائن المستخدم كانت
# تصل فعلاً إلى النموذج. حارس الخصوصية لم يره لأن أسئلته كلها تنتهي
# بـtool=unknown، أي أن مسار الشرح لم يكن يُنفَّذ فيه أصلاً.
#
# الحل: نستبدل كل اسم كيان برمز «كيان-ن» قبل الإرسال، ونعيده بعد الرد.
# النموذج يصوغ جملة عن «كيان-1» ونحن نعيد الاسم محلياً — فالصياغة تتحسّن
# بلا أن يرى المزوّد اسم زبون واحد. الأرقام تبقى كما هي: هي موضوع الشرح،
# وهي معروضة أصلاً على شاشة المستخدم.
#
# ما يبقى صريحاً: **سؤال المستخدم نفسه يُرسل كما كتبه**. لو كتب اسم زبون
# في سؤاله فقد أرسله بنفسه، ولا يمكن للنظام أن يعرف أنه اسم.

_MASK_RE = re.compile(r"«كيان-\d+»")


def _entity_labels(answers: list[Answer]) -> list[str]:
    """أسماء الكيانات كما خرجت من المحرك — لا استخراج تخميني من النص."""
    labels: set[str] = set()
    for a in answers:
        for m in a.metrics:
            value = m.value
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        lab = item.get("label")
                        if isinstance(lab, str):
                            labels.add(lab)
                    elif isinstance(item, (list, tuple)) and item and isinstance(item[0], str):
                        labels.add(item[0])
            for f in (m.evidence.filters_applied or []):
                if not isinstance(f, dict):
                    continue
                for key in ("value", "values"):
                    v = f.get(key)
                    if isinstance(v, str):
                        labels.add(v)
                    elif isinstance(v, (list, tuple)):
                        labels.update(x for x in v if isinstance(x, str))
    # الأطول أولاً: «شركة النور» قبل «النور»، وإلا شُوّه الاسم الأطول
    return sorted((l for l in labels if l and l.strip()), key=len, reverse=True)


def mask_entities(text: str, labels: list[str]) -> tuple[str, dict[str, str]]:
    mapping: dict[str, str] = {}
    for i, label in enumerate(labels, 1):
        if label in text:
            token = f"«كيان-{i}»"
            text = text.replace(label, token)
            mapping[token] = label
    return text, mapping


def unmask_entities(text: str, mapping: dict[str, str]) -> str:
    for token, label in mapping.items():
        text = text.replace(token, label)
    return text


def _evidence_json(answers: list[Answer]) -> str:
    import json
    data = []
    for a in answers:
        for m in a.metrics:
            ev = m.evidence
            data.append({
                "metric_name": ev.metric_name,
                "value": m.value,
                "formatted_ar": m.formatted_ar,
                "unit": m.unit,
                "rows_in_scope": ev.rows_in_scope,
                "rows_total": ev.rows_total,
                "date_range": ev.date_range,
                "filters_applied": ev.filters_applied,
            })
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


@dataclass
class AIExplainer:
    api_key: str | None = field(default_factory=lambda: os.environ.get("GEMINI_API_KEY"))
    model: str = DEFAULT_MODEL
    call_fn: Callable[[str], str] | None = None
    # سقف أمان لا زمن مقبول: نداء بلا مهلة يعلّق الطلب إلى الأبد
    timeout_seconds: float = LLM_TIMEOUT_SECONDS

    # عدّاد نداءات النموذج. الحساب يجري في طبقة الـAPI (هي التي تملك
    # التخزين)، لكن **العدّ** يجب أن يكون هنا: هذا هو المكان الوحيد الذي
    # يعرف يقيناً أن نداءً خرج فعلاً.
    llm_calls: int = 0

    def _call(self, prompt: str) -> str:
        self.llm_calls += 1
        fn = self.call_fn or (
            lambda p: _default_gemini_text_call(p, self.model, self.api_key))
        return call_with_timeout(fn, prompt, self.timeout_seconds)

    def explain(self, question: str, answer: Answer | list[Answer]) -> str:
        """لا استثناء يخرج من هذه الدالة أبداً — أي فشل (شبكة، رقم مختلَق،
        استنتاج سببي غير مبرَّر) يعيد answer_ar الحتمي الأصلي كـfallback آمن.
        هذا النص الحتمي مضمون الصحة أصلاً (قاعدة ذهبية #1) — الفشل هنا لا يعني
        إجابة خاطئة، يعني فقط أن الصياغة الأجمل لم تُستخدَم."""
        answers = answer if isinstance(answer, list) else [answer]
        fallback = " ".join(a.answer_ar for a in answers)

        all_metrics = [m for a in answers for m in a.metrics]
        if not all_metrics:
            return fallback  # لا مقاييس (مثلاً tool=unknown) — لا شيء لنفسّره

        # حالة خاصة صريحة (قسم 8 وقسم 9): rows_in_scope=0 يُذكر حرفياً، بلا أي
        # تدخّل من LLM — answer_ar الحالي مبني أصلاً ليقول هذا بدقة (ToolExecutor).
        if any(m.evidence.rows_in_scope == 0 for m in all_metrics):
            return fallback

        labels = _entity_labels(answers)
        masked_answer, mapping = mask_entities(fallback, labels)
        masked_evidence, ev_map = mask_entities(_evidence_json(answers), labels)
        mapping.update(ev_map)

        prompt = _PROMPT_TEMPLATE.format(
            question=question,
            answer_ar=masked_answer,
            evidence_json=masked_evidence,
        )
        try:
            raw = self._call(prompt)
        except Exception:
            return fallback

        if not isinstance(raw, str) or not raw.strip():
            return fallback

        raw = unmask_entities(raw, mapping)
        if _MASK_RE.search(raw):
            # النموذج اخترع رمزاً لا يقابل كياناً — لا نعرض رمزاً داخلياً
            # للمستخدم، والنص الحتمي جاهز وصحيح
            return fallback

        allowed = _allowed_numbers(answers)
        produced = _numbers_in_text(raw)
        if not produced <= allowed:
            return fallback
        if _has_unjustified_causal_claim(raw, answers):
            return fallback

        return raw.strip()
