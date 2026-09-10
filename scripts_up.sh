#!/usr/bin/env bash
# إقلاع بيئة العمل من الصفر — تُشغَّل ببداية كل جلسة.
# السبب: العمليات (postgres/redis/api) بتموت بين الجلسات، بس البيانات بتضل عالقرص.
set -u

PGBIN=/usr/lib/postgresql/16/bin
PGDATA=/var/lib/pg
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "→ PostgreSQL"
if ! pg_isready -h 127.0.0.1 -p 5432 >/dev/null 2>&1; then
  if [ ! -d "$PGDATA" ]; then
    id postgres >/dev/null 2>&1 || useradd -m postgres
    mkdir -p "$PGDATA" && chown postgres:postgres "$PGDATA"
    su postgres -c "$PGBIN/initdb -D $PGDATA -U postgres --auth=trust -E UTF8" >/tmp/initdb.log 2>&1
  fi
  su postgres -c "$PGBIN/pg_ctl -D $PGDATA -l /tmp/pg.log -o '-p 5432' start" >/dev/null 2>&1
  sleep 3
fi
pg_isready -h 127.0.0.1 -p 5432 >/dev/null 2>&1 && echo "   ✓ شغّال" || { echo "   ✗ فشل — راجع /tmp/pg.log"; exit 1; }

# المستخدم وقاعدة البيانات (تُنشأ مرة واحدة فقط)
psql -h 127.0.0.1 -U postgres -tc "SELECT 1 FROM pg_roles WHERE rolname='phoenix'" 2>/dev/null | grep -q 1 \
  || psql -h 127.0.0.1 -U postgres -c "CREATE USER phoenix WITH PASSWORD 'phoenix' SUPERUSER;" >/dev/null 2>&1
psql -h 127.0.0.1 -U postgres -tc "SELECT 1 FROM pg_database WHERE datname='phoenix'" 2>/dev/null | grep -q 1 \
  || psql -h 127.0.0.1 -U postgres -c "CREATE DATABASE phoenix OWNER phoenix;" >/dev/null 2>&1

echo "→ Redis"
redis-cli ping >/dev/null 2>&1 || { redis-server --daemonize yes --port 6379 --save "" >/dev/null 2>&1; sleep 1; }
redis-cli ping >/dev/null 2>&1 && echo "   ✓ شغّال" || { echo "   ✗ فشل"; exit 1; }

export PYTHONPATH="$ROOT:$ROOT/packages/data-engine"
echo "→ PYTHONPATH جاهز"
echo
echo "للتشغيل:"
echo "  python3 -m uvicorn apps.api.src.main:app --port 8000 &"
echo "  python3 -m arq apps.api.src.workers.worker.WorkerSettings &"
echo "للاختبارات:"
echo "  python3 -m pytest apps/api/tests packages/data-engine/tests -q"
