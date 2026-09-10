"""حارس الـSQL تحت هجوم — أهم سطح أمني في المنتج.

الأداة `run_sql` تسمح لنموذج اللغة بتوليد استعلام. لو اخترق أحدٌ الحارس عبر
سؤال مصاغ بخبث (prompt injection)، قرأ ملفات الخادم أو بيانات مستخدم آخر.

كل حالة هنا جُرِّبت فعلاً على النسخة السابقة من الحارس؛ **خمس منها كانت
تمرّ**.
"""
from __future__ import annotations

import pytest

from phoenix.analytics import MAX_RESULT_ROWS, SQLGuardError, validate_sql

ALLOWED = {"dataset"}


class TestSourceFunctionsAreBlockedWholesale:
    """قائمة سماح لا قائمة منع: أي دالة في موقع الجدول مرفوضة.

    القائمة السوداء القديمة كانت تمنع read_csv وتمرّر parquet_scan
    وread_text وread_blob وsniff_csv — لأن اسماً واحداً لم يخطر ببالنا يكفي.
    """

    @pytest.mark.parametrize("sql", [
        "SELECT * FROM read_csv('/etc/passwd')",
        "SELECT * FROM read_csv_auto('/etc/passwd')",
        "SELECT * FROM parquet_scan('/etc/passwd')",
        "SELECT * FROM read_parquet('/etc/passwd')",
        "SELECT * FROM read_text('/etc/passwd')",
        "SELECT * FROM read_blob('/etc/passwd')",
        "SELECT * FROM sniff_csv('/etc/passwd')",
        "SELECT * FROM read_json('/etc/passwd')",
        "SELECT * FROM glob('/etc/*')",
        "SELECT * FROM duckdb_settings()",
    ])
    def test_reading_from_a_function_is_refused(self, sql):
        with pytest.raises(SQLGuardError):
            validate_sql(sql, ALLOWED)


class TestOtherPeoplesData:
    @pytest.mark.parametrize("sql", [
        "SELECT * FROM secrets",
        "SELECT * FROM dataset a JOIN secrets b ON 1=1",
        "SELECT (SELECT COUNT(*) FROM secrets) AS x FROM dataset",
        "WITH t AS (SELECT * FROM secrets) SELECT * FROM t",
        "SELECT * FROM dataset UNION SELECT * FROM secrets",
        "SELECT * FROM dataset WHERE x IN (SELECT y FROM secrets)",
    ])
    def test_any_route_to_an_unlisted_table_is_refused(self, sql):
        with pytest.raises(SQLGuardError):
            validate_sql(sql, ALLOWED)


class TestWriteOperations:
    @pytest.mark.parametrize("sql", [
        "SELECT * FROM dataset; DROP TABLE dataset",
        "DELETE FROM dataset",
        "INSERT INTO dataset VALUES (1)",
        "UPDATE dataset SET x = 1",
        "CREATE TABLE t AS SELECT * FROM dataset",
        "ATTACH '/tmp/evil.db' AS e",
        "COPY dataset TO '/tmp/out.csv'",
        "INSTALL httpfs",
        "PRAGMA database_list",
    ])
    def test_anything_that_is_not_a_read_is_refused(self, sql):
        with pytest.raises(SQLGuardError):
            validate_sql(sql, ALLOWED)


class TestRowCeilingIsAlwaysEnforced:
    def test_a_huge_user_limit_is_clamped_not_honoured(self):
        """عيب حقيقي: `LIMIT 99999999` كان يمرّ كما هو ويسحب كل شيء."""
        out = validate_sql("SELECT * FROM dataset LIMIT 99999999", ALLOWED)
        assert str(MAX_RESULT_ROWS) in out
        assert "99999999" not in out

    def test_a_small_limit_is_respected(self):
        out = validate_sql("SELECT * FROM dataset LIMIT 5", ALLOWED)
        assert "LIMIT 5" in out.upper()

    def test_a_query_without_limit_gets_one(self):
        out = validate_sql("SELECT * FROM dataset", ALLOWED)
        assert f"LIMIT {MAX_RESULT_ROWS}" in out.upper()

    def test_limit_all_is_replaced_by_the_ceiling(self):
        out = validate_sql("SELECT * FROM dataset LIMIT ALL", ALLOWED)
        assert str(MAX_RESULT_ROWS) in out


class TestLegitimateQueriesStillWork:
    """حارس صارم يرفض الاستعلامات السليمة يُعطَّل بعد أسبوع — فنثبّت أنه لا يفعل."""

    @pytest.mark.parametrize("sql", [
        "SELECT * FROM dataset",
        "select * from DATASET",                      # حالة الأحرف لا تهم في SQL
        "SELECT SUM(x) FROM dataset WHERE y > 3 GROUP BY z ORDER BY 1 DESC",
        "WITH t AS (SELECT * FROM dataset) SELECT COUNT(*) FROM t",
        "SELECT COUNT(*) FROM (SELECT * FROM dataset) s",
        'SELECT "اسم الصنف", SUM("الإجمالي") FROM dataset GROUP BY 1',
    ])
    def test_accepted(self, sql):
        assert validate_sql(sql, ALLOWED)
