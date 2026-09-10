"""العملة — لا تُخمَّن، ولا تُجمع عملتان (ق-49).

عيبان حقيقيان اجتمعا في هذا الملف:

**الأول:** `fmt_currency` كانت قيمتها الافتراضية «ر.س». فكل مبلغ في المنتج — من
أول يوم — كان يظهر بعملة السعودية. على ملف مستودع في اللاذقية يعني ذلك رقماً
صحيحاً بعملة كاذبة، وهو أخطر من رقم بلا عملة لأن لا أحد يشكّ فيه. ولم يكشفه أي
اختبار: كل الاختبارات كانت تفحص **الأرقام**، ولا اختبار واحد يفحص ما بجانبها.

**الثاني — الأخطر في هذا السوق:** أكثر التجار هنا يتعاملون بالدولار وبالليرة
معاً. ملفٌ فيه العمودان، ومجموعٌ واحد فوقهما: 120,000 + 45,000,000 = 45,120,000.
رقم بلا معنى، منسّق، كبير، ويبدو معقولاً. هذا النوع من الأخطاء لا يُكتشف — يُصدَّق.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from phoenix import charts, pipeline  # noqa: E402
from phoenix.arabic_text import fmt_currency  # noqa: E402
from phoenix.ask import AskPhoenix  # noqa: E402
from phoenix.currency import detect_in_text  # noqa: E402


class TestReadingTheCurrencyFromText:
    @pytest.mark.parametrize("text,code", [
        ("1,250.00 ل.س", "SYP"),
        ("$120", "USD"),
        ("120 €", "EUR"),
        ("999 ر.س", "SAR"),
        ("د.ك 90", "KWD"),
        ("Price USD", "USD"),
        ("المبلغ (SYP)", "SYP"),
    ])
    def test_symbols_and_codes(self, text, code):
        assert detect_in_text(text) == code

    @pytest.mark.parametrize("text,code", [
        ("السعر بالدولار", "USD"),
        ("قيمة بالليرة السورية", "SYP"),
        ("بالليرة التركية", "TRY"),
    ])
    def test_arabic_prefixes_do_not_hide_the_word(self, text, code):
        """«بالدولار» = بـ + الـ + دولار. بلا نزع السوابق لا يطابق أي اسم عمود
        عربي طبيعي — وأسماء الأعمدة العربية هكذا تُكتب دائماً."""
        assert detect_in_text(text) == code

    def test_a_qualified_lira_beats_the_bare_one(self):
        assert detect_in_text("ليرة تركية") == "TRY"
        assert detect_in_text("ليرة لبنانية") == "LBP"

    def test_a_bare_dinar_is_refused(self):
        """«دينار» أردني أو عراقي أو كويتي — والفرق مئات الأضعاف. لا نخمّن."""
        assert detect_in_text("5 دينار") is None
        assert detect_in_text("دينار") is None

    def test_ordinary_column_names_are_not_currencies(self):
        for name in ("الإجمالي", "الكمية", "اسم المندوب", "التاريخ", "الصنف"):
            assert detect_in_text(name) is None, name


class TestNoSymbolWithoutEvidence:
    def test_the_formatter_adds_nothing_by_itself(self):
        assert fmt_currency(1234.5) == "1,234.50"
        assert fmt_currency(1234.5, "ل.س") == "1,234.50 ل.س"

    def test_a_file_with_no_currency_evidence_shows_bare_numbers(self, tmp_path):
        src = tmp_path / "plain.csv"
        rows = ["الصنف,الكمية,سعر الوحدة"]
        rows += [f"صنف {i},{i % 7 + 1},{100 + i}" for i in range(30)]
        src.write_text("\n".join(rows), encoding="utf-8")
        run = pipeline.process(src, run_dir=tmp_path / "run")
        assert not run.schema.currency.known
        engine = run.engine()
        try:
            kpis = charts.build_kpis(engine)
        finally:
            engine.close()
        money = [k["formatted_ar"] for k in kpis if k["unit"] == "currency"]
        assert money, "لا مؤشّر مالي أصلاً — الاختبار يفحص فراغاً"
        for text in money:
            assert "ر.س" not in text, f"عملة مخترَعة: {text}"

    def test_the_real_syrian_file_carries_no_saudi_symbol(self):
        """الملف الذي كشف العيب: كتالوج مستودع في اللاذقية."""
        real = (Path(__file__).resolve().parents[1] / "fixtures" / "real"
                / "feniqsync_catalog.json")
        run = pipeline.process(real, run_dir=Path("/tmp/cur_real_run"))
        engine = run.engine()
        try:
            kpis = charts.build_kpis(engine)
        finally:
            engine.close()
        joined = " | ".join(k["formatted_ar"] for k in kpis)
        assert "ر.س" not in joined, f"عملة السعودية على بيانات سورية: {joined}"


class TestEvidenceBringsTheSymbolBack:
    def _run(self, tmp_path, rows, name="f.csv"):
        src = tmp_path / name
        src.write_text("\n".join(rows), encoding="utf-8")
        return pipeline.process(src, run_dir=tmp_path / "run")

    def test_currency_written_inside_the_values(self, tmp_path):
        rows = ["الصنف,الكمية,سعر الوحدة"]
        rows += [f'صنف {i},{i % 7 + 1},"{1000 + i}.00 ل.س"' for i in range(30)]
        run = self._run(tmp_path, rows)
        assert run.schema.currency.code == "SYP"
        assert run.schema.currency.source == "values"

    def test_currency_written_in_the_column_name(self, tmp_path):
        rows = ["الصنف,الكمية,السعر بالدولار"]
        rows += [f"صنف {i},{i % 7 + 1},{100 + i}" for i in range(30)]
        run = self._run(tmp_path, rows)
        assert run.schema.currency.code == "USD"
        engine = run.engine()
        try:
            money = [k["formatted_ar"] for k in charts.build_kpis(engine)
                     if k["unit"] == "currency"]
        finally:
            engine.close()
        assert money and all("$" in m for m in money), money

    def test_an_explicit_currency_column(self, tmp_path):
        rows = ["الصنف,العملة,الكمية,سعر الوحدة"]
        rows += [f"صنف {i},ل.س,{i % 7 + 1},{1000 + i}" for i in range(30)]
        run = self._run(tmp_path, rows)
        assert run.schema.currency.code == "SYP"
        assert run.schema.currency.source == "column"
        assert not run.schema.currency.mixed


class TestTwoCurrenciesAreNeverAddedTogether:
    """أهم صفّ في الملف. السوق هنا يتعامل بالعملتين، فهذه هي الحالة العادية."""

    @pytest.fixture(scope="class")
    def mixed(self, tmp_path_factory):
        d = tmp_path_factory.mktemp("mixed")
        src = d / "mixed.csv"
        rows = ["العميل,العملة,الكمية,سعر الوحدة"]
        for i in range(40):
            usd = i % 2
            rows.append(f"عميل {i},{'USD' if usd else 'ل.س'},1,{100 if usd else 1_000_000}")
        src.write_text("\n".join(rows), encoding="utf-8")
        return pipeline.process(src, run_dir=d / "run")

    def test_the_file_is_recognised_as_mixed(self, mixed):
        assert mixed.schema.currency.mixed
        assert set(mixed.schema.currency.codes) == {"SYP", "USD"}
        assert mixed.schema.currency.symbol_ar is None, "رمز واحد لملف بعملتين"

    def test_the_total_is_split_not_summed(self, mixed):
        engine = mixed.engine()
        try:
            total = engine.aggregate("total_amount", "sum")
        finally:
            engine.close()
        assert isinstance(total.value, dict), f"مجموع واحد فوق عملتين: {total.value}"
        assert total.value["SYP"] == pytest.approx(20_000_000)
        assert total.value["USD"] == pytest.approx(2_000)
        assert "ل.س" in total.formatted_ar and "$" in total.formatted_ar
        # الرقم الكارثي: 20,002,000 — مجموعٌ لا يقابل شيئاً في الواقع
        assert "20,002,000" not in total.formatted_ar

    def test_the_answer_to_a_question_is_split_too(self, mixed):
        """الحارس على المؤشّر وحده لا يكفي — المستخدم يسأل بالعربية أيضاً."""
        engine = mixed.engine()
        try:
            answer = AskPhoenix(engine=engine, schema=mixed.schema).ask("كم إجمالي المبيعات؟")
        finally:
            engine.close()
        assert "ل.س" in answer.answer_ar and "$" in answer.answer_ar, answer.answer_ar

    def test_the_evidence_says_why(self, mixed):
        engine = mixed.engine()
        try:
            ev = engine.aggregate("total_amount", "sum").evidence
        finally:
            engine.close()
        assert "GROUP BY" in ev.sql.upper(), "الدليل لا يُظهر التقسيم — زر «كيف حُسب؟» يكذب"

    def test_a_single_currency_file_still_gets_one_number(self, tmp_path):
        """الحدّ المقابل: لولاه لكان الحلّ «لا نجمع أبداً»، وهو تعطيل لا إصلاح."""
        src = tmp_path / "one.csv"
        rows = ["العميل,العملة,الكمية,سعر الوحدة"]
        rows += [f"عميل {i},ل.س,1,{1_000_000}" for i in range(40)]
        src.write_text("\n".join(rows), encoding="utf-8")
        run = pipeline.process(src, run_dir=tmp_path / "run")
        engine = run.engine()
        try:
            total = engine.aggregate("total_amount", "sum")
        finally:
            engine.close()
        assert total.value == pytest.approx(40_000_000)
        assert total.formatted_ar == "40,000,000.00 ل.س"


class TestAPriceColumnIsNotAnExchangeRate:
    """«سعر الدولار» = كم تساوي العملة. «السعر بالدولار» = الثمن مقوَّماً بها.
    الفرق حرفُ جرّ واحد، والتشابه النصّي بينهما فوق 0.85 — فكان القاموس
    الضبابي يخلط بينهما، فيضيع كون العمود مبلغاً أصلاً ومعه عملته."""

    def _cols(self, tmp_path, header):
        src = tmp_path / "p.csv"
        rows = [header] + [f"صنف {i},{i % 7 + 1},{100 + i}" for i in range(30)]
        src.write_text("\n".join(rows), encoding="utf-8")
        run = pipeline.process(src, run_dir=tmp_path / "run")
        return {c.column_name: c for c in run.schema.columns}, run

    def test_priced_in_dollars_is_money(self, tmp_path):
        cols, run = self._cols(tmp_path, "الصنف,الكمية,السعر بالدولار")
        c = cols["السعر بالدولار"]
        assert c.concept == "unit_price" and c.unit == "currency"
        assert c.currency == "USD"
        assert run.schema.currency.code == "USD"

    def test_a_true_exchange_rate_is_left_alone(self, tmp_path):
        """الحدّ المقابل: بلا هذا يصير الحلّ «لا وجود لسعر صرف»."""
        cols, _ = self._cols(tmp_path, "الصنف,الكمية,سعر الدولار")
        assert cols["سعر الدولار"].concept == "exchange_rate"


class TestTwoPriceColumnsInTwoCurrencies:
    """قائمة أسعار بعمودين — بالدولار وبالليرة — أمرٌ يومي في هذا السوق."""

    @pytest.fixture(scope="class")
    def run(self, tmp_path_factory):
        d = tmp_path_factory.mktemp("two_cols")
        src = d / "list.csv"
        rows = ["الصنف,السعر بالدولار,السعر بالليرة السورية"]
        rows += [f"صنف {i},{10 + i},{(10 + i) * 13000}" for i in range(30)]
        src.write_text("\n".join(rows), encoding="utf-8")
        return pipeline.process(src, run_dir=d / "run")

    def test_each_column_keeps_its_own_currency(self, run):
        cols = {c.column_name: c for c in run.schema.columns}
        assert cols["السعر بالدولار"].currency == "USD"
        assert cols["السعر بالليرة السورية"].currency == "SYP"

    def test_the_file_has_no_single_currency(self, run):
        assert run.schema.currency.mixed
        assert run.schema.currency.symbol_ar is None
        assert set(run.schema.currency.codes) == {"USD", "SYP"}

    def test_a_computed_amount_carries_its_own_column_symbol(self, run):
        """الرقم يأخذ عملة **عموده**، لا عملة الملف — فالملف بلا عملة واحدة."""
        from phoenix.currency import symbol_of
        engine = run.engine()
        try:
            res = engine.aggregate("unit_price", "avg")
        finally:
            engine.close()
        chosen = run.schema.by_concept("unit_price")
        expected = symbol_of(chosen.currency)
        assert expected and expected in res.formatted_ar, (
            f"«{chosen.column_name}» عملته {chosen.currency} والناتج: {res.formatted_ar}")
        others = {"$", "ل.س"} - {expected}
        for sym in others:
            assert sym not in res.formatted_ar, f"عملتان في رقم واحد: {res.formatted_ar}"

    def test_prices_are_never_summed_into_a_headline_number(self, run):
        """مجموع أسعار الوحدة بلا معنى أصلاً — ومجموعها عبر عملتين أسوأ.
        المؤشرات لا تعرض مبلغاً هنا، وهذا هو الصواب لا نقص."""
        engine = run.engine()
        try:
            money = [k["formatted_ar"] for k in charts.build_kpis(engine)
                     if k["unit"] == "currency"]
        finally:
            engine.close()
        assert money == [], f"مبلغ في الرأس على قائمة أسعار بعملتين: {money}"
