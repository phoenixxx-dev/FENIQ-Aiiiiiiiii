#!/usr/bin/env python3
"""قياس Lighthouse على الواجهة — الخطة تشترط Performance > 85 وAccessibility > 90.

يبني الواجهة ويشغّلها ثم يقيس. الصفحة المقاسة هي شاشة الدخول (أول ما يراه
المستخدم) — الشاشات خلف تسجيل الدخول تحتاج جلسة، ويغطّيها فحص axe داخل
scripts_e2e.py.

الاستخدام:  python3 scripts_lighthouse.py
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "apps" / "web"
PORT = 3030
THRESHOLDS = {"performance": 85, "accessibility": 90,
              "best-practices": 85, "seo": 80}


def free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def main() -> int:
    if not free(PORT):
        print(f"✗ المنفذ {PORT} مشغول")
        return 1

    env = os.environ.copy()
    env["NEXT_PUBLIC_API_BASE"] = "http://127.0.0.1:8010"
    env["PORT"] = str(PORT)

    print("→ بناء الواجهة")
    build = subprocess.run(["npx", "next", "build"], cwd=WEB, env=env,
                           capture_output=True, text=True)
    if build.returncode != 0:
        print(build.stdout[-2000:])
        return 1

    proc = subprocess.Popen(["npx", "next", "start", "-p", str(PORT)], cwd=WEB, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
                            start_new_session=True)
    try:
        for _ in range(60):
            if not free(PORT):
                break
            time.sleep(1)
        else:
            print("✗ الواجهة لم تستجب")
            return 1

        print("→ قياس Lighthouse (موبايل)")
        out = ROOT / "artifacts" / "lighthouse.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(
            ["npx", "lighthouse", f"http://127.0.0.1:{PORT}/",
             "--quiet", "--output=json", f"--output-path={out}",
             "--only-categories=performance,accessibility,best-practices,seo",
             "--chrome-flags=--headless=new --no-sandbox --disable-dev-shm-usage"],
            cwd=WEB, env={**env, "CHROME_PATH": "/opt/pw-browsers/chromium"},
            capture_output=True, text=True, timeout=300)
        if not out.exists():
            print(r.stdout[-1500:] or r.stderr[-1500:])
            return 1

        data = json.loads(out.read_text())
        scores = {k: round((v.get("score") or 0) * 100)
                  for k, v in data["categories"].items()}
        print()
        failed = []
        for key, threshold in THRESHOLDS.items():
            got = scores.get(key, 0)
            mark = "✓" if got >= threshold else "✗"
            print(f"  {mark} {key:<15} {got:>3} (الحد {threshold})")
            if got < threshold:
                failed.append(key)

        if failed:
            print("\n  أهم الفرص:")
            audits = data["audits"]
            for aid, a in sorted(audits.items(),
                                 key=lambda kv: -(kv[1].get("details", {})
                                                  .get("overallSavingsMs", 0) or 0))[:5]:
                saving = a.get("details", {}).get("overallSavingsMs")
                if saving:
                    print(f"   • {a.get('title')}: {round(saving)}ms")
            return 1

        print("\n✓ كل المقاييس فوق حدود الخطة.")
        return 0
    finally:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:                                         # noqa: BLE001
            pass


if __name__ == "__main__":
    raise SystemExit(main())
