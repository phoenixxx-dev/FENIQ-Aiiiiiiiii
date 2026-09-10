"""إعداد بيئة الاختبار.

تحديد المحاولات مفعّل بالإنتاج، لكن حزمة الاختبار تسجّل عشرات المستخدمين خلال
ثوانٍ من نفس عنوان IP، فتصطدم بالحد وتفشل لسبب لا علاقة له بما تفحصه.

الحل: نرفع الحد هنا، **ولا نطفئ الميزة** — لها ملف اختبار مخصّص
(test_rate_limit.py) يخفض الحد ويثبت أنها تعمل فعلاً.
"""
from __future__ import annotations

import os

os.environ.setdefault("RATE_LIMIT_LOGIN_PER_MINUTE", "100000")
os.environ.setdefault("RATE_LIMIT_GENERAL_PER_MINUTE", "100000")
os.environ.setdefault("RATE_LIMIT_ASK_PER_MINUTE", "100000")

import pytest


def _external_arq_workers() -> list[str]:
    """عمّال ARQ خارجيون يعملون الآن على نفس الطابور.

    السبب (ق-27): الحزمة تُشغّل الوظائف بنفسها داخل الاختبار. عامل خارجي
    متروك من تشغيل يدوي (scripts_e2e.py مثلاً) يخطف الوظيفة من الطابور،
    فتفشل اختبارات لا علاقة لها بالسبب — وتُطارَد الأعراض ساعةً في الكود.
    كلّفني هذا ثلث تشغيلات فاشلة قبل أن أكتشف أن الخلل في `ps` لا في `git diff`.
    """
    found: list[str] = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) == os.getpid():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as fh:
                cmd = fh.read().replace(b"\x00", b" ").decode(errors="ignore")
        except (OSError, ProcessLookupError):
            continue
        if "arq" in cmd and "pytest" not in cmd:
            found.append(f"{entry}: {cmd.strip()[:80]}")
    return found


@pytest.fixture(scope="session", autouse=True)
def no_external_worker():
    workers = _external_arq_workers()
    if workers:
        pytest.exit(
            "عامل ARQ خارجي شغّال — سيخطف وظائف الاختبار وتفشل اختبارات "
            "بريئة. أوقفه أولاً:\n  " + "\n  ".join(workers) +
            "\n  kill " + " ".join(w.split(":")[0] for w in workers),
            returncode=3)
