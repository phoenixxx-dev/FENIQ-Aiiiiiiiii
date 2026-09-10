"""العقود — النماذج المشتركة بين كل طبقات المحرك.

قاعدة ذهبية #4: هذا الملف (وكل الحزمة) لا يستورد FastAPI ولا SQLAlchemy ولا أي إطار ويب.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, computed_field

# ---------------------------------------------------------------- الاستقبال

class FileInfo(BaseModel):
    path: str
    filename: str
    size_bytes: int
    detected_format: Literal["xlsx", "xls", "csv", "html", "json", "xml"]
    encoding: str | None = None
    delimiter: str | None = None
    sheet_names: list[str] = Field(default_factory=list)
    selected_sheet: str | None = None
    header_row: int = 0
    header_confidence: float = 1.0
    warnings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- التوصيف

class QualityIssue(BaseModel):
    kind: Literal[
        "missing_values", "duplicate_rows", "whitespace", "mixed_case",
        "outliers", "constant_column", "sparse_column", "mixed_types",
        "future_dates", "negative_values", "arabic_indic_digits",
    ]
    severity: Literal["info", "warning", "critical"]
    column: str | None = None
    affected_count: int = 0
    affected_pct: float = 0.0
    message_ar: str = ""


ColumnType = Literal[
    "integer", "float", "date", "datetime", "boolean",
    "categorical", "text", "identifier", "mixed", "empty",
]


class ColumnProfile(BaseModel):
    name: str
    position: int
    inferred_type: ColumnType
    type_confidence: float

    total_count: int
    null_count: int
    null_pct: float
    unique_count: int
    unique_pct: float

    # عددي
    min: float | None = None
    max: float | None = None
    mean: float | None = None
    median: float | None = None
    std: float | None = None
    p25: float | None = None
    p75: float | None = None
    zero_count: int | None = None
    negative_count: int | None = None

    # زمني
    min_date: str | None = None
    max_date: str | None = None
    granularity: str | None = None

    # نصي / فئوي
    top_values: list[tuple[str, int]] = Field(default_factory=list)
    avg_length: float | None = None

    sample_values: list[Any] = Field(default_factory=list)
    quality_issues: list[QualityIssue] = Field(default_factory=list)


class DatasetProfile(BaseModel):
    row_count: int
    column_count: int
    duplicate_row_count: int
    memory_mb: float
    columns: list[ColumnProfile]
    dataset_issues: list[QualityIssue] = Field(default_factory=list)

    def column(self, name: str) -> ColumnProfile | None:
        return next((c for c in self.columns if c.name == name), None)


# ---------------------------------------------------------------- الدلالة

SemanticRole = Literal["measure", "dimension", "time", "identifier", "flag",
                       "constant", "unknown"]


class SemanticColumn(BaseModel):
    column_name: str
    concept: str | None = None
    role: SemanticRole = "unknown"
    confidence: float = 0.0
    detection_method: Literal["dictionary", "pattern", "cross_validation", "derived",
                              "constant", "llm", "user", "none"] = "none"
    unit: str | None = None
    # عملة هذا العمود وحده. في سوق يتعامل بعملتين تشيع قوائم أسعار فيها عمودان:
    # «السعر بالدولار» و«السعر بالليرة». لكل عمود عملته، ولا يُجمعان.
    currency: str | None = None
    evidence_ar: str = ""
    user_overridden: bool = False

    @property
    def needs_review(self) -> bool:
        return self.confidence < 0.7 and not self.user_overridden


class DatasetCurrency(BaseModel):
    """عملة الملف — وكيف عرفناها. `code=None` مع `codes` متعددة تعني ملفاً
    فيه أكثر من عملة: عندها لا يوجد «الإجمالي» واحد أصلاً، بل مجموع لكل عملة."""
    code: str | None = None
    symbol_ar: str | None = None
    source: Literal["column", "values", "column_name", "user", "none"] = "none"
    column: str | None = None
    codes: list[str] = Field(default_factory=list)
    evidence_ar: str = ""

    # computed_field لا @property وحدها: FastAPI يبني الـOpenAPI (ثم أنواع
    # TypeScript) من تفريغ Pydantic، و@property العادية لا تظهر فيه أبداً —
    # الواجهة استعملت cur.mixed قبل أن يوجد في نوعها المولَّد فسقط npm build.
    @computed_field  # type: ignore[prop-decorator]
    @property
    def mixed(self) -> bool:
        return len(self.codes) > 1

    @computed_field  # type: ignore[prop-decorator]
    @property
    def known(self) -> bool:
        return self.code is not None or self.mixed


class SemanticSchema(BaseModel):
    columns: list[SemanticColumn]
    domain: str = "generic"
    currency: DatasetCurrency = Field(default_factory=DatasetCurrency)

    def by_concept(self, concept: str) -> SemanticColumn | None:
        c = [x for x in self.columns if x.concept == concept]
        return max(c, key=lambda x: x.confidence) if c else None

    def by_role(self, role: SemanticRole) -> list[SemanticColumn]:
        return [x for x in self.columns if x.role == role]

    def name_of(self, concept: str) -> str | None:
        col = self.by_concept(concept)
        return col.column_name if col else None


# ---------------------------------------------------------------- التنظيف

class CleaningOperation(BaseModel):
    id: str
    type: str
    target_columns: list[str]
    params: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    auto_suggested: bool = True
    reason_ar: str = ""

    @computed_field  # type: ignore[prop-decorator]
    @property
    def key(self) -> str:
        """معرّف ثابت للعملية عبر المعالجات المختلفة.

        ⚠️ لا نستعمل `id` لهذا: هو ترتيبي (op01, op02...) ويتغيّر لو تغيّرت
        الخطة — فتعطيلُ المستخدمِ عمليةً كان سيقع على عملية أخرى بعد إعادة
        المعالجة. النوع + الأعمدة يبقيان ثابتين لنفس الملف.
        """
        return f"{self.type}:{','.join(sorted(self.target_columns))}"


class OperationResult(BaseModel):
    operation_id: str
    operation_type: str
    rows_affected: int = 0
    cells_changed: int = 0
    columns_affected: list[str] = Field(default_factory=list)
    examples: list[dict[str, Any]] = Field(default_factory=list)
    summary_ar: str = ""


class CleaningRecipe(BaseModel):
    operations: list[CleaningOperation]
    version: int = 1


class ChangeLog(BaseModel):
    results: list[OperationResult]
    rows_before: int
    rows_after: int

    @property
    def total_cells_changed(self) -> int:
        return sum(r.cells_changed for r in self.results)


# ---------------------------------------------------------------- التحليل

class Evidence(BaseModel):
    """قاعدة ذهبية #1: كل رقم يخرج من المحرك يحمل دليله."""
    metric_name: str
    sql: str
    source_columns: list[str]
    filters_applied: list[dict[str, Any]] = Field(default_factory=list)
    date_range: tuple[str, str] | None = None
    rows_in_scope: int = 0
    rows_total: int = 0
    computed_at: datetime = Field(default_factory=datetime.now)
    duration_ms: float = 0.0

    def explain_ar(self) -> str:
        parts = [f"المقياس: {self.metric_name}"]
        if self.source_columns:
            parts.append(f"الأعمدة المستخدمة: {'، '.join(self.source_columns)}")
        parts.append(f"الصفوف المشمولة: {self.rows_in_scope:,} من {self.rows_total:,}")
        if self.date_range:
            parts.append(f"المدى الزمني: {self.date_range[0]} ← {self.date_range[1]}")
        if self.filters_applied:
            f = "، ".join(f"{x['column']} {x['op']} {x['value']}" for x in self.filters_applied)
            parts.append(f"الفلاتر: {f}")
        parts.append(f"زمن التنفيذ: {self.duration_ms:.1f} ملّي ثانية")
        parts.append(f"الاستعلام: {self.sql}")
        return "\n".join(parts)


