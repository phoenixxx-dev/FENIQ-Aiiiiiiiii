"""رسائل الخطأ بعين المستخدم.

المستخدم لا يقرأ `error_code` — يقرأ `message_ar`. رسالة تُخبره بما حدث ولا
تقول ماذا يفعل تتركه عالقاً، ورسالة بمصطلحاتنا الداخلية («التخزين»،
«الوظيفة»، «presigned») تُشعره أن العطل أكبر مما هو.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
API_SRC = ROOT / "apps" / "api" / "src"
sys.path.insert(0, str(ROOT))

MESSAGE_RE = re.compile(r'"message_ar":\s*(?:f?")([^"]{4,})"')

# ألفاظ من عالمنا لا من عالم المستخدم
INTERNAL_WORDS = ["presigned", "S3", "bucket", "parquet", "endpoint", "token",
                  "التخزين", "الوظيفة", "الطابور", "الكاش"]


def _messages() -> list[tuple[str, int, str]]:
    out = []
    for py in API_SRC.rglob("*.py"):
        for i, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            for m in MESSAGE_RE.finditer(line):
                out.append((py.name, i, m.group(1)))
    return out


class TestMessagesSpeakTheUsersLanguage:
    def test_there_are_messages_to_check(self):
        assert len(_messages()) >= 15, "لم يُعثر على رسائل — الفحص بلا معنى"

    def test_no_internal_vocabulary_reaches_the_user(self):
        offenders = [f"{f}:{i}: {msg}"
                     for f, i, msg in _messages()
                     if any(w in msg for w in INTERNAL_WORDS)]
        assert offenders == [], offenders

    def test_every_message_is_a_real_sentence(self):
        """رسالة من كلمتين («غير موجود») لا تُفيد أحداً."""
        offenders = [f"{f}:{i}: {msg}"
                     for f, i, msg in _messages()
                     if len(msg.split()) < 3]
        assert offenders == [], offenders

    def test_recoverable_errors_tell_the_user_what_to_do(self):
        """الأخطاء التي للمستخدم فيها حيلة يجب أن تذكرها.

        القائمة صريحة عمداً: «كل رسالة يجب أن تحوي فعل أمر» قاعدة خاطئة —
        «البريد مستخدم مسبقاً» لا تحتاج إرشاداً. المهم أن الحالات التي
        يعلق فيها المستخدم فعلاً تُرشده.
        """
        actionable = ["جرّب", "أعد", "ارفع", "ابدأ", "سجّل", "اختر", "انتظر",
                      "حدّثها", "صدّره"]
        must_guide = ["لم يكتمل رفع الملف", "انتهت صلاحية الرفع",
                      "الملف قيد المعالجة", "تعذّر الوصول إلى بيانات هذا الملف",
                      "الجلسة منتهية. سجّل", "صار خطأ غير متوقَّع",
                      "الملف الأصلي غير متاح", "عملية تنظيف غير معروفة"]
        found = {msg for _f, _i, msg in _messages()}
        for needle in must_guide:
            matching = [m for m in found if m.startswith(needle)]
            assert matching, f"لم تعد الرسالة موجودة: {needle}"
            assert any(a in matching[0] for a in actionable), \
                f"رسالة بلا إرشاد: {matching[0]}"
