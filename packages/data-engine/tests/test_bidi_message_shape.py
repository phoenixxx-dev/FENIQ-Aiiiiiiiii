"""شكل الرسالة العربية المختلطة — النقطة تهرب إلى الجهة الخطأ.

عيب حقيقي ظهر على الشاشة: «صدّر البيانات إلى Excel أو CSV.» تُعرض
«صدّر البيانات إلى Excel أو .CSV» — النقطة قفزت أمام الكلمة اللاتينية.

السبب bidi: الجملة عربية (RTL)، والكلمة اللاتينية مقطع LTR، والنقطة محرف
محايد في آخر النص فيلتحق باتجاه الفقرة ويظهر يسار المقطع اللاتيني.

العلاج المختار: **لا تُنهِ جملة عربية بكلمة لاتينية**. أرخص من حقن محارف
اتجاه غير مرئية (وهي التي نحذفها أصلاً من بيانات المستخدم، ق-40)، وأمتن
من الاعتماد على تصرّف المتصفح.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SOURCES = [ROOT / "packages" / "data-engine" / "phoenix", ROOT / "apps" / "api" / "src"]

ARABIC = re.compile(r"[؀-ۿ]")
# نص عربي ينتهي بكلمة لاتينية ثم نقطة
ENDS_LATIN_THEN_DOT = re.compile(r"[A-Za-z][A-Za-z0-9]*[)\]»\"']?\s*\.\s*$")


def _docstrings(tree: ast.AST) -> set[int]:
    """أسطر التوثيق — شرحٌ للمطوّر لا نصّ يُعرض للمستخدم."""
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                out.add(body[0].value.lineno)
    return out


def offenders() -> list[str]:
    bad: list[str] = []
    for root in SOURCES:
        for py in root.rglob("*.py"):
            if "tests" in py.parts:
                continue
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
            skip = _docstrings(tree)
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                    continue
                if node.lineno in skip:
                    continue
                text = node.value.strip()
                if not ARABIC.search(text) or len(text) < 12:
                    continue
                if ENDS_LATIN_THEN_DOT.search(text):
                    bad.append(f"{py.name}:{node.lineno}: …{text[-45:]}")
    return bad


class TestArabicMessagesDoNotEndWithALatinWord:
    def test_no_message_ends_with_a_latin_token_before_the_period(self):
        assert offenders() == [], (
            "جملة عربية تنتهي بكلمة لاتينية ثم نقطة — النقطة ستُعرض في الجهة "
            "الخطأ. أعِد الصياغة لتنتهي بكلمة عربية:\n  " + "\n  ".join(offenders()))

    def test_the_pattern_itself_matches_the_real_bug(self):
        """حارسٌ لا يُمسك الحالة التي وُجد لأجلها ليس حارساً."""
        assert ENDS_LATIN_THEN_DOT.search("صدّر البيانات إلى Excel أو CSV.")
        assert ENDS_LATIN_THEN_DOT.search("يرجى الحفظ بصيغة XLSX.")

    def test_the_pattern_does_not_flag_correct_arabic(self):
        assert not ENDS_LATIN_THEN_DOT.search("صدّر البيانات بصيغة CSV ثم أعد الرفع.")
        assert not ENDS_LATIN_THEN_DOT.search("الملف بصيغة PDF غير مدعوم.")
