# فينيق 🐦 — Phoenix AI

يقرأ ملفات إكسل وCSV العربية الفوضوية، يفهم معاني أعمدتها، ينظّفها، ويجيب
عن أسئلة بالعربية — **وكل رقم يحمل دليله**.

## التشغيل

```bash
# 1) الخدمات (PostgreSQL 16 + Redis 7)
./scripts_up.sh

# 2) المتغيرات
cp .env.example .env        # وعدّل ما يلزم

# 3) قاعدة البيانات
export PYTHONPATH="$PWD:$PWD/packages/data-engine"
python3 -m alembic upgrade head

# 4) الخادم والعامل
python3 -m uvicorn apps.api.src.main:app --port 8000 &
python3 -m arq apps.api.src.workers.worker.WorkerSettings &

# 5) الواجهة
cd apps/web && npm install && npm run dev
```

الواجهة على `localhost:3000` والـAPI على `localhost:8000/docs`.

## التحقق

```bash
python3 -m pytest apps/api/tests packages/data-engine/tests -q   # حزمة الخلفية
cd apps/web && npx vitest run                                    # حزمة الواجهة
python3 packages/data-engine/evals/run_eval.py                   # 30 سؤالاً
python3 scripts_e2e.py                                           # متصفح حقيقي
python3 scripts_load.py 10                                       # حِمل متزامن
python3 scripts_lighthouse.py                                    # Lighthouse
python3 -m pytest -m slow -s                                     # 500 ألف صف
```

الأعداد الحالية (اختبارات، تقييم، قياسات) في `docs/STATUS.md` وحده: رقمٌ
مكرَّر في مكانين يشيخ في أحدهما — كان هنا «396 اختباراً» بينما الحزمة تجاوزت
الخمسمئة.

## استعمال المحرك وحده

المحرك مكتبة نقية — يعمل بلا أي خدمة:

```python
from phoenix import pipeline
run = pipeline.process("ملفك.xlsx")
print(run.schema.columns, run.insights)
```

## الوثائق

| الملف | المحتوى |
|---|---|
| `docs/RULES.md` | القواعد الذهبية السبع وحارس كل واحدة |
| `docs/ARCHITECTURE.md` | الطبقات، مصدر كل شيء، مسار الملف |
| `docs/STATUS.md` | حالة كل بند بصدق + الحدود المعروفة |
| `docs/DECISIONS.md` | القرارات المعمارية وأسبابها والقياسات وراءها |

## قبل أي تعديل

اقرأ `docs/RULES.md`. القواعد ليست توصيات — لكل واحدة اختبار يفشل عند خرقها.
