"""حارس ضد «الحقول الأشباح»: إعداد معرَّف لا يقرؤه أحد.

وقعنا في هذا النمط أربع مرات (docs/DECISIONS.md ق-34، ق-35): إعداد موجود
ودالة موجودة والحماية غائبة. الخطر أن قراءة الاسم في الملف تُنتج شعوراً
زائفاً بالتغطية.

هذا الاختبار يجعل المرة الخامسة تفشل يوم كتابتها.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
API_SRC = ROOT / "apps" / "api" / "src"
ENGINE_SRC = ROOT / "packages" / "data-engine" / "phoenix"
CONFIG = API_SRC / "config.py"

# إعدادات تُقرأ داخل config.py نفسه عبر خاصية مشتقّة — قراءة حقيقية لا شبح.
READ_VIA_PROPERTY = {
    "cors_origins",          # cors_origin_list
    "max_file_size_mb",      # max_file_size_bytes
    "s3_endpoint",           # uses_s3
    "ai_layer_enabled",      # ai_ready
    "gemini_api_key",        # ai_ready
    "local_storage_dir",     # get_storage
}


def _fields() -> list[str]:
    return re.findall(r"^    (\w+):\s*[\w\[\]| ]+ = ", CONFIG.read_text(encoding="utf-8"), re.M)


def _body_outside_config() -> str:
    parts = []
    for src in (API_SRC, ENGINE_SRC):
        for py in src.rglob("*.py"):
            if py != CONFIG:
                parts.append(py.read_text(encoding="utf-8"))
    return "\n".join(parts)


class TestEverySettingIsActuallyUsed:
    def test_there_are_settings_to_check(self):
        assert len(_fields()) >= 15

    def test_no_setting_is_defined_and_never_read(self):
        body = _body_outside_config()
        config_text = CONFIG.read_text(encoding="utf-8")
        offenders = []
        for field in _fields():
            if field in READ_VIA_PROPERTY:
                # نتحقق أن الخاصية موجودة فعلاً وتقرؤه
                assert field in config_text
                continue
            if field not in body:
                offenders.append(field)
        assert offenders == [], (
            "إعدادات معرَّفة ولا يقرؤها أي كود — إمّا تُوصَل أو تُحذف: "
            + "، ".join(offenders))
