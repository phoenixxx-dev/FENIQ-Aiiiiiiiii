"""خط معالجة الملف — الجسر الوحيد بين الـAPI ومحرك فينيق.

⚠️ قاعدة ذهبية #4: المحرك (packages/data-engine) مكتبة نقية. الاستيراد باتجاه
واحد فقط: الـAPI يستورد المحرك، والمحرك لا يعرف بوجود الـAPI إطلاقاً.

هذه الوحدة متزامنة (sync) عن قصد: المحرك يعمل على CPU، فيُشغَّل داخل worker
منفصل أو في thread، لا داخل حلقة الأحداث (قاعدة ذهبية #6: لا endpoint > 3 ثوان).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from phoenix import pipeline
from phoenix.ingestion import IngestionError

# نص عربي لكل مرحلة — نفس مراحل المحرك، بلا اختراع مراحل وهمية
STAGE_AR = {
    "validating": "التحقق من الملف",
    "parsing": "قراءة الملف",
    "profiling": "توصيف الأعمدة",
    "semantic": "فهم معاني الأعمدة",
    "cleaning": "تنظيف البيانات",
    "analyzing": "تشغيل التحليلات",
    "insights": "استخراج الاكتشافات",
    "ready": "جاهز",
}

ProgressFn = Callable[[str, str, int], None]


class ProcessingFailed(Exception):
    """فشل معالجة برسالة عربية مفهومة للمستخدم."""

    def __init__(self, message_ar: str, technical: str = ""):
        super().__init__(message_ar)
        self.message_ar = message_ar
        self.technical = technical


def process_local_file(
    file_path: str | Path,
    run_dir: str | Path,
    on_progress: ProgressFn | None = None,
    schema_overrides: dict[str, dict] | None = None,
    cleaning_disabled: list[str] | None = None,
    currency_override: str | None = None,
) -> dict:
    """يشغّل خط أنابيب المحرك ويرجّع ملخصاً جاهزاً للتخزين كميتاداتا.

    كل رقم هنا مصدره المحرك مباشرة — لا حساب ولا تقدير في طبقة الـAPI.
    """
    try:
        run = pipeline.process(file_path, run_dir=run_dir, on_progress=on_progress,
                               schema_overrides=schema_overrides,
                               cleaning_disabled=cleaning_disabled,
                               currency_override=currency_override)
    except IngestionError as e:
        raise ProcessingFailed(str(e), technical=f"IngestionError: {e}") from e
    except Exception as e:                                   # noqa: BLE001
        raise ProcessingFailed(
            "تعذّرت معالجة الملف. تأكد أنه ملف بيانات سليم وحاول مجدداً.",
            technical=f"{type(e).__name__}: {e}",
        ) from e

    return {
        "run_dir": str(run.run_dir),
        "processed_path": str(run.processed_path),
        "detected_format": run.file_info.detected_format,
        "warnings": list(run.file_info.warnings),
        "row_count": run.profile.row_count,
        "column_count": len(run.schema.columns),
        "domain": run.schema.domain,
        "profile_json": json.loads(run.profile.model_dump_json()),
        "schema_json": json.loads(run.schema.model_dump_json()),
        "recipe_json": json.loads(run.recipe.model_dump_json()),
        "changelog_json": json.loads(run.changelog.model_dump_json()),
        "insights": [json.loads(i.model_dump_json()) for i in run.insights],
    }
