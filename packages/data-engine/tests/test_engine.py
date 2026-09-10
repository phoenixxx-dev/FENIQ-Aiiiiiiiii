"""حزمة اختبار المحرك.

القاعدة: تعمل بلا أي خدمة خارجية — لا قاعدة بيانات، لا شبكة، لا API.
هذا هو الاختبار العملي للقاعدة الذهبية #4 (المحرك مكتبة نقية).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import polars as pl
import re

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from phoenix import arabic_text as at
from phoenix.analytics import AnalyticsEngine, SQLGuardError, validate_sql
from phoenix.ask import AskPhoenix, RuleRouter
from phoenix.models import SemanticColumn, SemanticSchema
from phoenix.cleaning import _pick_canonical, execute_recipe, plan_cleaning
from phoenix.ingestion import find_header_row, load
from phoenix.pipeline import process
from phoenix.profiling import infer_type, profile_dataset
from phoenix.semantic import cross_validate_qty_price_total, resolve_schema

FIXTURE = ROOT / "fixtures" / "sales_ar_messy.xlsx"


@pytest.fixture(scope="session", autouse=True)
def ensure_fixture():
    if not FIXTURE.exists():
        subprocess.run([sys.executable, str(ROOT / "tools" / "make_fixture.py")], check=True)


@pytest.fixture(scope="session")
def run(tmp_path_factory):
    return process(FIXTURE, run_dir=tmp_path_factory.mktemp("run"))


# ============================================================ النص العربي
class TestArabicText:
    @pytest.mark.parametrize("raw,expected", [
        ("١٢٣", "123"), ("٠٩٨", "098"), ("۱۲۳", "123"),
        ("الكمية ١٢", "الكمية 12"), ("abc", "abc"),
    ])
    def test_digits_always_western(self, raw, expected):
        assert at.to_western_digits(raw) == expected

    def test_no_indic_digits_leak_in_output(self):
        """أي مخرج تنسيق يجب ألا يحتوي أرقاماً هندية — إطلاقاً."""
        for fn, val in [(at.fmt_number, 1234.5), (at.fmt_currency, 99.9),
                        (at.fmt_percent, 18.4), (at.fmt_compact, 1_500_000)]:
            assert not at.INDIC_DIGIT_RE.search(fn(val)), f"{fn.__name__} سرّب أرقاماً هندية"

    @pytest.mark.parametrize("a,b", [
        ("شركة النور", "شركه النور"), ("مؤسسة الفجر", "مؤسسه الفجر"),
        ("الأمانة", "الامانة"), ("الــكــمية", "الكمية"),
    ])
    def test_normalization_unifies_variants(self, a, b):
        assert at.normalize_ar(a) == at.normalize_ar(b)

    @pytest.mark.parametrize("raw,expected", [
        ("1,250.00 ر.س", 1250.0), ("١٢", 12.0), ("(500)", -500.0),
        ("-45.5", -45.5), ("12%", 12.0), ("١٢٣٤٫٥", 1234.5),
        ("INV-1151", None), ("نقدي", None), ("", None), ("abc123", None),
    ])
    def test_parse_number(self, raw, expected):
        assert at.parse_number(raw) == expected

    def test_invoice_id_never_parsed_as_number(self):
        """انحدار: INV-1151 كان يُقرأ -1151 ويُصنَّف العمود كمقياس."""
        for code in ["INV-1151", "FT-2026-01", "ORD-99", "أ-123"]:
            assert at.parse_number(code) is None

    @pytest.mark.parametrize("raw,expected", [
        ("2026-03-14", (2026, 3, 14)), ("14/03/2026", (2026, 3, 14)),
        ("2026/3/14", (2026, 3, 14)), ("١٤/٠٣/٢٠٢٦", (2026, 3, 14)),
        ("ليس تاريخاً", None),
    ])
    def test_parse_date(self, raw, expected):
        assert at.parse_date_parts(raw) == expected


# ============================================================ الاستقبال
class TestIngestion:
    def test_finds_header_below_title_rows(self):
        raw = [["شركة النور — تقرير"], [""], [""],
               ["رقم الفاتورة", "التاريخ", "الصنف", "الكمية"],
               ["INV-1", "2026-01-01", "قلم", 5],
               ["INV-2", "2026-01-02", "دفتر", 3]]
        row, conf = find_header_row(raw)
        assert row == 3 and conf > 0.5

    def test_loads_and_skips_empty_sheet(self, run):
        assert run.file_info.selected_sheet == "المبيعات"
        assert run.profile.row_count > 400

    def test_rejects_missing_file(self):
        from phoenix.ingestion import IngestionError, validate_file
        with pytest.raises(IngestionError):
            validate_file("/nonexistent/file.xlsx")

    def test_json_flat_array_of_objects_supported(self, tmp_path):
        """JSON بصيغة مصفوفة سجلات مسطّحة أصبح مدعوماً (عيب حقيقي: كان يُرفض
        بالكامل حتى للحالة البسيطة هذه، متل ملف عملاء بسيط)."""
        p = tmp_path / "customers.json"
        p.write_text(
            '[{"CustomerID":"P001","City":"Latakia"},'
            '{"CustomerID":"P002","City":"Latakia"}]',
            encoding="utf-8",
        )
        df, info = load(p)
        assert info.detected_format == "json"
        assert df.height == 2
        assert set(df.columns) == {"CustomerID", "City"}

    def test_json_envelope_object_with_records_array(self, tmp_path):
        """شكل واقعي جداً (وهو شكل ملف المستخدم 08): كائن جذر فيه حقول عامة +
        قائمة السجلات جوّاته. لازم نحلّل القائمة، ونضيف حقول الجذر كأعمدة ثابتة."""
        p = tmp_path / "envelope.json"
        p.write_text(
            '{"warehouse": "Al-Najah", "period": "2026-08",'
            ' "sales": [{"sku": "A1", "qty": 1}, {"sku": "A2", "qty": 2}]}',
            encoding="utf-8",
        )
        df, info = load(p)
        assert info.detected_format == "json"
        assert df.height == 2
        assert {"sku", "qty", "warehouse", "period"} <= set(df.columns)
        assert df["warehouse"].to_list() == ["Al-Najah", "Al-Najah"]
        assert any("ثابتة" in w for w in info.warnings)

    def test_json_inconsistent_fields_unioned_with_warning(self, tmp_path):
        """حقول ناقصة بين سجل وآخر ما عادت سبب رفض — اتحاد الحقول + فراغ مكان
        الناقص + تنبيه صريح، لأن الرفض هون بيخسّر المستخدم بياناته بلا داعي."""
        p = tmp_path / "inconsistent.json"
        p.write_text('[{"a": 1, "b": 2}, {"a": 1, "c": 3}]', encoding="utf-8")
        df, info = load(p)
        assert set(df.columns) == {"a", "b", "c"}
        assert df.height == 2
        assert any("فارغة" in w for w in info.warnings)

    def test_json_nested_object_flattened_to_dotted_columns(self, tmp_path):
        p = tmp_path / "nested_values.json"
        p.write_text(
            '[{"a": 1, "addr": {"city": "Latakia"}}, {"a": 2, "addr": {"city": "Homs"}}]',
            encoding="utf-8",
        )
        df, _ = load(p)
        assert "addr.city" in df.columns
        assert df["addr.city"].to_list() == ["Latakia", "Homs"]

    def test_json_scalar_list_joined_into_text(self, tmp_path):
        p = tmp_path / "list_values.json"
        p.write_text('[{"a": 1, "tags": ["x", "y"]}, {"a": 2, "tags": ["z"]}]', encoding="utf-8")
        df, _ = load(p)
        assert df["tags"].to_list() == ["x, y", "z"]

    def test_json_sub_table_list_still_rejected_clearly(self, tmp_path):
        """قائمة كائنات داخل السجل = جدول فرعي بمستوى تفصيل مختلف — دمجه بصف
        واحد تخمين، فالرفض الصريح أصح."""
        from phoenix.ingestion import IngestionError
        p = tmp_path / "subtable.json"
        p.write_text('[{"id": 1, "lines": [{"sku": "A1"}, {"sku": "A2"}]}]', encoding="utf-8")
        with pytest.raises(IngestionError, match="جدول فرعي"):
            load(p)

    def test_json_deep_nesting_still_rejected_clearly(self, tmp_path):
        from phoenix.ingestion import IngestionError
        p = tmp_path / "deep.json"
        p.write_text('[{"a": {"b": {"c": 1}}}]', encoding="utf-8")
        with pytest.raises(IngestionError, match="تعشيش"):
            load(p)


# ============================================================ التوصيف
class TestProfiling:
    @pytest.mark.parametrize("values,expected", [
        (["1", "2", "3", "4", "5"] * 10, "integer"),
        (["1.5", "2.7", "3.1"] * 10, "float"),
        (["2026-01-01", "2026-02-15"] * 10, "date"),
        (["نعم", "لا", "نعم"] * 10, "boolean"),
        (["الرياض", "جدة", "الرياض"] * 10, "categorical"),
    ])
    def test_type_inference(self, values, expected):
        assert infer_type(values)[0] == expected

    def test_detects_real_problems(self, run):
        kinds = {i.kind for c in run.profile.columns for i in c.quality_issues}
        assert "arabic_indic_digits" in kinds
        assert "whitespace" in kinds
        assert "missing_values" in kinds
        assert run.profile.duplicate_row_count > 0


# ============================================================ الدلالة
class TestSemantic:
    def test_dictionary_detects_arabic_columns(self, run):
        s = run.schema
        assert s.name_of("total_amount") == "الاجمالي"
        assert s.name_of("quantity") == "الكميه"
        assert s.name_of("date") == "التاريخ"
        assert s.name_of("customer") == "العميل"

    def test_cross_validation_works_without_names(self):
        """الاختبار الحاسم: أعمدة بأسماء بلا معنى — هل تُكتشف من الحساب؟"""
        df, _ = load(FIXTURE)
        blind = df.rename({c: f"حقل{i+1}" for i, c in enumerate(df.columns)})
        p = profile_dataset(blind)
        s = resolve_schema(blind, p)

        assert s.name_of("quantity") == "حقل4"
        assert s.name_of("unit_price") == "حقل5"
        assert s.name_of("total_amount") == "حقل6"
        assert s.by_concept("total_amount").detection_method == "cross_validation"

    def test_id_column_not_treated_as_measure(self, run):
        inv = next(c for c in run.schema.columns if c.column_name == "رقم الفاتورة")
        assert inv.role == "identifier"


# ============================================================ التنظيف
class TestCleaning:
    def test_raw_file_untouched(self, run):
        """قاعدة ذهبية #3."""
        assert run.raw_path.exists()
        assert run.raw_path.stat().st_size == FIXTURE.stat().st_size
        assert not (run.raw_path.stat().st_mode & 0o200), "الملف الخام يجب أن يكون للقراءة فقط"

    def test_canonical_prefers_correct_spelling(self):
        counts = {"مؤسسه الفجر": 50, "مؤسسة الفجر": 10}
        assert _pick_canonical(list(counts), counts) == "مؤسسة الفجر"

    def test_entity_merge_reduces_to_true_count(self, run):
        df = pl.read_parquet(run.processed_path)
        assert df["العميل"].n_unique() == 9      # 14 شكل إملائي ← 9 كيانات

    def test_duplicates_removed(self, run):
        assert run.changelog.rows_after < run.changelog.rows_before

    def test_outliers_flagged_never_deleted(self, run):
        df = pl.read_parquet(run.processed_path)
        flag_cols = [c for c in df.columns if c.startswith("_شاذ_")]
        assert flag_cols, "يجب وجود أعمدة وسم"
        assert df[flag_cols[0]].sum() > 0

    def test_changelog_traceable(self, run):
        assert run.changelog.total_cells_changed > 0
        assert any(r.examples for r in run.changelog.results)

    def test_fuzzy_merge_ignores_hyphen_and_spacing_variants(self):
        """عيب حقيقي قِيس بملف مرجعي من مستخدم: "Panadol Extra 500mg" /
        "PANADOL EXTRA 500 MG" / "Panadol-Extra 500mg" هي نفس المنتج (نفس رمز
        الصنف) لكن fuzzy_entity_merge القديم ما كان يوحّدها إطلاقاً (0 من 7 أسماء
        مرجعية اتّحدت) لأنها تختلف بمكان مسافة أو بوجود شرطة فقط."""
        from phoenix.cleaning import _op_fuzzy_merge
        from phoenix.models import CleaningOperation

        df = pl.DataFrame({"Product": [
            "Panadol Extra 500mg", "PANADOL EXTRA 500 MG", "Panadol-Extra 500mg",
            "Dermaxil Cream 30g", "DermaXil cream 30 G",
        ]})
        op = CleaningOperation(id="op01", type="fuzzy_entity_merge",
                               target_columns=["Product"], reason_ar="test")
        new_df, result = _op_fuzzy_merge(df, op)
        assert new_df["Product"].n_unique() == 2
        assert result.cells_changed == 3

    def test_fuzzy_merge_does_not_conflate_different_unit_strengths(self):
        """حماية من الإفراط: "Augmentin 1g" و"Augmentin 1000 mg" يحملان نفس
        الجرعة فعلياً لكن بصيغتين مختلفتين رقمياً — هذا يحتاج فهم تحويل وحدات
        حقيقي، مو مجرد تطبيع نص، فمن الأصح عدم دمجهما تلقائياً بدل تخمين خاطئ."""
        from phoenix.cleaning import _op_fuzzy_merge
        from phoenix.models import CleaningOperation

        df = pl.DataFrame({"Product": ["Augmentin 1g", "Augmentin 1000 mg"]})
        op = CleaningOperation(id="op01", type="fuzzy_entity_merge",
                               target_columns=["Product"], reason_ar="test")
        new_df, _ = _op_fuzzy_merge(df, op)
        assert new_df["Product"].n_unique() == 2


