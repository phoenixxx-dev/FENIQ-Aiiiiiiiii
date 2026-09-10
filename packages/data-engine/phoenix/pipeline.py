"""خط الأنابيب الكامل: رفع ← توصيف ← دلالة ← تنظيف ← تحليل ← اكتشافات.

قاعدة ذهبية #3: الملف الأصلي لا يُمس. المخرجات في مجلد run منفصل.
"""
from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import polars as pl

from . import cleaning, insights, ingestion, profiling, semantic
from . import currency as currency_mod
from .analytics import AnalyticsEngine
from .models import (ChangeLog, CleaningRecipe, DatasetProfile, FileInfo,
                     Insight, SemanticColumn, SemanticSchema)

STAGES = [
    ("validating", "التحقق من الملف", 10),
    ("parsing", "قراءة الملف", 25),
    ("profiling", "توصيف الأعمدة", 40),
    ("semantic", "فهم معاني الأعمدة", 55),
    ("cleaning", "تنظيف البيانات", 70),
    ("analyzing", "تشغيل التحليلات", 85),
    ("insights", "استخراج الاكتشافات", 95),
    ("ready", "جاهز", 100),
]


@dataclass
class DatasetRun:
    """كل ما نعرفه عن ملف بعد معالجته."""
    run_dir: Path
    file_info: FileInfo
    profile: DatasetProfile
    schema: SemanticSchema
    recipe: CleaningRecipe
    changelog: ChangeLog
    insights: list[Insight] = field(default_factory=list)
    raw_path: Path | None = None
    processed_path: Path | None = None
    duration_s: float = 0.0

    def engine(self) -> AnalyticsEngine:
        return AnalyticsEngine(self.processed_path, self.schema)


def process(
    file_path: str | Path,
    run_dir: str | Path | None = None,
    on_progress: Callable[[str, str, int], None] | None = None,
    schema_overrides: dict[str, dict] | None = None,
    cleaning_disabled: list[str] | None = None,
    currency_override: str | None = None,
) -> DatasetRun:
    """يعالج ملفاً كاملاً.

    schema_overrides: تصحيحات المستخدم لأدوار الأعمدة. تُطبَّق بعد الاستنتاج
    مباشرة، فيرث كلُّ ما بعدها (التنظيف، الأعمدة المشتقّة، التحليلات،
    الاكتشافات) القرارَ الصحيح — لا الاستنتاج الخاطئ.

    cleaning_disabled: مفاتيح عمليات تنظيف رفضها المستخدم.
    """
    t0 = time.perf_counter()
    src = Path(file_path)
    run = Path(run_dir or Path("runs") / f"{src.stem}_{int(time.time())}")
    (run / "raw").mkdir(parents=True, exist_ok=True)

    def emit(key: str):
        if on_progress:
            stage = next(s for s in STAGES if s[0] == key)
            on_progress(stage[0], stage[1], stage[2])

    # 1) تحقق + نسخة خام غير قابلة للمساس
    emit("validating")
    ingestion.validate_file(src)
    raw_copy = run / "raw" / src.name
    shutil.copy2(src, raw_copy)
    raw_copy.chmod(0o444)      # للقراءة فقط — الحماية على مستوى نظام الملفات

    # 2) قراءة
    emit("parsing")
    df, info = ingestion.load(raw_copy)

    df = _merge_split_concept_columns(df, info)

    # 3) توصيف
    emit("profiling")
    profile = profiling.profile_dataset(df)

    # 4) دلالة
    emit("semantic")
    schema = semantic.resolve_schema(df, profile)
    if schema_overrides:
        schema = semantic.apply_overrides(schema, schema_overrides)
    if currency_override:
        schema = currency_mod.apply_override(schema, currency_override)

    # 5) تنظيف
    emit("cleaning")
    recipe = cleaning.plan_cleaning(profile, schema)
    recipe = cleaning.apply_disabled(recipe, cleaning_disabled)
    clean_df, changelog = cleaning.execute_recipe(df, recipe)
    clean_df, schema = _derive_missing_total_amount(clean_df, schema)
    processed = run / "processed.parquet"
    clean_df.write_parquet(processed)

    # 6) تحليل
    emit("analyzing")
    eng = AnalyticsEngine(processed, schema)

    # 7) اكتشافات
    emit("insights")
    found = insights.generate(eng, schema, profile)
    eng.close()

    result = DatasetRun(
        run_dir=run, file_info=info, profile=profile, schema=schema,
        recipe=recipe, changelog=changelog, insights=found,
        raw_path=raw_copy, processed_path=processed,
        duration_s=round(time.perf_counter() - t0, 2),
    )
    _save(result)
    emit("ready")
    return result


