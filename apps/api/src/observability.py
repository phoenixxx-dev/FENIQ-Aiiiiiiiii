"""تسجيل منظَّم (JSON) مع معرّف طلب — الخطة §12.3.

لماذا يهم؟ لأن أول ما يقوله المستخدم عند العطل هو «ما زبط». بلا معرّف طلب
يربط رسالته بسطر في السجل، يصير التشخيص تخميناً.

كل استجابة تحمل `X-Request-ID`، وكل خطأ يُرجع `request_id` داخل جسمه، وكل
سطر سجل يحمل نفس المعرّف — فيغلق الخيط من شكوى المستخدم إلى السطر الدقيق.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

REQUEST_ID_HEADER = "X-Request-ID"
_request_id: ContextVar[str] = ContextVar("request_id", default="-")

# المسارات التي لا يفيد تسجيلها: فحص الصحة يُنادى كل ثوانٍ فيغرق السجل
_QUIET_PATHS = {"/health", "/docs", "/openapi.json", "/favicon.ico"}


def current_request_id() -> str:
    return _request_id.get()


class JsonFormatter(logging.Formatter):
    """سطر واحد JSON لكل حدث — قابل للبحث بأي أداة سجلات."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "request_id": getattr(record, "request_id", current_request_id()),
            "msg": record.getMessage(),
        }
        for key, value in getattr(record, "extra_fields", {}).items():
            payload[key] = value
        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    # سجلات وصول uvicorn مكرّرة: عندنا سجل أغنى منها بمعرّف الطلب والمدة
    logging.getLogger("uvicorn.access").disabled = True


log = logging.getLogger("phoenix.request")


class RequestContextMiddleware(BaseHTTPMiddleware):
    """يولّد/يمرّر معرّف الطلب ويسجّل نتيجته ومدّته."""

    async def dispatch(self, request: Request, call_next):
        rid = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex[:16]
        token = _request_id.set(rid)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # الاستثناء غير المتوقَّع يُسجَّل هنا بمعرّفه ثم يُترك لمعالج
            # الأخطاء ليُرجع شكلاً موحّداً للمستخدم
            log.exception("request failed", extra={"extra_fields": {
                "method": request.method, "path": request.url.path,
                "duration_ms": round((time.perf_counter() - started) * 1000),
            }})
            _request_id.reset(token)
            raise

        duration = round((time.perf_counter() - started) * 1000)
        response.headers[REQUEST_ID_HEADER] = rid
        if request.url.path not in _QUIET_PATHS:
            log.info("request", extra={"extra_fields": {
                "method": request.method, "path": request.url.path,
                "status": response.status_code, "duration_ms": duration,
            }})
        _request_id.reset(token)
        return response
