"""حارس خصوصية: لا تغادر بيانات المستخدم إلى النموذج أبداً.

هذا وعد قلناه للمستخدم صراحةً («فينيق لا يرسل بياناتك للنموذج»). الوعد بلا
حارس ادّعاء: تعديل صغير في بناء السياق يكفي لتسريب عمود قيم كاملاً، ولن
يفشل أي اختبار آخر.

الطريقة: نُشغّل الطبقة على بيانات فيها قيم **مميّزة لا تلتبس** (أسماء عملاء
ومبالغ نادرة)، ونلتقط النص المُرسَل فعلاً، ثم نبحث فيه عن أثر واحد منها.
"""
from __future__ import annotations

import json

import polars as pl
import pytest

from phoenix.ai_explainer import AIExplainer
from phoenix.llm_intent_resolver import LLMIntentResolver
from phoenix.phoenix_ai import PhoenixAI
from phoenix import pipeline

# قيم لا يمكن أن تظهر صدفةً في نصّ برومبت
SECRETS = ["زقاق_الياسمين_السري", "مؤسسة_البرق_الخاطف", "9876543219"]


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    d = tmp_path_factory.mktemp("leak")
    src = d / "data.csv"
    rows = ["العميل,المنتج,الكمية,سعر الوحدة"]
    for i in range(40):
        who = SECRETS[0] if i == 3 else (SECRETS[1] if i == 7 else f"عميل {i}")
        price = SECRETS[2] if i == 5 else str(1000 + i)
        rows.append(f"{who},منتج {i % 4},{i + 1},{price}")
    src.write_text("\n".join(rows), encoding="utf-8")
    return pipeline.process(src, run_dir=d / "run")


def _capture(run, question: str) -> list[str]:
    """يُشغّل المسار كاملاً ويُرجع كل النصوص المُرسَلة للنموذج."""
    sent: list[str] = []

    def spy_json(prompt: str) -> str:
        sent.append(prompt)
        return json.dumps({"tool": "unknown", "arguments": {}}, ensure_ascii=False)

    def spy_text(prompt: str) -> str:
        sent.append(prompt)
        return "نص"

    engine = run.engine()
    try:
        ai = PhoenixAI(engine=engine, schema=run.schema, profile=run.profile,
                       resolver=LLMIntentResolver(call_fn=spy_json),
                       explainer=AIExplainer(call_fn=spy_text), enabled=True)
        ai.ask(question)
    finally:
        engine.close()
    return sent


class TestNoUserDataInPrompts:
    @pytest.mark.parametrize("question", [
        "شو رأيك بالوضع العام يا فينيق؟",         # غامض ⇒ تصعيد مضمون
        "قارن أداء الفروع مع نمو الأسعار",
        "ليش الأرقام هيك؟",
    ])
    def test_no_cell_value_appears_in_what_is_sent(self, run, question):
        sent = _capture(run, question)
        if not sent:
            pytest.skip("لم يقع تصعيد لهذا السؤال — لا شيء أُرسل أصلاً")
        blob = "\n".join(sent)
        for secret in SECRETS:
            assert secret not in blob, f"تسرّبت قيمة من بيانات المستخدم: {secret}"

    def test_column_names_are_sent_but_not_their_contents(self, run):
        """أسماء الأعمدة تُرسَل عمداً — بها يفهم النموذج السؤال. القيم لا."""
        sent = _capture(run, "شو رأيك بالوضع العام يا فينيق؟")
        if not sent:
            pytest.skip("لم يقع تصعيد")
        blob = "\n".join(sent)
        assert "العميل" in blob, "لم تُرسَل أسماء الأعمدة — كيف سيفهم النموذج؟"
        assert "زقاق_الياسمين_السري" not in blob

    def test_prompt_size_does_not_grow_with_the_number_of_rows(self, tmp_path):
        """أقوى دليل على عدم التسريب: حجم المُرسَل **لا يتعلّق بحجم البيانات**.

        (فحصٌ ساذج بمقارنة حجم البرومبت بحجم الملف لا يصلح: البرومبت يحمل
        التعليمات وعقود الأدوات، فهو أكبر من ملف صغير وأصغر من ملف كبير.
        الثابت الصحيح هو أن يبقى **هو نفسه** مهما كبر الملف.)
        """
        sizes = []
        for rows in (40, 4000):
            src = tmp_path / f"data_{rows}.csv"
            lines = ["العميل,المنتج,الكمية,سعر الوحدة"]
            for i in range(rows):
                lines.append(f"عميل {i},منتج {i % 4},{i + 1},{1000 + i}")
            src.write_text("\n".join(lines), encoding="utf-8")
            run = pipeline.process(src, run_dir=tmp_path / f"run_{rows}")
            sent = _capture(run, "شو رأيك بالوضع العام يا فينيق؟")
            if not sent:
                pytest.skip("لم يقع تصعيد")
            sizes.append(max(len(x) for x in sent))

        small, large = sizes
        assert large <= small * 1.2, (
            f"حجم المُرسَل نما مع البيانات ({small} ← {large}) — مؤشّر تسريب")


