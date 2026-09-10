"""تمييز العدد العربي — «3 صف» خطأ يراه كل قارئ عربي فوراً.

المنتج كلّه يَعِد بعربية سليمة، والنص الذي يقول «حذف 3 صف مكرر» يُسقط تلك
الثقة في سطر واحد. لا يكشفه أي اختبار أرقام: الحساب صحيح واللغة مكسورة.

الحارس هنا **يفحص المخرجات الفعلية** لخط الأنابيب لا الشيفرة: كل نص عربي
يراه المستخدم يُمسح بحثاً عن «عدد بين 3 و10 يليه مفرد».
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from phoenix import pipeline
from phoenix.arabic_text import count_ar
from phoenix.ask import AskPhoenix


class TestCountAr:
    @pytest.mark.parametrize("n,expected", [
        (1, "خلية"),
        (2, "خليتان"),
        (3, "3 خلايا"),
        (9, "9 خلايا"),
        (10, "10 خلايا"),
        (11, "11 خلية"),
        (120, "120 خلية"),
        (1500, "1,500 خلية"),
    ])
    def test_the_four_arabic_forms(self, n, expected):
        """العربية تغيّر المعدود أربع مرات لا مرّتين."""
        assert count_ar(n, "خلية", "خليتان", "خلايا") == expected

    def test_zero_uses_the_singular_form(self):
        # «0 خلية» صحيح عربياً، و«0 خلايا» خطأ
        assert count_ar(0, "خلية", "خليتان", "خلايا") == "0 خلية"


SINGULARS = ["صف", "خلية", "عمود", "قيمة", "اسم", "فئة", "نقطة", "صنف",
             "مجموعة", "عميل", "منتج", "يوم", "شهر"]
BAD = re.compile(rf"(?<!\d)([3-9]|10)\s+({'|'.join(SINGULARS)})(?=[\s.,،:؛)«»]|$)")


def offenders(text: str) -> list[str]:
    return [m.group(0) for m in BAD.finditer(text or "")]


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    """ملف صغير عمداً: الأعداد الصغيرة (3–10) هي موضع الخطأ."""
    d = tmp_path_factory.mktemp("counting")
    src = d / "small.csv"
    rows = ["اسم الصنف,العميل,التاريخ,الكمية,سعر الوحدة,ملاحظات"]
    names = ["قلم", "دفتر", "ممحاة", "مسطرة", "حقيبة"]
    # الملف مُتعمَّد الاتساخ: كل نوع من مشاكل الجودة بعدد **بين 3 و10** —
    # وهي المنطقة التي يظهر فيها خطأ الجمع. ملفٌ نظيف كان سيجعل فحص رسائل
    # الجودة يمرّ على فراغ (وقع ذلك فعلاً، وأمسكه شرط «لا رسائل أصلاً»).
    for i in range(14):
        name = names[i % 5] + (" " if i < 3 else "")          # 3 قيم بمسافة زائدة
        qty = "" if i in (4, 7, 11) else str(i + 1)            # 3 قيم مفقودة
        price = -50 - i if i in (5, 8, 12) else 100 + i        # 3 قيم سالبة
        note = "٣ علب" if i < 4 else "علبة"                    # 4 قيم بأرقام هندية
        rows.append(f"{name},عميل {i % 4},2026-0{i % 8 + 1}-05,{qty},{price},{note}")
    # ثلاثة صفوف مكرّرة تماماً — العدد 3 مقصود: صفّان يقعان في المثنّى
    # فيمرّان من الحارس بلا فحص، والخطأ الحقيقي يبدأ من الثلاثة
    for _ in range(3):
        rows.append(rows[-1])
    src.write_text("\n".join(rows), encoding="utf-8")
    return pipeline.process(src, run_dir=d / "run")


class TestGeneratedArabicUsesCorrectPlurals:
    def test_cleaning_changelog(self, run):
        bad = [(r.operation_type, o)
               for r in run.changelog.results for o in offenders(r.summary_ar)]
        assert bad == [], f"مفرد بعد عدد صغير في سجل التنظيف: {bad}"

    def test_insights(self, run):
        bad = [(i.type, o) for i in run.insights
               for o in offenders(i.title_ar) + offenders(i.description_ar)]
        assert bad == [], f"مفرد بعد عدد صغير في الاكتشافات: {bad}"

    def test_chart_reasons(self, run):
        engine = run.engine()
        try:
            from phoenix import charts
            specs = charts.suggest_charts(engine)
        finally:
            engine.close()
        bad = [(s.title_ar, o) for s in specs
               for o in offenders(s.reason_ar) + offenders(s.title_ar)]
        assert bad == [], f"مفرد بعد عدد صغير في تفسير الرسوم: {bad}"

    def test_answers(self, run):
        engine = run.engine()
        try:
            ask = AskPhoenix(engine=engine, schema=run.schema)
            texts = [ask.ask(q).answer_ar for q in
                     ["كم إجمالي المبيعات؟", "مين أكتر 5 أصناف مبيعاً؟",
                      "شو توزيع المبيعات حسب العميل؟"]]
        finally:
            engine.close()
        bad = [(t[:40], o) for t in texts for o in offenders(t)]
        assert bad == [], f"مفرد بعد عدد صغير في الإجابات: {bad}"

    def test_ingestion_warnings(self, run):
        bad = [o for w in run.file_info.warnings for o in offenders(w)]
        assert bad == [], f"مفرد بعد عدد صغير في تحذيرات القراءة: {bad}"

    def test_column_quality_issues(self, run):
        """رسائل الجودة تظهر في بطاقة كل عمود — وهي أكثر ما يقرؤه المستخدم
        بعد الأرقام. «1 قيمة شاذة» و«5 قيمة مفقودة» ظهرتا فعلاً على الشاشة."""
        msgs = [i.message_ar for c in run.profile.columns for i in c.quality_issues]
        msgs += [i.message_ar for i in getattr(run.profile, "issues", [])]
        assert msgs, "لا رسائل جودة أصلاً — الاختبار يفحص فراغاً"
        bad = [(m[:45], o) for m in msgs for o in offenders(m)]
        assert bad == [], f"مفرد بعد عدد صغير في مشاكل الجودة: {bad}"


class TestTheGuardItselfCatchesTheMistake:
    """حارسٌ لا يفشل على الخطأ الذي وُجد لأجله ليس حارساً."""

    def test_detects_the_classic_error(self):
        assert offenders("حذف 3 صف مكرر") == ["3 صف"]
        assert offenders("9 نقطة زمنية") == ["9 نقطة"]

    def test_does_not_flag_correct_arabic(self):
        assert offenders("حذف 3 صفوف مكررة") == []
        assert offenders("120 صف") == []
        assert offenders("1,500 خلية") == []


class TestCountLabelsUsePlurals:
    """«عدد العميل» أول ما يراه المستخدم في بطاقة المؤشرات."""

    def test_known_concepts_have_plural_labels(self):
        from phoenix.charts import label_plural_ar
        assert label_plural_ar("customer") == "العملاء"
        assert label_plural_ar("product_name") == "المنتجات"
        assert label_plural_ar("region") == "المناطق"

    def test_unknown_concept_falls_back_to_the_singular(self):
        """مفهوم جديد يعطي عنواناً ركيكاً لا خاطئاً — والسقوط صامت مقصود."""
        from phoenix.charts import label_plural_ar
        assert label_plural_ar("مفهوم_جديد") == "مفهوم_جديد"

    def test_a_dimension_kpi_reads_correctly(self, tmp_path):
        from phoenix import charts, pipeline
        src = tmp_path / "one.csv"
        src.write_text("اسم العميل\n" + "\n".join(f"عميل {i}" for i in range(20)),
                       encoding="utf-8")
        run = pipeline.process(src, run_dir=tmp_path / "run")
        engine = run.engine()
        try:
            labels = [k["label_ar"] for k in charts.build_kpis(engine)]
        finally:
            engine.close()
        assert "عدد العملاء" in labels, labels
        assert "عدد العميل" not in labels
