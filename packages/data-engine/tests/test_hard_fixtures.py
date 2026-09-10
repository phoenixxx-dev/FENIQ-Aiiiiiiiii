"""ملفات إكسل الفوضوية التي تفرضها الخطة §12.2.

كل ملف هنا يمثّل واقعاً شائعاً في ملفات المستخدمين العرب: شعار الشركة فوق
الجدول، عدة أوراق، ترميز CP1256، أرقام هندية، ملف تالف. أُنشئت بـ
`packages/data-engine/fixtures/hard/` ونُنتجها من مولّد مثبَّت البذرة.
"""
from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from phoenix import pipeline
from phoenix.ingestion import IngestionError

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "hard"


def run(name: str, tmp_path: Path):
    return pipeline.process(FIX / name, run_dir=tmp_path / "run")


class TestMultipleSheets:
    def test_picks_the_data_sheet_not_the_cover_page(self, tmp_path):
        """أول ورقة غلاف، والثانية فارغة، والبيانات في الثالثة."""
        r = run("multi_sheet.xlsx", tmp_path)
        df = pl.read_parquet(r.processed_path)
        assert df.height == 120, f"اختار الورقة الخطأ ({df.height} صف)"
        assert "اسم الصنف" in df.columns
        assert r.file_info.selected_sheet == "المبيعات"
        assert "فارغة" in r.file_info.sheet_names, "الأوراق الفارغة لا تُذكر أصلاً؟"


class TestHeaderNotInFirstRow:
    def test_finds_the_real_header_under_the_company_letterhead(self, tmp_path):
        """ثلاثة أسطر ترويسة ثم سطر فارغ ثم الرؤوس — أشيع شكل في الواقع."""
        r = run("headers_row_5.xlsx", tmp_path)
        df = pl.read_parquet(r.processed_path)
        assert df.height == 90
        assert set(["رقم الفاتورة", "الكمية", "سعر الوحدة"]) <= set(df.columns)
        assert not any(c.startswith("عمود_") for c in df.columns), (
            "أعمدة بلا أسماء ⇒ قُرئ صف الترويسة كرؤوس")


class TestLegacyEncoding:
    def test_cp1256_arabic_is_read_correctly_not_as_mojibake(self, tmp_path):
        """CSV عربي من إكسل غالباً CP1256؛ قراءته UTF-8 تعطي «Ø§Ù„»."""
        r = run("cp1256_encoded.csv", tmp_path)
        df = pl.read_parquet(r.processed_path)
        assert "اسم الصنف" in df.columns
        assert "Ø" not in "".join(df.columns), "ترميز مقروء خطأ"
        assert df["المنطقة"].drop_nulls().str.contains("Ø").sum() == 0


class TestArabicIndicDigits:
    def test_a_whole_column_of_arabic_digits_is_not_silently_dropped(self, tmp_path):
        """عيب حقيقي: العمود كان يُستنتج رقمياً، ثم يُفرَّغ كله بصمت
        (ignore_errors)، ثم يُحذف كعمود فارغ — تختفي الكمية بلا أي رسالة."""
        r = run("arabic_numerals.csv", tmp_path)
        df = pl.read_parquet(r.processed_path)

        assert "الكمية" in df.columns, "عمود الأرقام الهندية اختفى"
        assert df.height == 40, f"ضاعت صفوف: {df.height} من 40"
        assert df["الكمية"].null_count() == 0
        assert df["الكمية"].dtype in (pl.Int64, pl.Float64)
        assert df["الكمية"].max() <= 30 and df["الكمية"].min() >= 1

    def test_arabic_decimal_separator_and_currency_are_parsed(self, tmp_path):
        """«١٢٥٠٠٫٥٠ ر.س» ⇒ 12500.5 — الفاصلة العشرية العربية غير النقطة."""
        r = run("arabic_numerals.csv", tmp_path)
        df = pl.read_parquet(r.processed_path)
        assert df["سعر الوحدة"].dtype == pl.Float64
        assert abs(df["سعر الوحدة"].min() - 9000.5) < 0.01


class TestBrokenFilesFailKindly:
    @pytest.mark.parametrize("name,needle", [
        ("empty.csv", "فارغ"),
        ("corrupt.xlsx", "معطوب"),
    ])
    def test_error_is_arabic_and_actionable(self, name, needle, tmp_path):
        """رسالة مكتبة إنجليزية (BadZipFile) لا تفيد مستخدماً عربياً."""
        with pytest.raises(IngestionError) as e:
            run(name, tmp_path)
        assert needle in str(e.value)
        assert "zip" not in str(e.value).lower()