# ============================================================ التحليل
class TestAnalytics:
    @pytest.mark.parametrize("sql", [
        "SELECT * FROM dataset; DROP TABLE dataset",
        "DELETE FROM dataset",
        "SELECT * FROM read_csv('/etc/passwd')",
        "SELECT * FROM other_user_table",
        "INSERT INTO dataset VALUES (1)",
    ])
    def test_sql_guard_blocks(self, sql):
        with pytest.raises(SQLGuardError):
            validate_sql(sql, {"dataset"})

    def test_sql_guard_allows_and_limits(self):
        out = validate_sql("SELECT * FROM dataset", {"dataset"})
        assert "LIMIT" in out.upper()

    def test_numbers_match_independent_calculation(self, run):
        """الاختبار الأهم: هل DuckDB يعطي نفس ما يعطيه Polars؟"""
        df = pl.read_parquet(run.processed_path)
        eng = run.engine()
        try:
            assert abs(eng.aggregate("total_amount", "sum").value - df["الاجمالي"].sum()) < 0.01
            assert abs(eng.aggregate("quantity", "sum").value - df["الكميه"].sum()) < 0.01
            assert abs(eng.aggregate("total_amount", "avg").value - df["الاجمالي"].mean()) < 0.01
            assert eng.count_distinct("customer").value == df["العميل"].n_unique()
        finally:
            eng.close()

    def test_every_result_carries_evidence(self, run):
        eng = run.engine()
        try:
            r = eng.aggregate("total_amount", "sum")
            assert r.evidence.sql and r.evidence.source_columns
            assert r.evidence.rows_in_scope > 0
            assert "الاجمالي" in r.evidence.explain_ar()
        finally:
            eng.close()

    def test_top_n_sorted_and_sums_correctly(self, run):
        eng = run.engine()
        try:
            rows, _ = eng.top_n("total_amount", "salesperson", n=5)
            vals = [r["value"] for r in rows]
            assert vals == sorted(vals, reverse=True)
            df = pl.read_parquet(run.processed_path)
            manual = (df.group_by("المندوب").agg(pl.col("الاجمالي").sum())
                        .sort("الاجمالي", descending=True).head(5))
            assert abs(vals[0] - manual["الاجمالي"][0]) < 0.01
        finally:
            eng.close()