class MetricResult(BaseModel):
    value: Any
    formatted_ar: str
    unit: str | None = None
    evidence: Evidence


# ---------------------------------------------------------------- الرسوم

class AxisSpec(BaseModel):
    field: str
    label_ar: str
    type: Literal["category", "value", "time"]


class ChartSpec(BaseModel):
    type: Literal["line", "bar", "bar_horizontal", "scatter", "histogram", "donut", "table"]
    title_ar: str
    x: AxisSpec
    y: list[AxisSpec]
    data: list[dict[str, Any]]
    options: dict[str, Any] = Field(default_factory=dict)
    reason_ar: str = ""
    evidence: Evidence | None = None


# ---------------------------------------------------------------- الاكتشافات

class Insight(BaseModel):
    id: str
    type: Literal["growth", "decline", "concentration", "anomaly", "quality", "opportunity"]
    severity: Literal["info", "warning", "critical"]
    icon: str
    title_ar: str
    description_ar: str
    importance_score: float
    evidence: Evidence | None = None


# ---------------------------------------------------------------- الأسئلة

class ToolCall(BaseModel):
    tool: str
    arguments: dict[str, Any]


class Answer(BaseModel):
    question: str
    understood_as: str
    tool_calls: list[ToolCall]
    answer_ar: str
    metrics: list[MetricResult] = Field(default_factory=list)
    chart: ChartSpec | None = None
    confidence: float = 1.0
