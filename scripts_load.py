#!/usr/bin/env python3
"""اختبار حِمل: 10 مستخدمين متزامنين — معيار القبول في الخطة (المرحلة 12).

يشغّل الـAPI + عاملين حقيقيين، ثم يُجري المسار الكامل لعشرة مستخدمين في
وقت واحد: تسجيل ← رفع ← معالجة ← لوحة ← أسئلة. المقياس ليس «هل يعمل» بل:
كم فشل؟ وكم استغرق الأبطأ؟

الاستخدام:  python3 scripts_load.py [عدد_المستخدمين]
"""
from __future__ import annotations

import os
import signal
import socket
import statistics
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent
API_PORT = 8020
BASE = f"http://127.0.0.1:{API_PORT}"
FIXTURE = ROOT / "packages" / "data-engine" / "fixtures" / "sales_ar_messy.xlsx"

QUESTIONS = ["كم إجمالي المبيعات؟", "مين أكتر 5 منتجات مبيعاً؟",
             "المبيعات حسب المنطقة", "شو الأصناف الراكدة؟"]

timings: dict[str, list[float]] = {}
failures: list[str] = []


def record(name: str, seconds: float) -> None:
    timings.setdefault(name, []).append(seconds)


def timed(name: str, fn):
    t0 = time.perf_counter()
    try:
        return fn()
    finally:
        record(name, time.perf_counter() - t0)


def one_user(index: int) -> None:
    """مسار مستخدم واحد كاملاً. أي استثناء يُسجَّل ولا يُسقط البقية."""
    try:
        with httpx.Client(base_url=BASE, timeout=120.0) as c:
            email = f"load_{index}_{uuid.uuid4().hex[:8]}@example.com"
            r = timed("register", lambda: c.post(
                "/auth/register", json={"email": email, "password": "StrongPass123"}))
            r.raise_for_status()
            h = {"Authorization": f"Bearer {r.json()['access_token']}"}

            with FIXTURE.open("rb") as fh:
                r = timed("upload", lambda: c.post(
                    "/datasets", headers=h, files={"file": (FIXTURE.name, fh)}))
            r.raise_for_status()
            ds_id, job_id = r.json()["dataset_id"], r.json()["job_id"]

            t0 = time.perf_counter()
            deadline = t0 + 180
            while time.perf_counter() < deadline:
                j = c.get(f"/jobs/{job_id}", headers=h).json()
                if j["status"] == "succeeded":
                    break
                if j["status"] == "failed":
                    failures.append(f"user{index}: المعالجة فشلت — {j.get('error_ar')}")
                    return
                time.sleep(1.0)
            else:
                failures.append(f"user{index}: المعالجة تجاوزت 180 ثانية")
                return
            record("processing", time.perf_counter() - t0)

            r = timed("dashboard", lambda: c.get(f"/datasets/{ds_id}/dashboard", headers=h))
            r.raise_for_status()
            if not r.json()["kpis"]:
                failures.append(f"user{index}: لوحة بلا مؤشرات")

            for q in QUESTIONS:
                r = timed("ask", lambda q=q: c.post(
                    f"/datasets/{ds_id}/ask", headers=h, json={"question": q}))
                r.raise_for_status()

            r = timed("rows", lambda: c.get(
                f"/datasets/{ds_id}/rows?page=2&page_size=50", headers=h))
            r.raise_for_status()
    except Exception as e:                                        # noqa: BLE001
        failures.append(f"user{index}: {type(e).__name__}: {e}")


def report() -> None:
    print("\n  العملية        عدد     وسيط      أبطأ")
    print("  " + "-" * 40)
    for name in ("register", "upload", "processing", "dashboard", "ask", "rows"):
        xs = timings.get(name)
        if not xs:
            continue
        print(f"  {name:<13} {len(xs):>4}  {statistics.median(xs):>7.2f}s  {max(xs):>7.2f}s")


def main() -> int:
    users = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", API_PORT)) == 0:
            print(f"✗ المنفذ {API_PORT} مشغول — أوقف العملية القديمة أولاً.")
            return 1

    env = os.environ.copy()
    env["PYTHONPATH"] = f"{ROOT}:{ROOT / 'packages' / 'data-engine'}"
    # الحد الافتراضي يمنع 10 تسجيلات متزامنة من نفس الـIP — نرفعه هنا لأن
    # المُختبَر هو السعة لا حماية إساءة الاستخدام (لها اختبارها المستقل).
    env["RATE_LIMIT_LOGIN_PER_MINUTE"] = "10000"
    env["RATE_LIMIT_GENERAL_PER_MINUTE"] = "10000"

    procs = []
    try:
        print(f"→ تشغيل الـAPI وعاملين ({users} مستخدماً متزامناً)")
        procs.append(subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "apps.api.src.main:app", "--host", "127.0.0.1",
             "--port", str(API_PORT), "--log-level", "warning"],
            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
            start_new_session=True))
        for _ in range(2):
            procs.append(subprocess.Popen(
                [sys.executable, "-m", "arq", "apps.api.src.workers.worker.WorkerSettings"],
                cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
                start_new_session=True))

        for _ in range(60):
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", API_PORT)) == 0:
                    break
            time.sleep(1)
        else:
            print("   ✗ الـAPI لم يستجب")
            return 1
        print("   ✓ جاهز")

        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=users) as pool:
            list(pool.map(one_user, range(users)))
        total = time.perf_counter() - started

        report()
        print(f"\n  الزمن الكلي: {total:.1f}s لـ{users} مستخدماً متوازياً")

        if failures:
            print(f"\n✗ {len(failures)} فشل:")
            for f in failures[:10]:
                print("   •", f)
            return 1

        # القاعدة الذهبية #6 (لا endpoint فوق 3 ثوانٍ) تُطبَّق بصرامة عند
        # الحمل المستهدف في الخطة (10 متزامنين). فوق ذلك ما نقيسه هو سلوك
        # التحميل الزائد على نواتين — الانتظار في الطابور متوقَّع، والانهيار
        # لا. لذلك نُبلغ ولا نفشل.
        slow_ask = [x for x in timings.get("ask", []) if x > 3.0]
        target = 10
        if slow_ask and users <= target:
            print(f"\n✗ {len(slow_ask)} سؤال تجاوز 3 ثوانٍ عند الحمل المستهدف")
            return 1
        if slow_ask:
            print(f"\n⚠ {len(slow_ask)} سؤال تجاوز 3 ثوانٍ عند {users} متزامناً "
                  f"(فوق الحمل المستهدف {target} على نواتين) — انتظار طابور لا انهيار")

        print("\n✓ لا انهيار ولا فشل.")
        return 0
    finally:
        for p in procs:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            except Exception:                                     # noqa: BLE001
                pass


if __name__ == "__main__":
    raise SystemExit(main())