# ============================================================ الاكتشافات
class TestInsights:
    def test_produces_insights_with_evidence(self, run):
        assert len(run.insights) >= 3
        for i in run.insights:
            assert i.title_ar and i.description_ar
            assert not at.INDIC_DIGIT_RE.search(i.description_ar), "أرقام هندية في اكتشاف"

    def test_detects_injected_outlier(self, run):
        """الملف يحوي طلبية 294,000 مزروعة عمداً — هل يجدها المحرك؟"""
        assert any(i.type == "anomaly" for i in run.insights)

    def test_detects_injected_growth(self, run):
        assert any(i.type in ("growth", "decline") for i in run.insights)


# ============================================================ الأسئلة
class TestAsk:
    @pytest.mark.parametrize("q,tool", [
        ("كم إجمالي المبيعات؟", "aggregate"),
        ("مين أكتر 5 منتجات مبيعاً؟", "top_n"),
        ("كم عدد الفواتير؟", "count"),
        ("متوسط قيمة الفاتورة؟", "aggregate"),
        ("المبيعات حسب الشهر", "timeseries"),
        ("مين أفضل مندوب؟", "top_n"),
        ("شو الأصناف الراكدة؟", "stagnant_items"),
        ("قارنلي بين شباط وحزيران", "compare_periods"),
    ])
    def test_router_picks_right_tool(self, run, q, tool):
        assert RuleRouter().route(q, run.schema).tool == tool

    def test_answer_matches_engine_number(self, run):
        """الرقم في النص العربي يجب أن يطابق حساب المحرك بالضبط."""
        eng = run.engine()
        try:
            expected = eng.aggregate("total_amount", "sum").value
            a = AskPhoenix(eng, run.schema).ask("كم إجمالي المبيعات؟")
            assert a.metrics[0].value == expected
            assert at.fmt_number(expected, 2) in a.answer_ar
        finally:
            eng.close()

    def test_answers_never_contain_indic_digits(self, run):
        eng = run.engine()
        try:
            p = AskPhoenix(eng, run.schema)
            for q in p.suggested_questions():
                assert not at.INDIC_DIGIT_RE.search(p.ask(q).answer_ar)
        finally:
            eng.close()

    def test_unknown_question_admits_it(self, run):
        eng = run.engine()
        try:
            a = AskPhoenix(eng, run.schema).ask("ما هي عاصمة اليابان؟")
            assert a.confidence == 0.0
            assert "لم أفهم" in a.answer_ar
        finally:
            eng.close()

    def test_suggestions_come_from_actual_schema(self, run):
        eng = run.engine()
        try:
            assert len(AskPhoenix(eng, run.schema).suggested_questions()) >= 4
        finally:
            eng.close()



