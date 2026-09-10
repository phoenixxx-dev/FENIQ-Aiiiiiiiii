"""حالات «الضياع الصامت» — بيانات تُفقد أو تُبتر بلا أي رسالة.

هذه أخطر فئة عيوب في هذا المنتج: الانهيار يُرى ويُصلَح، أما التقرير الكامل
الشكل الناقص المعنى فيُبنى عليه قرار. كل اختبار هنا يقارن ما دخل بما خرج.
"""
from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from phoenix import pipeline


def write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


class TestRaggedRows:
    def test_extra_fields_are_announced_not_truncated_silently(self, tmp_path):
        """فاصلة داخل نص غير محاط باقتباس تدفع حقلاً زائداً فيُبتر.

        البتر نفسه سلوك مقبول (البديل رفض الملف كله)، لكن الصمت غير مقبول:
        قيمة اختفت من بيانات المستخدم.
        """
        src = write(tmp_path, "ragged.csv",
                    "الصنف,الكمية,السعر\n"
                    "قلم,5,100\n"
                    "دفتر,3,200,ملاحظة زائدة\n"
                    "ممحاة,2,50\n")
        run = pipeline.process(src, run_dir=tmp_path / "run")

        joined = " ".join(run.file_info.warnings)
        assert "أكثر من عدد الأعمدة" in joined, f"لا تحذير: {run.file_info.warnings}"
        assert "3" in joined, "التحذير لا يذكر رقم الصف المتأثر"
        # ومع ذلك تبقى بقية البيانات سليمة
        df = pl.read_parquet(run.processed_path)
        assert df.height == 3

    def test_a_clean_file_gets_no_false_alarm(self, tmp_path):
        """فواصل داخل اقتباسات شرعية تماماً — إنذار كاذب هنا يُفقد التحذير قيمته."""
        src = write(tmp_path, "quoted.csv",
                    'الصنف,المنطقة,السعر\n'
                    '"قلم","الشام، دمشق",100\n'
                    '"دفتر","حلب، السبيل",200\n')
        run = pipeline.process(src, run_dir=tmp_path / "run")
        assert not [w for w in run.file_info.warnings if "أكثر من عدد الأعمدة" in w]


class TestUnparseableNumbers:
    def test_values_that_could_not_be_converted_are_reported(self, tmp_path):
        """«غير محدد» في عمود كمية تصير فارغة — يجب أن يُقال ذلك صراحةً.

        بلا هذا: المستخدم يرى ثقوباً في عموده بلا سبب، والمجموع محسوب على
        أقل مما رفع.
        """
        rows = "\n".join(f"صنف{i},{i}" for i in range(1, 30))
        src = write(tmp_path, "mixed.csv",
                    f"الصنف,الكمية\n{rows}\nصنف30,غير محدد\nصنف31,لا يوجد\n")
        run = pipeline.process(src, run_dir=tmp_path / "run")

        parse = [r for r in run.changelog.results if r.operation_type == "parse_numbers"]
        assert parse, "لم تُنفَّذ عملية تحويل الأرقام أصلاً"
        assert "تعذّر تحويلها" in parse[0].summary_ar, parse[0].summary_ar
        # ومثال ملموس يُعرض للمستخدم في شاشة «ماذا تغيّر»
        assert any(e.get("after") == "(فارغ)" for e in parse[0].examples)

    def test_a_fully_parseable_column_reports_no_failures(self, tmp_path):
        src = write(tmp_path, "clean_numbers.csv",
                    "الصنف,السعر\n" + "\n".join(
                        f'صنف{i},"1,{i:03d}.50 ر.س"' for i in range(1, 20)) + "\n")
        run = pipeline.process(src, run_dir=tmp_path / "run")
        parse = [r for r in run.changelog.results if r.operation_type == "parse_numbers"]
        if parse:
            assert "تعذّر تحويلها" not in parse[0].summary_ar
        df = pl.read_parquet(run.processed_path)
        assert df["السعر"].null_count() == 0


class TestRowAndColumnCountsSurvive:
    @pytest.mark.parametrize("name,content,rows,cols", [
        ("plain.csv", "أ,ب\n1,2\n3,4\n5,6\n", 3, 2),
        ("with_spaces.csv", "أ,ب\n 1 , 2 \n 3 , 4 \n", 2, 2),
    ])
    def test_nothing_disappears_on_ordinary_files(self, tmp_path, name, content,
                                                  rows, cols):
        """حارس أساسي: لا صف ولا عمود يختفي من ملف عادي تماماً."""
        run = pipeline.process(write(tmp_path, name, content), run_dir=tmp_path / "run")
        df = pl.read_parquet(run.processed_path)
        assert df.height == rows
        assert df.width == cols
