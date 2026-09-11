#!/bin/sh
# تشغيل Render المجاني: هجرات ← عامل المعالجة ← الـAPI، بنفس الحاوية.
# سكربت لا سطر أوامر داخل render.yaml: Render لا يمرّر dockerCommand عبر shell،
# فعلامات الاقتباس و& تصل حرفياً (عيب حقيقي: exit 127 بأول نشر).
set -e
python -m apps.api.src.db.migrate
arq apps.api.src.workers.worker.WorkerSettings &
exec uvicorn apps.api.src.main:app --host 0.0.0.0 --port "${PORT:-8000}"