# ============================================================ إصلاحات الـRouting
class TestRoutingAuditSafety:
    def test_ranking_without_number_defaults_to_five(self, run):
        call = RuleRouter().route("مين أفضل مندوب؟", run.schema)
        assert call.tool == "top_n"
        assert call.arguments["n"] == 5

    def test_missing_named_dimension_does_not_fallback_to_global_aggregate(self):
        schema = SemanticSchema(columns=[
            SemanticColumn(column_name="الإجمالي", concept="total_amount",
                           role="measure", confidence=1.0),
        ])
        call = RuleRouter().route("أعلى المناطق مبيعات", schema)
        assert call.tool == "unknown"


class TestRoutingAuditFixes:
    def test_alaa_manatiq_mabiaat_routes_to_top_n_by_region(self, run):
        """أعلى المناطق مبيعات → top_n(region, total_amount) لا aggregate كلي."""
        call = RuleRouter().route("أعلى المناطق مبيعات", run.schema)
        assert call.tool == "top_n"
        assert call.arguments["dimension"] == "region"
        assert call.arguments["measure"] == "total_amount"

    def test_aktar_3_manadeeb_mabiaan_uses_total_amount_not_quantity(self, run):
        """أكثر 3 مناديب مبيعاً → total_amount مع n=3."""
        call = RuleRouter().route("أكثر 3 مناديب مبيعاً", run.schema)
        assert call.tool == "top_n"
        assert call.arguments["dimension"] == "salesperson"
        assert call.arguments["measure"] == "total_amount"
        assert call.arguments["n"] == 3

    def test_taht_had_al_talab_routes_to_low_stock_count(self):
        """كم عدد الأصناف تحت حد الطلب؟ → low_stock + count modifier."""
        schema = SemanticSchema(columns=[
            SemanticColumn(column_name="الصنف", concept="product_name",
                           role="dimension", confidence=1.0),
            SemanticColumn(column_name="المخزون", concept="stock_qty",
                           role="measure", confidence=1.0),
            SemanticColumn(column_name="حد الطلب", concept="reorder_level",
                           role="measure", confidence=1.0),
        ])
        call = RuleRouter().route("كم عدد الأصناف تحت حد الطلب؟", schema)
        assert call.tool == "low_stock"
        assert call.arguments["dimension"] == "product_name"
        assert call.arguments["count_only"] is True

    def test_top_five_customers_prefers_ranking(self, run):
        """مين أعلى 5 عملاء؟ → top_n وليس count."""
        call = RuleRouter().route("مين أعلى 5 عملاء؟", run.schema)
        assert call.tool == "top_n"
        assert call.arguments["dimension"] == "customer"
        assert call.arguments["measure"] == "total_amount"
        assert call.arguments["n"] == 5

    def test_ala_madar_alshahr_routes_to_timeseries(self, run):
        """المبيعات على مدار الشهر → timeseries شهرية."""
        call = RuleRouter().route("المبيعات على مدار الشهر", run.schema)
        assert call.tool == "timeseries"
        assert call.arguments["measure"] == "total_amount"
        assert call.arguments["granularity"] == "month"

    def test_reorder_level_question_is_not_low_stock(self):
        """السؤال عن حد إعادة الطلب نفسه ليس سؤال أصناف منخفضة المخزون."""
        schema = SemanticSchema(columns=[
            SemanticColumn(column_name="الصنف", concept="product_name",
                           role="dimension", confidence=1.0),
            SemanticColumn(column_name="المخزون", concept="stock_qty",
                           role="measure", confidence=1.0),
            SemanticColumn(column_name="حد الطلب", concept="reorder_level",
                           role="measure", confidence=1.0),
        ])
        call = RuleRouter().route("شو حد إعادة الطلب؟", schema)
        assert call.tool != "low_stock"