class TestEntityNamesDoNotLeakThroughTheExplainer:
    """المسار الذي كان الحارس أعمى عنه.

    كل أسئلة الصنف السابق تنتهي بـ`tool=unknown`، أي أن مسار الشرح لا
    يُنفَّذ فيها أصلاً — فبقي مساراً كاملاً بلا حراسة. وفيه كان التسريب:
    جواب «أعلى 5 عملاء» يحمل أسماء العملاء، والشرح كان يُرسل الجواب كما هو.
    """

    @pytest.fixture(scope="class")
    def run(self, tmp_path_factory):
        """بيانات خاصة: الأسماء السرّية يجب أن تكون **ضمن أعلى خمسة**، وإلا
        لم تظهر بالجواب أصلاً واختبر الحارس فراغاً."""
        d = tmp_path_factory.mktemp("leak_top")
        src = d / "data.csv"
        rows = ["العميل,المنتج,الكمية,سعر الوحدة"]
        for i in range(40):
            who = SECRETS[0] if i == 0 else (SECRETS[1] if i == 1 else f"عميل {i}")
            # كمية ثابتة وسعر تنازلي: الإجمالي = كمية × سعر، فلو تركنا
            # الكمية تكبر مع الفهرس لانقلب الترتيب وخرج السرّان من أعلى خمسة
            price = 9_000 - i * 10
            rows.append(f"{who},منتج {i % 4},1,{price}")
        src.write_text("\n".join(rows), encoding="utf-8")
        return pipeline.process(src, run_dir=d / "run")

    def _top_n_answer(self, run):
        from phoenix.ask import AskPhoenix
        engine = run.engine()
        try:
            return AskPhoenix(engine=engine, schema=run.schema).ask("مين أكتر 5 عملاء شراءً؟")
        finally:
            engine.close()

    def test_a_real_answer_carries_the_names(self, run):
        """تحقّق أوّلي: لولا هذا لكان الاختبار التالي ينجح بلا معنى."""
        answer = self._top_n_answer(run)
        assert SECRETS[0] in answer.answer_ar or SECRETS[1] in answer.answer_ar, (
            "الجواب لا يحمل أسماء أصلاً — الاختبار التالي يصير بلا قيمة")

    def test_names_are_masked_before_the_prompt_leaves(self, run):
        from phoenix.ai_explainer import AIExplainer
        answer = self._top_n_answer(run)
        sent: list[str] = []
        AIExplainer(call_fn=lambda p: (sent.append(p), "شرح مختصر")[1]).explain(
            "مين أهم الزباين؟", [answer])
        assert sent, "لم يُستدعَ النموذج — الاختبار لا يفحص شيئاً"
        blob = sent[0]
        for secret in SECRETS[:2]:
            assert secret not in blob, f"اسم كيان غادر إلى النموذج: {secret}"
        assert "«كيان-" in blob, "لم يُستبدل الاسم برمز — ما الذي أُرسل إذن؟"

    def test_names_come_back_to_the_user_unchanged(self, run):
        """الإخفاء لا يجوز أن يُفسد ما يراه المستخدم."""
        from phoenix.ai_explainer import AIExplainer
        answer = self._top_n_answer(run)
        out = AIExplainer(
            call_fn=lambda p: "أعلى العملاء «كيان-1» ثم «كيان-2»."
        ).explain("مين أهم الزباين؟", [answer])
        assert "«كيان-" not in out, "رمز داخلي ظهر للمستخدم"
        assert any(s in out for s in SECRETS[:2]), f"لم يُعَد الاسم: {out}"

    def test_an_invented_mask_token_falls_back_instead_of_showing_a_code(self, run):
        from phoenix.ai_explainer import AIExplainer
        answer = self._top_n_answer(run)
        out = AIExplainer(call_fn=lambda p: "أعلى العملاء «كيان-99».").explain(
            "س", [answer])
        assert out == answer.answer_ar, "رمز مخترَع يجب أن يُسقط الصياغة كلها"
