#!/usr/bin/env python3
"""قياس زمن نقاط النهاية على ملف كبير — القاعدة الذهبية #6 (لا endpoint فوق 3 ثوانٍ).

يشغّل خادماً وعاملاً حقيقيين، يرفع ملفاً مولَّداً، ثم يقيس كل مسار يستعمله
المستخدم. **يقتل عملياته دائماً** في النهاية: عاملٌ شارد بقي يعمل بعد قياس
سابق التقط وظائف الاختبارات فأربكها ساعةً كاملة — الدرس صار جزءاً من الأداة.

الاستخدام:  python3 scripts_bench.py [عدد_الصفوف]
"""
from __future__ import annotations

import os
import random
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import polars as pl

ROOT = Path(__file__).resolve().parent
API_PORT = 8040
BASE = f"http://127.0.0.1:{API_PORT}"
SLOW = 3.0          # حد القاعدة الذهبية #6


def make_file(rows: int, path: Path) -> Path:
    if path.exists():
        return path
    random.seed(5)
    prods = ["لابتوب ديل انسبايرون", "شاشة سامسونج 27", "طابعة انش بي", "كرسي دوّار"]
    regs = ["دمشق", "حلب", "حمص", "اللاذقية"]
    df = pl.DataFrame({
        "رقم الفاتورة": [f"INV-{i}" for i in range(rows)],
        "التاريخ": [f"2026-{random.randint(1, 8):02d}-{random.randint(1, 28):02d}"
                    for _ in range(rows)],
        "اسم الصنف": [random.choice(prods) for _ in range(rows)],
        "المنطقة": [random.choice(regs) for _ in range(rows)],
        "الكمية": [random.randint(1, 25) for _ in range(rows)],
        "سعر الوحدة": [random.choice([12500, 38000, 9000]) for _ in range(rows)],
    })
    path.parent.mkdir(parents=True, exist_ok=True)
    df.with_columns((pl.col("الكمية") * pl.col("سعر الوحدة")).alias("الإجمالي")).write_csv(path)
    return path


def main() -> int:
    rows = int(sys.argv[1]) if len(sys.argv) > 1 else 300_000
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", API_PORT)) == 0:
            print(f"✗ المنفذ {API_PORT} مشغول")
            return 1

    src = make_file(rows, ROOT / "artifacts" / f"bench_{rows}.csv")
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{ROOT}:{ROOT / 'packages' / 'data-engine'}"
    env["RATE_LIMIT_LOGIN_PER_MINUTE"] = "10000"

    procs: list[subprocess.Popen] = []
    try:
        procs.append(subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "apps.api.src.main:app", "--host", "127.0.0.1",
             "--port", str(API_PORT), "--log-level", "warning"],
            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
            start_new_session=True))
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
            print("✗ الـAPI لم يستجب")
            return 1

        with httpx.Client(base_url=BASE, timeout=900) as c:
            email = f"bench_{uuid.uuid4().hex[:8]}@example.com"
            r = c.post("/auth/register", json={"email": email, "password": "StrongPass123"})
            h = {"Authorization": f"Bearer {r.json()['access_token']}"}

            with src.open("rb") as fh:
                r = c.post("/datasets", headers=h, files={"file": (src.name, fh)})
            ds, job = r.json()["dataset_id"], r.json()["job_id"]

            t0 = time.perf_counter()
            while time.perf_counter() - t0 < 600:
                st = c.get(f"/jobs/{job}", headers=h).json()
                if st["status"] in ("succeeded", "failed"):
                    break
                time.sleep(2)
            print(f"\n  المعالجة: {st['status']} · {time.perf_counter() - t0:.1f}s · {rows} صف\n")
            if st["status"] != "succeeded":
                print("  ✗", st.get("error_ar"))
                return 1

            checks = [
                ("اللوحة", lambda: c.get(f"/datasets/{ds}/dashboard", headers=h)),
                ("صفحة صفوف", lambda: c.get(f"/datasets/{ds}/rows?page=100", headers=h)),
                ("بحث عربي", lambda: c.get(f"/datasets/{ds}/rows?search=دمشق", headers=h)),
                ("فرز", lambda: c.get(
                    f"/datasets/{ds}/rows?sort_by=الإجمالي&descending=true", headers=h)),
                ("سؤال", lambda: c.post(f"/datasets/{ds}/ask", headers=h,
                                        json={"question": "كم إجمالي المبيعات؟"})),
                ("بطاقات الأعمدة", lambda: c.get(f"/datasets/{ds}/profile", headers=h)),
                ("سجل التنظيف", lambda: c.get(f"/datasets/{ds}/cleaning", headers=h)),
                ("تصدير CSV", lambda: c.get(f"/datasets/{ds}/export?fmt=csv", headers=h)),
                ("تصدير XLSX", lambda: c.get(f"/datasets/{ds}/export?fmt=xlsx", headers=h)),
            ]
            slow = []
            for label, call in checks:
                t = time.perf_counter()
                resp = call()
                took = time.perf_counter() - t
                mb = len(resp.content) / 1e6
                mark = ""
                if took > SLOW and resp.status_code < 400:
                    mark = "⚠ فوق 3 ثوانٍ"
                    slow.append(label)
                print(f"  {label:<16} {resp.status_code}  {took:6.2f}s  {mb:6.1f}MB  {mark}")

        print()
        if slow:
            print(f"✗ تجاوزت الحد: {'، '.join(slow)}")
            return 1
        print("✓ كل المسارات تحت 3 ثوانٍ على هذا الحجم.")
        return 0
    finally:
        # لا مجال للنسيان: الإنهاء في finally، وبالمجموعة كاملة
        for p in procs:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            except Exception:                                     # noqa: BLE001
                pass


if __name__ == "__main__":
    raise SystemExit(main())