# ============================================================ المسار الكامل
class TestPipeline:
    def test_all_artifacts_written(self, run):
        for f in ["file_info.json", "profile.json", "schema.json",
                  "cleaning_recipe.json", "changelog.json", "insights.json"]:
            assert (run.run_dir / f).exists(), f"مفقود: {f}"
        assert run.processed_path.exists()

    def test_reload_without_reprocessing(self, run):
        from phoenix.pipeline import load_run
        r2 = load_run(run.run_dir)
        assert r2.profile.row_count == run.profile.row_count
        assert r2.schema.name_of("total_amount") == run.schema.name_of("total_amount")

    # قاعدة ذهبية #4: المحرك مكتبة نقية. الحارس يعيش هنا لا في حزمة الـAPI،
    # لأن شرط القاعدة نفسه أن اختبارات المحرك تبقى خضراء لو حُذف apps/api.
    #
    # النسخة القديمة كانت تبحث نصّياً عن «import fastapi»، فيمرّ منها
    # `from fastapi import x` — ولذلك صارت تحليلاً للشجرة النحوية.
    BANNED = {"fastapi", "starlette", "sqlalchemy", "alembic", "asyncpg", "boto3",
              "arq", "redis", "flask", "django"}
    # عملاء الشبكة ممنوعة في كل وحدة إلا حدّ الشبكة المُعلَن (انظر ق-36):
    # الحارس يحرس الخاصية «لا شبكة إلا هنا»، لا قائمة أسماء مكتبات.
    NETWORK = {"requests", "httpx", "aiohttp", "urllib3", "socket", "http",
               "google", "openai", "anthropic", "cohere", "websockets"}
    NETWORK_BOUNDARY = {"llm_provider.py"}

    @staticmethod
    def _top_level_imports(py):
        import ast
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    yield a.name.split(".")[0]
            elif isinstance(node, ast.ImportFrom) and not node.level:
                yield (node.module or "").split(".")[0]

    def test_engine_has_no_web_framework_imports(self):
        offenders = [f"{py.name}: {n}" for py in (ROOT / "phoenix").rglob("*.py")
                     for n in self._top_level_imports(py) if n in self.BANNED]
        assert offenders == [], f"المحرك يستورد اعتماديات خدمة: {offenders}"

    def test_network_lives_only_in_the_declared_boundary_file(self):
        offenders = [f"{py.name}: {n}" for py in (ROOT / "phoenix").rglob("*.py")
                     if py.name not in self.NETWORK_BOUNDARY
                     for n in self._top_level_imports(py) if n in self.NETWORK]
        assert offenders == [], (
            "عميل شبكة خارج phoenix/llm_provider.py — القاعدة #4: "
            f"{offenders}")


