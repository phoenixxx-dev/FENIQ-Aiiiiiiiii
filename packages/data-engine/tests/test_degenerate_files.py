"""ملفات على الحدّ: عمود واحد، صف واحد، عمود فارغ بالكامل، قيم متطابقة.

المستخدم لا يرفع دائماً جدول مبيعات مثالياً. يرفع أحياناً عمود أسماء فقط،
أو ملفاً فيه صف واحد جرّبه ليرى ماذا يحدث. الانهيار هنا ليس «حالة نادرة»:
هو أول انطباع عند نصف المستخدمين الذين يجرّبون بملف صغير أولاً.

القاعدة المفحوصة: **لا انهيار، ولا شاشة فارغة بلا سبب** — إمّا نتيجة أو
رسالة عربية مفهومة.
"""
from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from phoenix import charts, pipeline
from phoenix.ask import AskPhoenix
from phoenix.ingestion import IngestionError


def write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


def full_run(src: Path, tmp_path: Path):
    """خط الأنابيب كاملاً + اللوحة + سؤال — أي انهيار في أي طبقة يظهر هنا."""
    run = pipeline.process(src, run_dir=tmp_path / "run")
    engine = run.engine()
    try:
        kpis = charts.build_kpis(engine)
        specs = charts.suggest_charts(engine)
        answer = AskPhoenix(engine=engine, schema=run.schema).ask("كم إجمالي المبيعات؟")
    finally:
        engine.close()
    return run, kpis, specs, answer


class TestOneColumn:
    def test_a_single_text_column_does_not_crash(self, tmp_path):
        src = write(tmp_path, "one_col.csv",
                    "اسم العميل\n" + "\n".join(f"عميل {i}" for i in range(20)))
        run, kpis, specs, answer = full_run(src, tmp_path)

        df = pl.read_parquet(run.processed_path)
        assert df.width == 1 and df.height == 20
        # لا مقياس عددي ⇒ لا مؤشرات مالية، لكن يجب أن يبقى شيء يُعرض
        assert isinstance(kpis, list)
        assert isinstance(specs, list)
        # والسؤال المالي يجب أن يُجاب بصدق: «لا يوجد» لا رقم مخترع
        assert answer.answer_ar.strip(), "إجابة فارغة — شاشة بيضاء للمستخدم"

    def test_a_single_numeric_column_still_gives_a_number(self, tmp_path):
        src = write(tmp_path, "nums.csv",
                    "المبلغ\n" + "\n".join(str(100 + i) for i in range(20)))
        run, kpis, specs, answer = full_run(src, tmp_path)
        assert pl.read_parquet(run.processed_path).height == 20
        # المؤشرات يجب أن تحمل رقماً حقيقياً لا صفراً افتراضياً
        assert kpis, "عمود أرقام واضح بلا أي مؤشر"


class TestOneRow:
    def test_a_single_data_row_survives_the_whole_pipeline(self, tmp_path):
        src = write(tmp_path, "one_row.csv",
                    "الصنف,الكمية,سعر الوحدة\nقلم,5,100")
        run, kpis, specs, answer = full_run(src, tmp_path)
        df = pl.read_parquet(run.processed_path)
        assert df.height == 1, f"ضاع الصف الوحيد: {df.height}"
        assert answer.answer_ar.strip()

    def test_header_only_file_is_rejected_with_an_arabic_message(self, tmp_path):
        """ملف عناوين بلا بيانات: الرفض مقبول، الانهيار الصامت لا."""
        src = write(tmp_path, "header_only.csv", "الصنف,الكمية,سعر الوحدة\n")
        try:
            run = pipeline.process(src, run_dir=tmp_path / "run")
        except IngestionError as e:
            assert any("؀" <= ch <= "ۿ" for ch in str(e)), (
                f"رسالة غير عربية: {e}")
            return
        assert pl.read_parquet(run.processed_path).height == 0


class TestEmptyAndConstantColumns:
    def test_a_fully_empty_column_does_not_break_profiling(self, tmp_path):
        rows = "\n".join(f"صنف {i},{i + 1}," for i in range(15))
        src = write(tmp_path, "empty_col.csv", f"الصنف,الكمية,ملاحظات\n{rows}")
        run, kpis, specs, answer = full_run(src, tmp_path)
        # العمود الفارغ إمّا يُحذف أو يبقى — المهم ألا ينهار شيء وأن تبقى البيانات
        df = pl.read_parquet(run.processed_path)
        assert df.height == 15
        assert "الصنف" in df.columns and "الكمية" in df.columns

    def test_a_constant_column_is_reported_not_crashed(self, tmp_path):
        rows = "\n".join(f"صنف {i},10,دمشق" for i in range(15))
        src = write(tmp_path, "const.csv", f"الصنف,الكمية,المدينة\n{rows}")
        run, kpis, specs, answer = full_run(src, tmp_path)
        assert pl.read_parquet(run.processed_path).height == 15
        # عمود ثابت مشكلة جودة معروفة — يُفترض أن يُذكر لا أن يُتجاهل
        kinds = {i.kind for c in run.profile.columns for i in c.quality_issues}
        assert kinds, "لم تُرصد أي مشكلة جودة رغم وجود عمود ثابت وعمود بلا تنوّع"


class TestIdenticalRows:
    def test_all_rows_identical_leaves_one_and_says_so(self, tmp_path):
        rows = "\n".join("قلم,5,100" for _ in range(12))
        src = write(tmp_path, "same.csv", f"الصنف,الكمية,سعر الوحدة\n{rows}")
        run, kpis, specs, answer = full_run(src, tmp_path)
        df = pl.read_parquet(run.processed_path)
        assert df.height == 1, f"التكرار الكامل لم يُزل: {df.height}"
        dedup = [r for r in run.changelog.results if r.operation_type == "deduplicate_rows"]
        assert dedup and dedup[0].summary_ar.strip(), (
            "أُزيل 11 صفاً بلا أي سطر يشرح ذلك للمستخدم")


class TestTheOneColumnFixDidNotOpenTheDoor:
    """قبول العمود الواحد يجب ألا يعيد العيب الذي أُصلح سابقاً: ملف غير
    جدولي يُقبل بصمت فيُنتج المحرك هراءً ويعلن النجاح."""

    def test_a_prose_text_file_is_still_rejected(self, tmp_path):
        from phoenix.ingestion import detect_format
        src = write(tmp_path, "note.txt", "\n".join([
            "هذا ملف ملاحظات عادي كتبه المستخدم بلغة طبيعية ولا يحمل أي بيانات جدولية.",
            "والسطر الثاني جملة كاملة فيها كلمات كثيرة لا تصلح أن تكون قيمة في عمود.",
            "أما الثالث فشرح مطوّل عن موضوع لا علاقة له بجدول بيانات إطلاقاً.",
        ]))
        with pytest.raises(IngestionError):
            detect_format(src)

    def test_a_real_one_column_list_is_accepted(self, tmp_path):
        from phoenix.ingestion import detect_format
        src = write(tmp_path, "names.csv",
                    "اسم العميل\nشركة النور\nمؤسسة الفجر\nمتجر السلام\n")
        assert detect_format(src) == "csv"
