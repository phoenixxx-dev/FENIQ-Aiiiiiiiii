"""B5 — محرك التحليل. كل رقم يخرج من هنا يحمل دليله (قاعدة ذهبية #1).

DuckDB يستعلم Parquet مباشرة. لا LLM يلمس الحساب.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import duckdb
import sqlglot
from sqlglot import exp

from .arabic_text import fmt_currency, fmt_number, fmt_percent
from .currency import detect_in_text, symbol_of
from .models import Evidence, MetricResult, SemanticSchema

QUERY_TIMEOUT_SECONDS = 30
# سقف ذاكرة الاستعلام. DuckDB يسكب على القرص عند بلوغه بدل أن يلتهم الجهاز.
QUERY_MEMORY_LIMIT = "1GB"
MAX_RESULT_ROWS = 10_000

FORBIDDEN = (exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create,
             exp.Alter, exp.Command, exp.Copy)


class SQLGuardError(Exception):
    """رُفض الاستعلام قبل تنفيذه."""


class QueryTimeout(Exception):
    """تجاوز الاستعلام مهلته وأُوقف."""


def validate_sql(sql: str, allowed_tables: set[str]) -> str:
    """الحارس: SELECT فقط، جداول مسموحة فقط، بلا دوال مصادر، وبسقف صفوف مفروض.

    مبدأ التصميم: **قائمة سماح لا قائمة منع**. القائمة السوداء تُهزم بأول
    اسم دالة لم نفكّر فيه — وقد حدث فعلاً: `read_csv` كان ممنوعاً بينما
    `parquet_scan` و`read_text` و`read_blob` و`sniff_csv` تمرّ. لذلك صار
    الشرط: كل ما يقع موقع الجدول في FROM/JOIN يجب أن يكون **اسم جدول
    مسموح**، وأي دالة هناك مرفوضة مهما كان اسمها.
    """
    try:
        tree = sqlglot.parse_one(sql, dialect="duckdb")
    except Exception as e:
        raise SQLGuardError(f"استعلام غير صالح: {e}") from e

    if tree is None or not isinstance(tree, (exp.Select, exp.Subquery)):
        raise SQLGuardError("يُسمح باستعلامات SELECT فقط.")

    for node_type in FORBIDDEN:
        if list(tree.find_all(node_type)):
            raise SQLGuardError("العملية غير مسموحة — القراءة فقط.")

    # أسماء الجداول في SQL غير حسّاسة لحالة الأحرف: «DATASET» و«dataset»
    # نفس الجدول. المقارنة الحسّاسة كانت ترفض استعلاماً سليماً.
    allowed_lower = {t.lower() for t in allowed_tables}
    # أسماء CTE مصادر شرعية داخل الاستعلام نفسه، لا جداول خارجية
    cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}

    for tbl in tree.find_all(exp.Table):
        # دالة في موقع الجدول (read_csv، parquet_scan، read_text، sniff_csv...)
        # يلفّها sqlglot في Table اسمها فارغ. الفحص القديم كان يتخطّى الاسم
        # الفارغ فتمرّ — ومهما طالت قائمة الأسماء الممنوعة يبقى اسم لم نعرفه.
        # لذلك: كل مصدر يجب أن يكون **معرّفاً** لا استدعاء دالة.
        if not isinstance(tbl.this, exp.Identifier):
            raise SQLGuardError("الاستعلام يقرأ من مصدر غير مسموح — مرفوض.")
        name = (tbl.name or "").strip('"').lower()
        if not name or (name not in allowed_lower and name not in cte_names):
            raise SQLGuardError(f"لا صلاحية للوصول إلى «{tbl.name}».")

    lowered = sql.lower()
    for banned in ("install", "load ", "attach", "copy ", "pragma", "system",
                   "duckdb_", "sqlite_", "postgres_"):
        if banned in lowered:
            raise SQLGuardError("الاستعلام يحاول الوصول إلى موارد خارجية — مرفوض.")

    if not isinstance(tree, exp.Subquery):
        # سقف الصفوف مفروض دائماً: استعلام يحمل LIMIT ضخماً كان يمرّ كما هو،
        # فيسحب ملايين الصفوف إلى الذاكرة وإلى الرد.
        current = tree.args.get("limit")
        keep = None
        if current is not None:
            try:
                keep = int(current.expression.this)
            except (AttributeError, TypeError, ValueError):
                keep = None            # LIMIT ALL أو تعبير غير رقمي
        if keep is None or keep > MAX_RESULT_ROWS:
            tree.set("limit", None)
            tree = tree.limit(MAX_RESULT_ROWS)
    return tree.sql(dialect="duckdb")


class AnalyticsEngine:
    """غلاف DuckDB على ملف Parquet واحد. اتصال للقراءة فقط."""

    TABLE = "dataset"

    def __init__(self, parquet_path: str | Path, schema: SemanticSchema,
                 timeout_seconds: float = QUERY_TIMEOUT_SECONDS):
        self.path = str(parquet_path)
        self.schema = schema
        self.timeout_seconds = timeout_seconds
        self.con = duckdb.connect(":memory:")
        # الترتيب مقصود: نُحضر البيانات إلى جدول في الذاكرة أولاً، ثم نقفل الوصول
        # لنظام الملفات نهائياً. بعد هذه اللحظة لا يستطيع أي استعلام — مهما كان
        # مصدره — قراءة ملف على القرص أو الوصول لبيانات مستخدم آخر.
        self.con.execute(
            f"CREATE TABLE {self.TABLE} AS SELECT * FROM read_parquet('{self.path}')"
        )
        self.con.execute(f"SET memory_limit='{QUERY_MEMORY_LIMIT}'")
        self.con.execute("SET enable_external_access=false")
        self.con.execute("SET lock_configuration=true")   # يمنع إعادة تفعيله لاحقاً
        self.row_total = self.con.execute(f"SELECT count(*) FROM {self.TABLE}").fetchone()[0]

    # ------------------------------------------------------------ التنفيذ
    def run(self, sql: str, metric_name: str, source_columns: list[str],
            filters: list[dict] | None = None) -> tuple[list[tuple], Evidence]:
        safe = validate_sql(sql, {self.TABLE})
        t0 = time.perf_counter()
        rows = self._execute(safe).fetchall()
        ms = (time.perf_counter() - t0) * 1000

        scope = self.row_total
        if filters:
            where = " AND ".join(f'"{f["column"]}" {f["op"]} {_lit(f["value"])}' for f in filters)
            scope = self.con.execute(
                f"SELECT count(*) FROM {self.TABLE} WHERE {where}"
            ).fetchone()[0]

        ev = Evidence(
            metric_name=metric_name, sql=safe, source_columns=source_columns,
            filters_applied=filters or [], rows_in_scope=scope,
            rows_total=self.row_total, duration_ms=round(ms, 2),
        )
        return rows, ev

    def _execute(self, sql: str):
        """ينفّذ الاستعلام بمهلة قصوى.

        ⚠️ `QUERY_TIMEOUT_SECONDS` كان **معلناً ولا يُفرَض**: ثابت في أعلى
        الملف يوحي بوجود حماية لا وجود لها. حارس الـSQL يحدّ عدد الصفوف
        الراجعة لا مقدار العمل — استعلام ثقيل على ملف كبير كان يستطيع
        الاستمرار دقائق ويحتجز خيطاً.

        DuckDB لا يملك `statement_timeout`؛ الوسيلة المتاحة `interrupt()`
        من مؤقّت جانبي، وهي التي نستعملها.
        """
        timer = threading.Timer(self.timeout_seconds, self.con.interrupt)
        timer.daemon = True
        timer.start()
        try:
            return self.con.execute(sql)
        except duckdb.InterruptException as e:
            raise QueryTimeout(
                f"تجاوز الاستعلام {self.timeout_seconds} ثانية وأُوقف. "
                f"جرّب سؤالاً أضيق أو فترة زمنية أقصر."
            ) from e
        finally:
            timer.cancel()

    def _col(self, concept: str) -> str:
        name = self.schema.name_of(concept)
        if not name:
            raise ValueError(f"لا يوجد عمود يمثّل «{concept}» في هذا الملف.")
        return name

    def _where(self, filters: list[dict] | None) -> str:
        if not filters:
            return ""
        parts = [f'"{f["column"]}" {f["op"]} {_lit(f["value"])}' for f in filters]
        return " WHERE " + " AND ".join(parts)

    # ------------------------------------------------------------ المقاييس
    def aggregate(self, concept: str, agg: str = "sum",
                  filters: list[dict] | None = None) -> MetricResult:
        col = self._col(concept)
        sc = self.schema.by_concept(concept)
        unit = sc.unit if sc else None

        # التقسيم لكل عملة لا يصحّ إلا إذا كانت العملة **داخل الصفوف**. أعمدةٌ
        # مالية بعملات مختلفة حالةٌ أخرى: كل عمود يُجمع وحده بعملته هو.
        if (unit == "currency" and self.schema.currency.mixed
                and self.schema.currency.column and agg in ("sum", "avg")):
            return self._aggregate_per_currency(concept, col, agg, filters)

        sql = f'SELECT {agg}("{col}") FROM {self.TABLE}{self._where(filters)}'
        rows, ev = self.run(sql, f"{agg}({concept})", [col], filters)
        val = rows[0][0] if rows and rows[0][0] is not None else 0
        if unit == "currency":
            symbol = symbol_of(sc.currency) if sc and sc.currency else None
            formatted = fmt_currency(val, symbol or self.schema.currency.symbol_ar)
        elif unit == "percent":
            formatted = fmt_percent(val)
        else:
            formatted = fmt_number(val)
        return MetricResult(
            value=float(val), unit=unit,
            formatted_ar=formatted,
            evidence=ev,
        )

    def _aggregate_per_currency(self, concept: str, col: str, agg: str,
                                filters: list[dict] | None) -> MetricResult:
        """ملف بعملتين لا «إجمالي» واحد له.

        جمع 120,000 دولار مع 45,000,000 ليرة يعطي 45,120,000 — رقمٌ لا يقابل
        شيئاً في الواقع، ولا يظهر خطؤه لأنه منسّق وكبير ومعقول الشكل. وهذه
        الحالة هي القاعدة لا الاستثناء في سوق يتعامل بالعملتين معاً.

        فلا نجمع، ولا نحوّل: التحويل يحتاج سعر صرف، وسعر الصرف قرار المالك
        بتاريخ محدّد، لا اجتهاد محرك. نعطي كل عملة مجموعها، والقرار له.
        """
        cur_col = self.schema.currency.column
        sql = (f'SELECT "{cur_col}", {agg}("{col}") FROM {self.TABLE}'
               f'{self._where(filters)} GROUP BY 1 ORDER BY 2 DESC')
        rows, ev = self.run(sql, f"{agg}({concept}) لكل عملة", [col, cur_col], filters)

        totals: dict[str, float] = {}
        for raw, amount in rows:
            if amount is None:
                continue
            code = detect_in_text(str(raw)) or str(raw)
            totals[code] = totals.get(code, 0.0) + float(amount)

        parts = [fmt_currency(v, symbol_of(k) or k)
                 for k, v in sorted(totals.items(), key=lambda kv: -kv[1])]
        return MetricResult(
            value=totals, unit="currency",
            formatted_ar=" + ".join(parts) if parts else "—",
            evidence=ev,
        )

    def aggregate_filtered(self, measure_concept: str, filter_concept: str,
                           filter_value: Any, agg: str = "sum") -> MetricResult:
        """مثل aggregate()، لكن مقيَّدة بصف واحد بقيمة بُعد محددة (مثلاً: مبيعات
        منطقة اللاذقية تحديداً بالاسم). لا منطق حساب جديد — تعيد استخدام معامل
        filters الموجود أصلاً بـaggregate()/run()، فقط تُرجم concept البُعد إلى
        اسم العمود الفعلي وتبني شرط المساواة.

        قسم 10 بالمواصفة المعمارية: القدرة الوحيدة الناقصة فعلياً بمحرك البيانات.
        """
        col = self._col(filter_concept)
        return self.aggregate(measure_concept, agg,
                              filters=[{"column": col, "op": "=", "value": filter_value}])

    def count_rows(self, filters: list[dict] | None = None) -> MetricResult:
        sql = f"SELECT count(*) FROM {self.TABLE}{self._where(filters)}"
        rows, ev = self.run(sql, "count(rows)", [], filters)
        return MetricResult(value=int(rows[0][0]), formatted_ar=fmt_number(rows[0][0]),
                            unit="row", evidence=ev)

    def count_distinct(self, concept: str, filters: list[dict] | None = None) -> MetricResult:
        col = self._col(concept)
        sql = f'SELECT count(DISTINCT "{col}") FROM {self.TABLE}{self._where(filters)}'
        rows, ev = self.run(sql, f"count_distinct({concept})", [col], filters)
        return MetricResult(value=int(rows[0][0]), formatted_ar=fmt_number(rows[0][0]),
                            unit="entity", evidence=ev)

    def top_n(self, measure_concept: str, dimension_concept: str, n: int = 10,
              agg: str = "sum", ascending: bool = False,
              filters: list[dict] | None = None) -> tuple[list[dict], Evidence]:
        m, d = self._col(measure_concept), self._col(dimension_concept)
        order = "ASC" if ascending else "DESC"
        sql = (f'SELECT "{d}" AS label, {agg}("{m}") AS value FROM {self.TABLE}'
               f'{self._where(filters)} GROUP BY "{d}" ORDER BY value {order} LIMIT {n}')
        rows, ev = self.run(sql, f"top_{n}({measure_concept} حسب {dimension_concept})",
                            [m, d], filters)
        return [{"label": r[0], "value": float(r[1] or 0)} for r in rows], ev

    def timeseries(self, measure_concept: str, granularity: str = "month",
                   agg: str = "sum", filters: list[dict] | None = None
                   ) -> tuple[list[dict], Evidence]:
        m, t = self._col(measure_concept), self._col("date")
        trunc = {"day": "day", "week": "week", "month": "month", "year": "year"}[granularity]
        sql = (f'SELECT date_trunc(\'{trunc}\', "{t}") AS period, {agg}("{m}") AS value '
               f'FROM {self.TABLE}{self._where(filters)} '
               f'WHERE "{t}" IS NOT NULL GROUP BY period ORDER BY period'
               if not filters else
               f'SELECT date_trunc(\'{trunc}\', "{t}") AS period, {agg}("{m}") AS value '
               f'FROM {self.TABLE}{self._where(filters)} AND "{t}" IS NOT NULL '
               f'GROUP BY period ORDER BY period')
        rows, ev = self.run(sql, f"سلسلة زمنية({measure_concept}/{granularity})", [m, t], filters)
        return [{"period": str(r[0]), "value": float(r[1] or 0)} for r in rows], ev

    def compare_periods(self, measure_concept: str, p1: tuple[str, str],
                        p2: tuple[str, str], agg: str = "sum",
                        filter_concept: str | None = None,
                        filter_value: Any = None) -> dict[str, Any]:
        """مقارنة فترتين، باختيار مقيَّدة بصف واحد بقيمة بُعد محددة.

        تمديد (لا إعادة بناء) — بلا filter_concept السلوك مطابق تماماً للسابق.
        قسم 10 بالمواصفة المعمارية: filtered comparisons مبنية فوق نفس القدرة
        المستخدمة في aggregate_filtered.
        """
        m, t = self._col(measure_concept), self._col("date")
        filters: list[dict] | None = None
        where = ""
        if filter_concept is not None:
            fcol = self._col(filter_concept)
            filters = [{"column": fcol, "op": "=", "value": filter_value}]
            where = f' WHERE "{fcol}" = {_lit(filter_value)}'
        sql = (f'SELECT '
               f'{agg}(CASE WHEN "{t}" BETWEEN DATE \'{p1[0]}\' AND DATE \'{p1[1]}\' '
               f'THEN "{m}" END) AS v1, '
               f'{agg}(CASE WHEN "{t}" BETWEEN DATE \'{p2[0]}\' AND DATE \'{p2[1]}\' '
               f'THEN "{m}" END) AS v2 FROM {self.TABLE}{where}')
        rows, ev = self.run(sql, f"مقارنة فترتين({measure_concept})", [m, t], filters)
        v1 = float(rows[0][0] or 0)
        v2 = float(rows[0][1] or 0)
        change = ((v2 - v1) / v1 * 100) if v1 else None
        ev.date_range = (p1[0], p2[1])
        return {"period_1": {"range": p1, "value": v1}, "period_2": {"range": p2, "value": v2},
                "change_pct": change, "evidence": ev}

    def date_bounds(self) -> tuple[str, str] | None:
        try:
            t = self._col("date")
        except ValueError:
            return None
        r = self.con.execute(
            f'SELECT min("{t}"), max("{t}") FROM {self.TABLE} WHERE "{t}" IS NOT NULL'
        ).fetchone()
        return (str(r[0]), str(r[1])) if r and r[0] else None

    def concentration(self, measure_concept: str, dimension_concept: str
                      ) -> tuple[dict, Evidence]:
        """تحليل باريتو: كم عنصراً يشكّل 80% من الإجمالي."""
        m, d = self._col(measure_concept), self._col(dimension_concept)
        sql = (f'SELECT "{d}" AS label, sum("{m}") AS value FROM {self.TABLE} '
               f'GROUP BY "{d}" ORDER BY value DESC')
        rows, ev = self.run(sql, f"تركّز({measure_concept} حسب {dimension_concept})", [m, d])
        total = sum(float(r[1] or 0) for r in rows) or 1.0
        cum, k80 = 0.0, 0
        for i, r in enumerate(rows, 1):
            cum += float(r[1] or 0)
            if cum / total >= 0.8:
                k80 = i
                break
        top = rows[0] if rows else None
        return ({
            "total_entities": len(rows), "entities_for_80pct": k80,
            "top_label": top[0] if top else None,
            "top_value": float(top[1]) if top else 0.0,
            "top_share_pct": (float(top[1]) / total * 100) if top else 0.0,
            "grand_total": total,
        }, ev)

    def outlier_rows(self, measure_concept: str, limit: int = 5
                     ) -> tuple[list[dict], Evidence]:
        m = self._col(measure_concept)
        from .cleaning import OUTLIER_PREFIX
        flag = f"{OUTLIER_PREFIX}{m}"
        cols = self.con.execute(f"SELECT * FROM {self.TABLE} LIMIT 0").description
        names = [c[0] for c in cols]
        if flag not in names:
            return [], Evidence(metric_name="outliers", sql="", source_columns=[m])
        sql = (f'SELECT * FROM {self.TABLE} WHERE "{flag}" = true '
               f'ORDER BY "{m}" DESC LIMIT {limit}')
        rows, ev = self.run(sql, f"قيم شاذة({measure_concept})", [m])
        return [dict(zip(names, r)) for r in rows], ev

    def close(self):
        self.con.close()


def _lit(v: Any) -> str:
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v).replace("'", "''")
    return f"'{s}'"