# ============================================================ انحدار: اختبارات أعمى
class TestRegressionFromBlindTests:
    """كل اختبار هنا يحرس عيباً حقيقياً ظهر في اختبار أعمى على ملف مستخدم."""

    def test_unsupported_formats_rejected_loudly(self, tmp_path):
        """العيب الأخطر: HTML كان يُعامل كـCSV وينتج 601 صف هراء ويعلن النجاح.

        ملاحظة: حالة JSON انشالت من هون بعد ما صار JSON مدعوماً فعلياً — ما عاد
        رفضه سلوكاً مطلوباً."""
        from phoenix.ingestion import IngestionError, detect_format
        for content, name in [(b"%PDF-1.4 x", "a.pdf"),
                              (b"binary junk no delimiters\nmore junk\n", "c.bin")]:
            p = tmp_path / name
            p.write_bytes(content)
            with pytest.raises(IngestionError):
                detect_format(p)

    def test_xml_repeated_records_supported(self, tmp_path):
        """عيب حقيقي على ملف مستخدم: XML كان يُصنَّف "html" لمجرد بدئه بـ"<?xml"،
        فيفشل برسالة مربكة ("لا يوجد جدول"). هلأ XML بشكل سجلات متكررة مسطّحة
        (نفس شكل ملفات المستخدم الحقيقية) مدعوم فعلياً."""
        p = tmp_path / "sales.xml"
        p.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<warehouse period="2026-08">'
            '<sale id="S01"><sku>A100</sku><product>Panadol Extra</product>'
            '<qty>100</qty><unit_price>12500</unit_price></sale>'
            '<sale id="S02"><sku>A101</sku><product>Augmentin 1g</product>'
            '<qty>50</qty><unit_price>38000</unit_price></sale>'
            '</warehouse>',
            encoding="utf-8",
        )
        df, info = load(p)
        assert info.detected_format == "xml"
        assert df.height == 2
        assert set(df.columns) == {"id", "sku", "product", "qty", "unit_price"}

    def test_xml_without_repeated_elements_rejected_clearly(self, tmp_path):
        """XML بلا عناصر متكررة (سجل واحد فقط) ما إلو شكل جدول واضح — رفض صريح
        يشرح السبب بدل تخمين، بدل ما يفشل برسالة "لا يوجد جدول" المربكة القديمة."""
        from phoenix.ingestion import IngestionError, detect_format
        p = tmp_path / "single.xml"
        p.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<warehouse><sale><sku>A1</sku><qty>1</qty></sale></warehouse>',
            encoding="utf-8",
        )
        with pytest.raises(IngestionError, match="سجل"):
            detect_format(p)

    def test_xml_inconsistent_records_rejected_clearly(self, tmp_path):
        from phoenix.ingestion import IngestionError, detect_format
        p = tmp_path / "inconsistent.xml"
        p.write_text(
            '<root><item><a>1</a></item><item><a>1</a><b>2</b></item></root>',
            encoding="utf-8",
        )
        with pytest.raises(IngestionError, match="متسقة"):
            detect_format(p)

    def test_xml_nested_records_rejected_clearly(self, tmp_path):
        from phoenix.ingestion import IngestionError, detect_format
        p = tmp_path / "nested.xml"
        p.write_text(
            '<root><item><a><x>1</x></a></item><item><a><x>2</x></a></item></root>',
            encoding="utf-8",
        )
        with pytest.raises(IngestionError, match="متشعبة"):
            detect_format(p)

    def test_xml_with_actual_html_table_still_parsed_as_html(self, tmp_path):
        """لا نكسر الحالة الشرعية: XHTML يبدأ بـ"<?xml" لكنه فعلاً فيه <table>."""
        from phoenix.ingestion import detect_format
        p = tmp_path / "t.xhtml"
        p.write_text(
            '<?xml version="1.0"?><html><body><table><tr><td>a</td></tr></table></body></html>',
            encoding="utf-8",
        )
        assert detect_format(p) == "html"

    def test_numeric_looking_identifier_keeps_leading_zeros(self, tmp_path):
        """عيب حقيقي على ملف مستخدم: SKU "001" كان يتحول لرقم عائم "1.0"
        فتضيع الأصفار البادئة، لأن التوصيف كان يعامل أي عمود رقمي بالكامل كرقم
        بغض النظر عن فقدان المعلومة."""
        import polars as pl
        from phoenix import cleaning, profiling

        df = pl.DataFrame({
            "SKU": [f"{i:03d}" for i in range(1, 12)],   # "001".."011" — أكثر من 10 صفوف
            "Qty": list(range(1, 12)),
        })
        profile = profiling.profile_dataset(df)
        sku_profile = profile.column("SKU")
        assert sku_profile.inferred_type == "identifier"

        from phoenix import semantic
        schema = semantic.resolve_schema(df, profile)
        recipe = cleaning.plan_cleaning(profile, schema)
        clean_df, _ = cleaning.execute_recipe(df, recipe)
        assert clean_df["SKU"].to_list()[0] == "001"

    def test_ambiguous_sales_column_left_unclassified_without_corroboration(self):
        """عيب حقيقي على ملف مستخدم: عمود "Sales" بدفتر مخزون (قيم صغيرة = عدد
        قطع، بلا أي عمود مالي آخر بالملف) صُنِّف كـtotal_amount/currency بثقة 1.0،
        فأعطى "155.00 ر.س" لعدد وحدات فعلياً 155 قطعة."""
        df = pl.DataFrame({
            "SKU": ["A100", "A101", "A102", "A103", "A104", "A105", "A106"],
            "Product": ["a", "b", "c", "d", "e", "f", "g"],
            "OpeningStock": [100, 50, 30, 80, 25, 10, 0],
            "Purchases": [75, 20, 40, 10, 0, 12, 5],
            "Sales": [20, 35, 0, 75, 25, 0, 0],
            "ExpectedClosing": [155, 35, 70, 15, 0, 22, 5],
        })
        profile = profile_dataset(df)
        schema = resolve_schema(df, profile)
        sales_col = next(c for c in schema.columns if c.column_name == "Sales")
        assert sales_col.concept is None
        assert sales_col.unit is None
        assert schema.by_concept("total_amount") is None

    def test_sales_column_kept_as_currency_when_corroborated(self):
        """نفس كلمة "Sales" بملف فيه أعمدة مالية أخرى (Collected/Outstanding)
        تصنَّف بثقة كمبلغ مالي فعلاً — الغموض سياقي وليس رفضاً مطلقاً للكلمة."""
        df = pl.DataFrame({
            "Rep": ["Ahmed", "Laith", "Reham", "Aws"],
            "Sales": [5000000, 4200000, 3800000, 5100000],
            "Collected": [4800000, 3000000, 3700000, 2500000],
            "Outstanding": [200000, 1200000, 100000, 2600000],
        })
        profile = profile_dataset(df)
        schema = resolve_schema(df, profile)
        sales_col = next(c for c in schema.columns if c.column_name == "Sales")
        assert sales_col.concept == "total_amount"
        assert sales_col.unit == "currency"
        assert schema.by_concept("collected_amount") is not None
        assert schema.by_concept("outstanding_amount") is not None

    def test_opening_debt_not_confused_with_opening_stock(self):
        """عيب أدخلته إضافة مفهوم opening_stock نفسه ثم اكتُشف وأُصلح: عمود
        "OpeningDebt" (مبلغ مالي بذمم عميل) كان يطابق alias "opening" العام
        فيُصنَّف خطأً كـopening_stock (عدد قطع)، بينما "OpeningStock" الحقيقي
        بملف مخزون لازم يبقى يُصنَّف صح."""
        df = pl.DataFrame({
            "CustomerID": ["C01", "C02", "C03"],
            "OpeningDebt": [1000000, 500000, 200000],
            "Payments": [500000, 100000, 0],
        })
        profile = profile_dataset(df)
        schema = resolve_schema(df, profile)
        opening_debt = next(c for c in schema.columns if c.column_name == "OpeningDebt")
        assert opening_debt.concept != "opening_stock"

        df2 = pl.DataFrame({
            "SKU": ["A1", "A2", "A3"],
            "OpeningStock": [100, 50, 30],
        })
        profile2 = profile_dataset(df2)
        schema2 = resolve_schema(df2, profile2)
        assert schema2.by_concept("opening_stock") is not None

    def test_split_concept_columns_merged_when_mutually_exclusive(self, tmp_path):
        """عيب حقيقي على ملف مستخدم (JSON غير منتظم): نفس الحقل مكتوب "qty"
        بسجلات و"quantity" بسجلات تانية، فانقسمت البيانات على عمودين وصار
        الإجمالي ناقصاً بصمت."""
        p = tmp_path / "split.json"
        p.write_text(
            '[{"sku":"A1","qty":100,"price":10},'
            ' {"sku":"A2","quantity":30,"price":10}]',
            encoding="utf-8",
        )
        run = process(p, run_dir=tmp_path / "run")
        df = pl.read_parquet(run.processed_path)
        assert "quantity" not in df.columns or "qty" not in df.columns
        qty_col = run.schema.by_concept("quantity")
        assert qty_col is not None
        assert df[qty_col.column_name].to_list() == [100.0, 30.0]
        assert any("دمجهما" in w for w in run.file_info.warnings)

    def test_same_concept_columns_not_merged_when_they_co_occur(self, tmp_path):
        """حماية من الإفراط: عمودان بنفس المفهوم لكن يتعبّى فيهما نفس الصف
        (كمية مطلوبة وكمية مباعة) هما حقلان مختلفان فعلاً — ممنوع دمجهما."""
        p = tmp_path / "two_qty.csv"
        p.write_text("الكمية المطلوبة,الكمية المباعة\n10,8\n5,5\n", encoding="utf-8")
        run = process(p, run_dir=tmp_path / "run")
        df = pl.read_parquet(run.processed_path)
        assert "الكمية المطلوبة" in df.columns
        assert "الكمية المباعة" in df.columns

    def test_exchange_rate_column_recognized(self):
        """يسمح بالسؤال عن سعر الصرف نفسه إذا وُجد كعمود بالملف — لا يحلّ فخّ
        العملة كاملاً (يحتاج ربط بملف تاني بالتاريخ) لكنه خطوة أولى مفيدة."""
        df = pl.DataFrame({
            "Date": ["2026-08-01", "2026-08-02"],
            "SYP_per_USD": [13000, 14000],
        })
        profile = profile_dataset(df)
        schema = resolve_schema(df, profile)
        assert schema.by_concept("exchange_rate") is not None

    def test_inventory_ledger_concepts_recognized(self):
        """عيب حقيقي: OpeningStock/Purchases/ExpectedClosing كانت تُصنَّف
        identifier/None بالكامل، فأي سؤال عنها كان يُتجاهل صامتاً."""
        df = pl.DataFrame({
            "SKU": ["A100", "A101", "A102"],
            "OpeningStock": [100, 50, 30],
            "Purchases": [75, 20, 40],
            "ExpectedClosing": [155, 35, 70],
        })
        profile = profile_dataset(df)
        schema = resolve_schema(df, profile)
        assert schema.by_concept("opening_stock") is not None
        assert schema.by_concept("purchases") is not None
        assert schema.by_concept("closing_stock") is not None

    def test_missing_total_amount_derived_from_quantity_times_price(self, tmp_path):
        """عيب حقيقي على ملف مستخدم: فاتورة فيها Qty وListPrice لكن بلا عمود
        إجمالي جاهز، فسؤال "كم إجمالي المبيعات؟" كان يرجع 347 (مجموع الكمية) بدل
        مبلغ مالي، لأن total_amount ما كان موجوداً إطلاقاً فوقع الاختيار على أول
        مقياس متاح (quantity) بالخطأ."""
        from phoenix.ask import RuleRouter, ToolExecutor

        p = tmp_path / "no_total.csv"
        p.write_text(
            "SKU,Product,Qty,ListPrice\n"
            "001,Panadol,120,12500\n"
            "002,Augmentin,40,38000\n",
            encoding="utf-8",
        )
        run = process(p, run_dir=tmp_path / "run")
        total_col = run.schema.by_concept("total_amount")
        assert total_col is not None
        assert total_col.detection_method == "derived"
        assert total_col.unit == "currency"

        eng = run.engine()
        executor = ToolExecutor(eng, run.schema)
        call = RuleRouter().route("كم إجمالي المبيعات؟", run.schema)
        ans = executor.execute(call, "كم إجمالي المبيعات؟")
        assert "1,500,000" in ans.answer_ar or "3,020,000" in ans.answer_ar
        eng.close()

    def test_total_amount_not_derived_when_column_already_exists(self, tmp_path):
        """لا نشتق عموداً محسوباً إذا في عمود إجمالي أصلي أصلاً — الأصلي دايماً أولى."""
        p = tmp_path / "has_total.csv"
        p.write_text(
            "SKU,Qty,ListPrice,Total\n001,10,100,999\n002,5,200,999\n",
            encoding="utf-8",
        )
        run = process(p, run_dir=tmp_path / "run")
        total_col = run.schema.by_concept("total_amount")
        assert total_col is not None
        assert total_col.detection_method != "derived"
        assert total_col.column_name == "Total"

    def test_html_tables_readable(self, tmp_path):
        from phoenix.ingestion import load
        p = tmp_path / "t.html"
        p.write_text(
            '<html><head><meta charset="UTF-8"></head><body><table>'
            "<tr><th>الصنف</th><th>الكمية</th></tr>"
            "<tr><td>بنادول</td><td>5</td></tr>"
            "<tr><td>شاش</td><td>3</td></tr></table></body></html>",
            encoding="utf-8")
        df, info = load(p)
        assert info.detected_format == "html"
        assert df.height == 2 and "الصنف" in df.columns

    def test_declared_charset_beats_guess(self, tmp_path):
        """العيب: تجاهُل <meta charset> شوّه العربي إلى «ط§ظ„ظ…ظ†طھط¬»."""
        from phoenix.ingestion import detect_encoding
        p = tmp_path / "u.html"
        p.write_bytes('<meta charset="UTF-8"><table><tr><td>المنتج</td></tr></table>'
                      .encode("utf-8"))
        assert detect_encoding(p) in ("utf-8", "utf-8-sig")

    @pytest.mark.parametrize("token", ["—", "–", "-", "N/A", "لا يوجد", "", "  "])
    def test_common_null_markers_recognized(self, token):
        """العيب: «—» أسقط عمود أسعار كامل من رقم إلى نص."""
        assert at.is_blank(token)

    def test_price_column_with_dashes_still_numeric(self):
        assert infer_type(["$0.80", "$1.52", "—", "$1.05", "$0.84"])[0] == "float"

    def test_dictionary_beats_uniqueness_on_small_tables(self):
        """العيب: «المخزون الحالي» صار identifier لأن 10 قيم كلها فريدة."""
        df = pl.DataFrame({"الصنف": [f"صنف{i}" for i in range(10)],
                           "المخزون الحالي": [503, 144, 177, 209, 254, 492, 650, 742, 171, 202]})
        s = resolve_schema(df, profile_dataset(df))
        assert s.name_of("stock_qty") == "المخزون الحالي"

    def test_superlative_with_dimension_ranks_not_aggregates(self):
        """العيب الصامت الأخطر: «أكتر الصيدليات شراءً» أُجيب بـmax(الإجمالي)
        — رقم صحيح حسابياً لكنه جواب على سؤال مختلف تماماً."""
        from phoenix.models import SemanticColumn, SemanticSchema
        schema = SemanticSchema(columns=[
            SemanticColumn(column_name="اسم الصيدلية", concept="customer",
                           role="dimension", confidence=1.0),
            SemanticColumn(column_name="الإجمالي", concept="total_amount",
                           role="measure", confidence=1.0, unit="currency"),
        ])
        call = RuleRouter().route("مين أكتر الصيدليات شراءً؟", schema)
        assert call.tool == "top_n"
        assert call.arguments["dimension"] == "customer"

    def test_low_stock_needs_real_stock_column(self):
        from phoenix.models import SemanticColumn, SemanticSchema
        schema = SemanticSchema(columns=[
            SemanticColumn(column_name="الإجمالي", concept="total_amount",
                           role="measure", confidence=1.0),
        ])
        assert RuleRouter().route("شو الأصناف اللي عندها مخزون منخفض؟", schema).tool == "low_stock"


