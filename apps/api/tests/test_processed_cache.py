"""التخزين المؤقت للملف المعالَج.

القيمة الحقيقية: تقليل نداءات التخزين. الخطر الحقيقي: تقديم نسخة قديمة.
الاختباران أدناه يغطّيان الاثنين معاً — مكسب بلا مخاطرة.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "packages" / "data-engine"))

from apps.api.src.config import get_settings  # noqa: E402
from apps.api.src.storage import cache  # noqa: E402
from apps.api.src.storage.client import get_storage  # noqa: E402

KEY = "tests/cache/sample.parquet"


@pytest.fixture
def stored(tmp_path):
    """يضع ملفاً حقيقياً في التخزين ويُرجع مساره المحلي الأصلي."""
    src = tmp_path / "sample.parquet"
    src.write_bytes(b"PAR1-first-version")
    get_storage().put_file(get_settings().s3_bucket_processed, KEY, src)
    cache.invalidate(KEY)
    yield src
    cache.invalidate(KEY)


def test_first_fetch_downloads_and_returns_content(stored):
    local = cache.fetch_processed(KEY)
    assert local.exists()
    assert local.read_bytes() == b"PAR1-first-version"


def test_second_fetch_does_not_hit_storage_again(stored, monkeypatch):
    """المكسب: السؤال الثاني على نفس الملف لا يُنزّله من جديد."""
    cache.fetch_processed(KEY)

    calls: list[str] = []
    real = get_storage().fetch_to_local

    def counting(bucket, key, dest_dir):
        calls.append(key)
        return real(bucket, key, dest_dir)

    monkeypatch.setattr(get_storage(), "fetch_to_local", counting)
    local = cache.fetch_processed(KEY)

    assert calls == [], "أعاد التنزيل رغم وجود نسخة صالحة"
    assert local.read_bytes() == b"PAR1-first-version"


def test_invalidate_forces_fresh_download(stored, tmp_path):
    """الخطر: بعد إعادة المعالجة يجب ألا تُقدَّم البيانات القديمة أبداً."""
    cache.fetch_processed(KEY)

    updated = tmp_path / "v2.parquet"
    updated.write_bytes(b"PAR1-second-version")
    get_storage().put_file(get_settings().s3_bucket_processed, KEY, updated)

    cache.invalidate(KEY)
    assert cache.fetch_processed(KEY).read_bytes() == b"PAR1-second-version"


def test_cache_survives_repeated_calls(stored):
    paths = {cache.fetch_processed(KEY) for _ in range(5)}
    assert len(paths) == 1, "كل نداء ينتج مساراً مختلفاً — لا فائدة من التخزين المؤقت"
