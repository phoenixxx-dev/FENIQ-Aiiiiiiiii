"""السجل المنظَّم ومعرّف الطلب — الخطة §12.3.

القيمة: عندما يقول المستخدم «ما زبط»، نحتاج خيطاً يربط شكواه بسطر السجل.
المُختبَر هنا أن الخيط موجود فعلاً وغير مقطوع.
"""
from __future__ import annotations

import json
import logging
import sys
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "packages" / "data-engine"))

from apps.api.src.db.session import create_all, dispose_engine  # noqa: E402
from apps.api.src.main import app  # noqa: E402
from apps.api.src.observability import (REQUEST_ID_HEADER,  # noqa: E402
                                        JsonFormatter)


@pytest_asyncio.fixture
async def client():
    await dispose_engine()
    await create_all()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    await dispose_engine()


class TestRequestId:
    @pytest.mark.asyncio
    async def test_every_response_carries_a_request_id(self, client):
        r = await client.get("/health")
        assert r.headers.get(REQUEST_ID_HEADER)

    @pytest.mark.asyncio
    async def test_error_body_carries_the_same_id_as_the_header(self, client):
        """بلا هذا التطابق لا يستطيع المستخدم أن يخبرنا أي طلب فشل."""
        r = await client.get("/datasets")
        assert r.status_code == 401
        assert r.json()["request_id"] == r.headers[REQUEST_ID_HEADER]

    @pytest.mark.asyncio
    async def test_incoming_id_is_preserved_for_tracing_across_services(self, client):
        mine = uuid.uuid4().hex[:16]
        r = await client.get("/health", headers={REQUEST_ID_HEADER: mine})
        assert r.headers[REQUEST_ID_HEADER] == mine

    @pytest.mark.asyncio
    async def test_two_requests_get_different_ids(self, client):
        a = (await client.get("/health")).headers[REQUEST_ID_HEADER]
        b = (await client.get("/health")).headers[REQUEST_ID_HEADER]
        assert a != b

    @pytest.mark.asyncio
    async def test_validation_error_body_is_json_serialisable(self, client):
        """خطأ التحقق يحمل تفاصيل قد تتضمن استثناءات — يجب أن تُسلسَل بأمان."""
        r = await client.post("/auth/register", json={"email": "not-an-email",
                                                      "password": "x"})
        assert r.status_code == 422
        assert r.json()["error_code"] == "validation_error"
        assert r.json()["request_id"]


class TestJsonFormatter:
    def test_line_is_valid_json_with_readable_arabic(self):
        """العربية تُكتب حرفاً لا \\uXXXX — سجل غير مقروء سجل لا يُقرأ."""
        record = logging.LogRecord("phoenix.test", logging.INFO, __file__, 1,
                                   "تعذّرت المعالجة", None, None)
        record.extra_fields = {"dataset_id": "abc", "duration_ms": 12}
        line = JsonFormatter().format(record)
        parsed = json.loads(line)
        assert parsed["msg"] == "تعذّرت المعالجة"
        assert parsed["dataset_id"] == "abc"
        assert parsed["level"] == "info"
        assert "\\u" not in line

    def test_exception_is_included(self):
        try:
            raise ValueError("boom")
        except ValueError:
            record = logging.LogRecord("phoenix.test", logging.ERROR, __file__, 1,
                                       "failed", None, sys.exc_info())
        parsed = json.loads(JsonFormatter().format(record))
        assert "ValueError: boom" in parsed["error"]