# ======================================================= تطبيع البحث في Polars
class TestNormalizeExprMatchesPython:
    """النسختان (نصّية وPolars) يجب ألا تتباعدا — وإلا صار البحث يجد ما لا
    يجده التطابق، أو العكس."""

    @pytest.mark.parametrize("value", [
        "شركة النور للتجارة", "مؤسسة الفجر", "التقنية الحديثة",
        "الرياض للتوريدات", "الكمـية", "دِمَشْق", "١٢٣ شارع",
        "  مسافات   زائدة  ", "ABC مختلط 12", "", "إبراهيم", "آمال", "ىسرى",
    ])
    def test_same_output_as_normalize_ar(self, value):
        from phoenix.arabic_text import normalize_ar, normalize_expr
        got = (pl.DataFrame({"v": [value]})
               .select(normalize_expr(pl.col("v")).alias("n"))["n"][0])
        assert got == normalize_ar(value)


class TestArabicMessagesRenderCorrectly:
    """رسائل المحرك تُعرض كما هي في واجهة عربية — فاتجاهها جزء من صحتها."""

    # حقول تصل المستخدم فعلاً. التعليقات وdocstrings لا تُعرض فلا تُفحص.
    USER_FACING = ("message_ar", "summary_ar", "title_ar", "description_ar",
                   "reason_ar", "IngestionError", "warnings.append", "label_ar")
    # مدى قصير بشرطة («0-9»، «1-5») ينقلب بصرياً داخل جملة عربية.
    # التواريخ (2026-08-01) مستثناة: أربع خانات فأكثر، وتُعرض ضمن سياق واضح.
    RANGE = re.compile(r"(?<!\d)\d{1,3}\s*-\s*\d{1,3}(?!\d)")

    def test_no_user_facing_message_contains_an_ambiguous_number_range(self):
        """«0-9» داخل جملة عربية يُعرض «9-0» على الشاشة — عيب حقيقي شوهد."""
        offenders = []
        for py in (ROOT / "phoenix").rglob("*.py"):
            for i, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if not any(k in line for k in self.USER_FACING):
                    continue
                if re.search(r"[؀-ۿ]", line) and self.RANGE.search(line):
                    offenders.append(f"{py.name}:{i}: {stripped[:80]}")
        assert offenders == [], offenders
