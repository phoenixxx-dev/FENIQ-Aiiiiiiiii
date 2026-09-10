"""المحارف غير المرئية — عيب لا يراه أحد بالعين ويكسر كل تجميع.

ملفات إكسل العربية الحقيقية مليئة بها: مسافة غير فاصلة (NBSP) تأتي من
النسخ من الويب، وعلامات اتجاه (RLM/LRM) تضعها بعض البرامج تلقائياً، ومسافة
صفرية العرض (ZWSP) تأتي من أنظمة قديمة.

الخطر: «شركة النور» و«شركة‏النور» تبدوان **متطابقتين على الشاشة**
وتُحسبان كيانين. المستخدم يرى مجموعين لعميل واحد ولا يفهم لماذا، ولا توجد
رسالة خطأ ولا مؤشّر — أخطر أشكال الضياع الصامت.
"""
from __future__ import annotations

from pathlib import Path

import polars as pl

from phoenix import pipeline
from phoenix.arabic_text import normalize_ar


def write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


class TestNormalizationRemovesInvisibles:
    def test_nbsp_is_treated_as_an_ordinary_space(self):
        assert normalize_ar("شركة النور") == normalize_ar("شركة النور")

    def test_direction_marks_do_not_create_a_different_name(self):
        for mark in ("‎", "‏", "؜"):
            assert normalize_ar(f"شركة{mark} النور") == normalize_ar("شركة النور"), mark

    def test_zero_width_characters_do_not_split_a_name(self):
        for mark in ("​", "‌", "‍", "﻿"):
            assert normalize_ar(f"شركة{mark}النور") == normalize_ar("شركةالنور"), mark


class TestPipelineMergesNamesThatLookIdentical:
    def test_one_customer_not_two(self, tmp_path):
        """الاختبار الحقيقي: ملف فيه الاسم نفسه بمحرف خفي في بعض صفوفه."""
        rows = ["العميل,الكمية,سعر الوحدة"]
        for i in range(12):
            # كل الصفوف تحمل المحرف الخفي: لا يمكن لتوحيد الأسماء أن
            # "ينقذ" النتيجة باختيار نسخة نظيفة — التنظيف وحده يفعل
            name = "شركة​النور"
            rows.append(f"{name},{i + 1},{100 + i}")
        run = pipeline.process(write(tmp_path, "invisible.csv", "\n".join(rows)),
                               run_dir=tmp_path / "run")
        df = pl.read_parquet(run.processed_path)
        names = df["العميل"].unique().to_list()
        assert len(names) == 1, (
            f"اسم واحد بالعين صار كيانين في البيانات: {names!r}")
        # ولا يكفي الدمج: المحرف الخفي يجب ألا يبقى في القيمة المحفوظة.
        # لو بقي، عاد نفس العيب عند أول ملف ثانٍ يُقارَن به، وعند البحث،
        # وعند التصدير إلى نظام آخر.
        from phoenix.arabic_text import _INVISIBLE_CHARS
        leftover = [c for c in _INVISIBLE_CHARS if c in names[0]]
        assert leftover == [], (
            f"محرف خفي بقي في القيمة المحفوظة: {names[0]!r}")
