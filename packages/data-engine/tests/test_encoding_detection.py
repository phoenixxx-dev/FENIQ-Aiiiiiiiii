"""كشف الترميز — قرارٌ كان يُحسم بفارق أربعة بالألف (ق-50).

العيب: ملف CSV عربي بترميز UTF-8 سليم كان يُقرأ أحياناً على أنه CP1256، فيتحوّل
«الصنف» إلى «ط§ظ„طµظ†ظپ». وبعدها لا شيء يعمل: لا القاموس يطابق اسماً، ولا
المستخدم يقرأ جدوله، ولا رسالة خطأ واحدة تظهر. الملف «نجح».

السبب أن الاختيار كان مسابقة نِسَب: أيّ الترميزَين يُنتج نسبة حروف عربية أعلى.
والمكيدة أن CP1256 **تقرأ أي بايت بلا خطأ**، وقرابة نصف ما تُخرجه من فوضى يقع
في نطاق الحروف العربية نفسه. فقيس عملياً: 0.2730 لـUTF-8 مقابل 0.2768 لقراءتها
الخاطئة. خسر الصحيحُ بفارق أربعة بالألف.

الدرس: قرارٌ يُحسم بفارق كهذا ليس قراراً، بل قرعة. وحين توجد حقيقة بنيوية
قاطعة (بنية بايتات UTF-8 لا تصادفها نصوص CP1256) فالأصل أن تُستعمل قاعدةً لا
أن تُضاف نقطةً في مسابقة.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from phoenix import pipeline  # noqa: E402
from phoenix.ingestion import detect_encoding  # noqa: E402

ARABIC_ROWS = ["الصنف,الكمية,سعر الوحدة"] + [
    f"صنف {i},{i % 7 + 1},{100 + i}" for i in range(30)
]


class TestUtf8Wins:
    def test_a_plain_utf8_arabic_csv(self, tmp_path):
        p = tmp_path / "utf8.csv"
        p.write_text("\n".join(ARABIC_ROWS), encoding="utf-8")
        assert detect_encoding(p) == "utf-8"

    def test_a_bom_still_wins(self, tmp_path):
        p = tmp_path / "bom.csv"
        p.write_text("\n".join(ARABIC_ROWS), encoding="utf-8-sig")
        assert detect_encoding(p) == "utf-8-sig"

    def test_a_multibyte_char_cut_at_the_sample_edge(self, tmp_path):
        """نقرأ 64 كيلوبايت فقط. لو انقطع حرف عربي عند الحافة ورفضنا الملف
        لعاد العيب نفسه على الملفات الكبيرة وحدها — وهي الأهم."""
        p = tmp_path / "big.csv"
        p.write_text("صنف طويل جداً بالعربية," * 4000, encoding="utf-8")
        assert detect_encoding(p) == "utf-8"


class TestRealCp1256IsStillRecognised:
    """الحدّ المقابل: بلا هذا يصير الحلّ «كل شيء UTF-8»، وهو عطلٌ آخر —
    وCSV عربي خارج من Excel قديم هو CP1256 فعلاً في أحيان كثيرة."""

    def test_a_genuine_cp1256_file(self, tmp_path):
        p = tmp_path / "old.csv"
        p.write_bytes("\n".join(ARABIC_ROWS).encode("cp1256"))
        assert detect_encoding(p) == "cp1256"

    def test_a_genuine_cp1256_file_reads_correctly(self, tmp_path):
        p = tmp_path / "old.csv"
        p.write_bytes("\n".join(ARABIC_ROWS).encode("cp1256"))
        run = pipeline.process(p, run_dir=tmp_path / "run")
        assert "الصنف" in [c.name for c in run.profile.columns]


class TestTheNamesSurviveTheWholePipeline:
    """الفحص الحقيقي ليس على الدالة بل على ما يصل للمستخدم."""

    @pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "cp1256"])
    def test_arabic_headers_are_readable_after_processing(self, tmp_path, encoding):
        p = tmp_path / f"{encoding}.csv"
        text = "\n".join(ARABIC_ROWS)
        if encoding == "cp1256":
            p.write_bytes(text.encode("cp1256"))
        else:
            p.write_text(text, encoding=encoding)
        run = pipeline.process(p, run_dir=tmp_path / f"run_{encoding}")
        names = [c.name for c in run.profile.columns]
        assert "الصنف" in names, f"{encoding}: أسماء مشوّهة {names}"
        assert not any("ط§" in n or "ظ„" in n for n in names), names
        # والأثر الحقيقي: الأعمدة تُفهم بعد أن صارت مقروءة
        assert run.schema.by_concept("quantity") is not None, "الكمية لم تُفهم"

    def test_a_garbled_read_would_actually_fail_this_guard(self):
        """حارسٌ لا يفشل على العيب الذي وُجد لأجله ليس حارساً — نتأكد أن
        شكل الفوضى المعروف («ط§ظ„طµظ†ظپ») يسقط في الفحص أعلاه."""
        garbled = "ط§ظ„طµظ†ظپ"
        assert "ط§" in garbled or "ظ„" in garbled
        assert "الصنف" != garbled
