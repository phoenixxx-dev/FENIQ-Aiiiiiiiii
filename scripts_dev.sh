#!/usr/bin/env bash
# تشغيل محلي بلا Docker (كما جُرّب فعلياً في هذه البيئة)
set -e
export PYTHONPATH="$(pwd):$(pwd)/packages/data-engine"
python3 -m uvicorn apps.api.src.main:app --host 127.0.0.1 --port 8000 &
python3 -m arq apps.api.src.workers.worker.WorkerSettings &
wait