def _merge_split_concept_columns(df: pl.DataFrame, info: FileInfo) -> pl.DataFrame:
    """يدمج عمودين اسمهما مختلف لكن معناهما واحد ("qty" و"quantity") لما تكون
    البيانات نفسها تثبت إنهما نفس الحقل: ما في ولا صف معبّى فيه الاثنان معاً.

    عيب حقيقي شُوهد على ملف مستخدم (JSON مصدَّر من نظام غير منتظم): نفس الحقل
    مكتوب بمفتاحين مختلفين حسب السجل، فانقسمت البيانات على عمودين وصار الإجمالي
    ناقصاً بصمت.

    الشرطان معاً إلزاميان — الاسم وحده ما يكفي: عمودان متل «الكمية المطلوبة»
    و«الكمية المباعة» بينتموا لنفس المفهوم بس بيتعبّوا بنفس الصف، فهنّ حقلان
    مختلفان فعلاً وما بينندمجوا.
    """
    concepts = semantic.load_dictionary()
    by_concept: dict[str, list[str]] = {}
    for name in df.columns:
        sc = semantic.match_dictionary(name, "mixed", concepts)
        if sc and sc.concept and sc.confidence >= 0.9:
            by_concept.setdefault(sc.concept, []).append(name)

    for concept, cols in by_concept.items():
        if len(cols) < 2:
            continue
        keep = cols[0]
        for other in cols[1:]:
            if keep not in df.columns or other not in df.columns:
                continue
            overlap = (df[keep].is_not_null() & df[other].is_not_null()).sum()
            if overlap:
                continue
            if df[keep].dtype != df[other].dtype:
                df = df.with_columns(
                    pl.col(keep).cast(pl.Utf8, strict=False),
                    pl.col(other).cast(pl.Utf8, strict=False),
                )
            df = df.with_columns(
                pl.when(pl.col(keep).is_null()).then(pl.col(other))
                  .otherwise(pl.col(keep)).alias(keep)
            ).drop(other)
            info.warnings.append(
                f"العمودان «{keep}» و«{other}» يمثّلان نفس المعنى ({concept}) ولا يجتمعان "
                f"بأي صف — تم دمجهما بعمود واحد «{keep}» حتى لا تنقسم البيانات."
            )
    return df


def _derive_missing_total_amount(
    df: pl.DataFrame, schema: SemanticSchema
) -> tuple[pl.DataFrame, SemanticSchema]:
    """لو الملف فيه quantity وunit_price لكن بلا عمود إجمالي جاهز، نشتق عموداً
    محسوباً = quantity × unit_price بدل ترك سؤال "كم الإجمالي؟" بلا إجابة أو
    يرجع كمية بالغلط (عيب حقيقي شُوهد على ملف مستخدم).

    قاعدة ذهبية #1: كل رقم من محرك البيانات — وهذا حساب مباشر على أعمدة تحقّقنا
    منها فعلاً، لا تخمين ولا استدعاء AI. الشفافية بأنه "محسوب" لا "أصلي" محفوظة
    بـdetection_method="derived" وevidence_ar الواضحة.
    """
    if schema.by_concept("total_amount") is not None:
        return df, schema
    # الرصيد × السعر = **قيمة المخزون** — وهي أهم رقم لصاحب مستودع، وكانت
    # تضيع لأن الاشتقاق كان يشترط «كمية مباعة» تحديداً.
    qty_col = schema.by_concept("quantity") or schema.by_concept("stock_qty")
    price_col = schema.by_concept("unit_price")
    if qty_col is None or price_col is None:
        return df, schema
    if qty_col.column_name not in df.columns or price_col.column_name not in df.columns:
        return df, schema

    stock = qty_col.concept == "stock_qty"
    base = "_محسوب_قيمة_المخزون" if stock else "_محسوب_الإجمالي"
    derived_name = base
    suffix = 1
    while derived_name in df.columns:
        suffix += 1
        derived_name = f"{base}_{suffix}"

    qty_series = df[qty_col.column_name].cast(pl.Float64, strict=False)
    price_series = df[price_col.column_name].cast(pl.Float64, strict=False)
    df = df.with_columns((qty_series * price_series).alias(derived_name))

    new_col = SemanticColumn(
        column_name=derived_name, concept="total_amount", role="measure",
        confidence=0.9, detection_method="derived", unit="currency",
        evidence_ar=((f"لا يوجد عمود قيمة جاهز بالملف — قيمة المخزون محسوبة تلقائياً = "
                      f"«{qty_col.column_name}» (الرصيد) × «{price_col.column_name}» (السعر).")
                     if stock else
                     (f"لا يوجد عمود إجمالي جاهز بالملف — محسوب تلقائياً = "
                      f"«{qty_col.column_name}» × «{price_col.column_name}».")),
    )
    return df, SemanticSchema(columns=schema.columns + [new_col], domain=schema.domain,
                              currency=schema.currency)


def _save(r: DatasetRun) -> None:
    """كل مخرج يُحفظ كـJSON — هذه بالضبط ما ستقرأه طبقة الـAPI لاحقاً."""
    def w(name: str, obj):
        (r.run_dir / name).write_text(
            json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )

    w("file_info.json", r.file_info.model_dump())
    w("profile.json", r.profile.model_dump())
    w("schema.json", r.schema.model_dump())
    w("cleaning_recipe.json", r.recipe.model_dump())
    w("changelog.json", r.changelog.model_dump())
    w("insights.json", [i.model_dump() for i in r.insights])


def load_run(run_dir: str | Path) -> DatasetRun:
    """يعيد تحميل معالجة سابقة دون إعادة تشغيلها."""
    d = Path(run_dir)

    def rd(name: str):
        return json.loads((d / name).read_text(encoding="utf-8"))

    return DatasetRun(
        run_dir=d,
        file_info=FileInfo(**rd("file_info.json")),
        profile=DatasetProfile(**rd("profile.json")),
        schema=SemanticSchema(**rd("schema.json")),
        recipe=CleaningRecipe(**rd("cleaning_recipe.json")),
        changelog=ChangeLog(**rd("changelog.json")),
        insights=[Insight(**i) for i in rd("insights.json")],
        processed_path=d / "processed.parquet",
    )
