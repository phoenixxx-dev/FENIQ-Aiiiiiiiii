"""مهلة استعلام DuckDB — كانت معلنة ولا تُفرَض.

حارس الـSQL يحدّ **عدد الصفوف الراجعة** لا **مقدار العمل**. استعلام ثقيل
على ملف كبير كان يستطيع الاستمرار دقائق ويحتجز خيطاً من الخادم.
"""
from __future__ import annotations

import time

import polars as pl
import pytest

from phoenix.analytics import (QUERY_MEMORY_LIMIT, AnalyticsEngine,
                               QueryTimeout)
from phoenix.models import SemanticColumn, SemanticSchema


@pytest.fixture(scope="module")
def parquet(tmp_path_factory):
    d = tmp_path_factory.mktemp("timeout")
    path = d / "data.parquet"
    pl.DataFrame({
        "المنتج": [f"صنف {i % 50}" for i in range(3000)],
        "الإجمالي": [float(i) for i in range(3000)],
    }).write_parquet(path)
    return path


def _schema() -> SemanticSchema:
    return SemanticSchema(columns=[
        SemanticColumn(column_name="المنتج", concept="product_name",
                       role="dimension", confidence=1.0),
        SemanticColumn(column_name="الإجمالي", concept="total_amount",
                       role="measure", confidence=1.0),
    ])


class TestTimeoutIsEnforced:
    def test_a_heavy_query_is_stopped_not_left_running(self, parquet):
        """ضرب ديكارتي ثلاثي على 3000 صف = 27 مليار صف — لن ينتهي أبداً."""
        engine = AnalyticsEngine(parquet, _schema(), timeout_seconds=1.0)
        try:
            started = time.perf_counter()
            with pytest.raises(QueryTimeout):
                engine.run(
                    "SELECT COUNT(*) FROM dataset a, dataset b, dataset c",
                    metric_name="اختبار", source_columns=[])
            took = time.perf_counter() - started
            assert took < 15, f"لم يُوقَف الاستعلام في الوقت: {took:.1f}s"
        finally:
            engine.close()

    def test_the_message_tells_the_user_what_to_do(self, parquet):
        engine = AnalyticsEngine(parquet, _schema(), timeout_seconds=1.0)
        try:
            with pytest.raises(QueryTimeout) as e:
                engine.run("SELECT COUNT(*) FROM dataset a, dataset b, dataset c",
                           metric_name="اختبار", source_columns=[])
            msg = str(e.value)
            assert "ثانية" in msg
            assert "جرّب" in msg, f"رسالة بلا مخرج: {msg}"
        finally:
            engine.close()

    def test_normal_queries_are_unaffected(self, parquet):
        """الحد المقابل: مهلة تقتل الاستعلامات العادية أسوأ من غيابها."""
        engine = AnalyticsEngine(parquet, _schema(), timeout_seconds=1.0)
        try:
            rows, ev = engine.run("SELECT COUNT(*) FROM dataset",
                                  metric_name="عدد", source_columns=[])
            assert rows[0][0] == 3000
            assert ev.duration_ms >= 0
        finally:
            engine.close()

    def test_the_connection_survives_a_timeout(self, parquet):
        """بعد الإيقاف يجب أن يبقى الاتصال صالحاً للأسئلة التالية."""
        engine = AnalyticsEngine(parquet, _schema(), timeout_seconds=1.0)
        try:
            with pytest.raises(QueryTimeout):
                engine.run("SELECT COUNT(*) FROM dataset a, dataset b, dataset c",
                           metric_name="اختبار", source_columns=[])
            rows, _ = engine.run("SELECT COUNT(*) FROM dataset",
                                 metric_name="عدد", source_columns=[])
            assert rows[0][0] == 3000
        finally:
            engine.close()


class TestMemoryLimit:
    def test_the_connection_declares_a_memory_ceiling(self, parquet):
        engine = AnalyticsEngine(parquet, _schema())
        try:
            value = engine.con.execute(
                "SELECT current_setting('memory_limit')").fetchone()[0]
            assert value, "لا سقف ذاكرة معلن"
        finally:
            engine.close()
