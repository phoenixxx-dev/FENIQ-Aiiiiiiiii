"""معيار الأداء في الخطة: ملف 500 ألف صف يُعالَج دون تجاوز 2GB ذاكرة.

الملف يُولَّد عند التشغيل لا يُخزَّن في المستودع (30 ميغابايت لا مكان لها
في git). البذرة ثابتة فالنتيجة قابلة للتكرار.

التشغيل:  pytest -m slow packages/data-engine/tests/test_large_file.py
"""
from __future__ import annotations

import os
import random
import resource
import time
from pathlib import Path

import polars as pl
import pytest

from phoenix import pipeline

ROWS = 500_000
MEMORY_LIMIT_MB = 2048
pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def big_csv(tmp_path_factory) -> Path:
    random.seed(11)
    products = ["لابتوب ديل انسبايرون", "شاشة سامسونج 27", "طابعة انش بي",
                "كرسي مكتب دوّار", "هارد ديسك 1 تيرا", "كاميرا ويب"]
    regions = ["دمشق", "حلب", "حمص", "اللاذقية", "طرطوس"]
    reps = ["أحمد الحلبي", "سامر خوري", "ليلى النجار", "رنا العلي"]

    path = tmp_path_factory.mktemp("large") / "large_500k.csv"
    df = pl.DataFrame({
        "رقم الفاتورة": [f"INV-{i}" for i in range(ROWS)],
        "التاريخ": [f"2026-{random.randint(1, 8):02d}-{random.randint(1, 28):02d}"
                    for _ in range(ROWS)],
        "اسم الصنف": [random.choice(products) for _ in range(ROWS)],
        "المنطقة": [random.choice(regions) for _ in range(ROWS)],
        "المندوب": [random.choice(reps) for _ in range(ROWS)],
        "الكمية": [random.randint(1, 25) for _ in range(ROWS)],
        "سعر الوحدة": [random.choice([12500, 38000, 9000, 22000]) for _ in range(ROWS)],
    })
    df = df.with_columns((pl.col("الكمية") * pl.col("سعر الوحدة")).alias("الإجمالي"))
    df.write_csv(path)
    return path


def test_half_a_million_rows_within_the_memory_budget(big_csv, tmp_path):
    before_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    t0 = time.perf_counter()
    run = pipeline.process(big_csv, run_dir=tmp_path / "run")
    seconds = time.perf_counter() - t0
    peak_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024

    out = pl.read_parquet(run.processed_path)
    print(f"\n  {ROWS} صف · {seconds:.1f}s · ذروة الذاكرة {peak_mb:.0f}MB "
          f"(بدأت من {before_mb:.0f}MB) · المخرج {out.height} صف")

    assert out.height > ROWS * 0.9, "فقدنا أكثر من 10% من الصفوف"
    assert peak_mb < MEMORY_LIMIT_MB, (
        f"تجاوز الذاكرة: {peak_mb:.0f}MB > {MEMORY_LIMIT_MB}MB")
    # الملف المعالَج مضغوط عموديّاً — يجب أن يكون أصغر بوضوح من الأصل
    assert run.processed_path.stat().st_size < os.path.getsize(big_csv)


def test_analytics_on_the_large_file_answers_quickly(big_csv, tmp_path):
    """السؤال على نصف مليون صف يجب أن يبقى تحت 3 ثوانٍ (قاعدة ذهبية #6)."""
    from phoenix.ask import RuleRouter, ToolExecutor

    run = pipeline.process(big_csv, run_dir=tmp_path / "run2")
    engine = run.engine()
    try:
        for q in ["كم إجمالي المبيعات؟", "مين أكتر 5 منتجات مبيعاً؟",
                  "المبيعات حسب المنطقة"]:
            t0 = time.perf_counter()
            call = RuleRouter().route(q, run.schema)
            answer = ToolExecutor(engine, run.schema).execute(call, q)
            took = time.perf_counter() - t0
            print(f"  «{q}» → {took:.2f}s")
            assert answer.metrics, f"«{q}» بلا نتيجة"
            assert took < 3.0, f"«{q}» استغرق {took:.1f}s"
    finally:
        engine.close()
