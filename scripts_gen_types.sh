#!/usr/bin/env bash
# توليد أنواع TypeScript من OpenAPI — مصدر الحقيقة الوحيد (الخطة §13.1).
# لا يحتاج خادماً شغّالاً: نستخرج المخطط من التطبيق مباشرة.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="$ROOT:$ROOT/packages/data-engine"

OUT_JSON="$(mktemp -t openapi-XXXX.json)"
python3 -c "
import json, sys
from apps.api.src.main import app
json.dump(app.openapi(), open(sys.argv[1], 'w'), ensure_ascii=False)
" "$OUT_JSON"

cd "$ROOT/apps/web"
npx --no-install openapi-typescript "$OUT_JSON" -o src/lib/generated-types.ts
rm -f "$OUT_JSON"
echo "✓ src/lib/generated-types.ts محدَّث من OpenAPI"
