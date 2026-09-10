"""خلايا إكسل مختلطة الأنواع — نفس فئة «الضياع الصامت» لكن في مسار XLSX.

في CSV اكتُشف أن عموداً كاملاً قد يُفرَّغ بصمت. المسار الآخر (XLSX عبر
calamine) يستحق نفس السؤال: ماذا يحدث لعمود أرقام فيه خلايا نصّية؟
"""
from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from phoenix import pipeline

openpyxl = pytest.importorskip("openpyxl")


def build(tmp_path: Path, rows: list[list]) -> Path:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "بيانات"
    for r in rows:
        ws.append(r)
    path = tmp_path / "mixed.xlsx"
    wb.save(path)
    return path


class TestMixedTypeColumn:
    def test_text_cells_inside_a_numeric_column_do_not_erase_the_column(self, tmp_path):
        """عمود كمية أغلبه أرقام وفيه «غير محدد» — يجب أن تبقى الأرقام."""
        rows = [["الصنف", "الكمية", "سعر الوحدة"]]
        for i in range(1, 25):
            rows.append([f"صنف {i}", i, 1000 * i])
        rows.append(["صنف 25", "غير محدد", 25000])
        rows.append(["صنف 26", "", 26000])

        run = pipeline.process(build(tmp_path, rows), run_dir=tmp_path / "run")
        df = pl.read_parquet(run.processed_path)

        assert "الكمية" in df.columns, "العمود اختفى كلياً"
        assert df.height == 26, f"ضاعت صفوف: {df.height}"
        # 24 قيمة رقمية صالحة يجب أن تنجو مهما كان مصير الخليتين الأخيرتين
        assert df["الكمية"].drop_nulls().len() >= 24
        assert df["الكمية"].sum() == sum(range(1, 25))

    def test_numbers_stored_as_text_are_still_summed(self, tmp_path):
        """إكسل عربي كثيراً ما يخزّن الأرقام كنصوص («'1500»)."""
        rows = [["الصنف", "الكمية"]]
        for i in range(1, 15):
            rows.append([f"صنف {i}", str(i)])

        run = pipeline.process(build(tmp_path, rows), run_dir=tmp_path / "run")
        df = pl.read_parquet(run.processed_path)

        assert df["الكمية"].dtype in (pl.Int64, pl.Float64), (
            "بقيت الكميات نصوصاً — لن تُجمع في أي تحليل")
        assert df["الكمية"].sum() == sum(range(1, 15))

    def test_a_column_that_is_entirely_text_stays_text(self, tmp_path):
        """الحد المقابل: لا نحوّل عموداً نصّياً إلى أرقام بالقوة."""
        rows = [["الصنف", "الملاحظة"]]
        for i in range(1, 10):
            rows.append([f"صنف {i}", "بحاجة لمراجعة"])

        run = pipeline.process(build(tmp_path, rows), run_dir=tmp_path / "run")
        df = pl.read_parquet(run.processed_path)
        assert df["الملاحظة"].dtype == pl.Utf8
        assert df["الملاحظة"].null_count() == 0
