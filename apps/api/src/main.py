"""نقطة دخول الـAPI."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from phoenix.analytics import QueryTimeout

from .config import get_settings
from .db.session import create_all, dispose_engine
from .security.rate_limit import limit_general
from .observability import (REQUEST_ID_HEADER, RequestContextMiddleware,
                            configure_logging, current_request_id)
from .routers import alerts, analysis, ask, auth, datasets, health, jobs, sync, uploads

configure_logging()

log = logging.getLogger("phoenix.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # الإنتاج يطبّق الهجرات صراحةً: alembic upgrade head
    # الإنشاء التلقائي للتطوير والاختبار فقط، ويُطفأ بـAUTO_CREATE_TABLES=false
    if get_settings().auto_create_tables:
        await create_all()
    yield
    await dispose_engine()


app = FastAPI(
    title="Phoenix AI API",
    version="0.1.0",
    description="واجهة فينيق: رفع ملف ← معالجة بخط أنابيب المحرك ← سؤال بالعربية بإجابة لها دليل.",
    lifespan=lifespan,
)

# الترتيب مقصود: middleware معرّف الطلب يُسجَّل أولاً فيلفّ كل ما بعده،
# ويصل معرّفه حتى إلى الطلبات التي يرفضها CORS.
app.add_middleware(RequestContextMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition", "Retry-After", REQUEST_ID_HEADER],
)

app.include_router(health.router)
app.include_router(auth.router)
# الحد العام يُطبَّق على كل ما يخصّ بيانات المستخدم. لا يُطبَّق على /health
# (يُنادى كل ثوانٍ من المراقبة) ولا على /auth (له حدّه الأضيق).
_general = [Depends(limit_general)]
app.include_router(uploads.router, dependencies=_general)   # قبل datasets: /datasets/upload-url أخص من /datasets/{id}
app.include_router(datasets.router, dependencies=_general)
app.include_router(jobs.router, dependencies=_general)
app.include_router(analysis.router, dependencies=_general)
app.include_router(ask.router, dependencies=_general)
# المزامنة لها حدّها الخاص لكل مفتاح جهاز (داخل المسار) إضافةً للحد العام.
app.include_router(sync.router, dependencies=_general)
app.include_router(alerts.router, dependencies=_general)


@app.exception_handler(StarletteHTTPException)
async def http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    """كل خطأ بنفس الشكل: {error_code, message_ar, details}."""
    detail = exc.detail
    if isinstance(detail, dict) and "message_ar" in detail:
        body = {"error_code": detail.get("error_code", "error"),
                "message_ar": detail["message_ar"],
                "details": detail.get("details")}
    else:
        body = {"error_code": "http_error", "message_ar": str(detail), "details": None}
    # معرّف الطلب داخل الجسم: المستخدم يقرأه ويرسله لنا، فنصل للسطر الدقيق
    # في السجل بدل التخمين.
    body["request_id"] = current_request_id()
    # ترويسات الاستثناء تُمرَّر كما هي — Retry-After مثلاً يخبر العميل متى يعيد
    # المحاولة، وإسقاطها يجعل رسالة «جرّب بعد كذا ثانية» بلا قيمة تقنية.
    return JSONResponse(status_code=exc.status_code, content=body,
                        headers=getattr(exc, "headers", None))


@app.exception_handler(RequestValidationError)
async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"error_code": "validation_error",
                 "message_ar": "البيانات المُرسلة غير صالحة.",
                 "details": jsonable_encoder(exc.errors()),
                 "request_id": current_request_id()},
    )


@app.exception_handler(QueryTimeout)
async def query_timeout(_: Request, exc: QueryTimeout) -> JSONResponse:
    """استعلام أوقفناه بالمهلة — رسالة المحرك نفسها تقول للمستخدم ما العمل."""
    log.warning("استعلام تجاوز مهلته: %s", exc)
    return JSONResponse(
        status_code=status.HTTP_408_REQUEST_TIMEOUT,
        content={"error_code": "query_timeout", "message_ar": str(exc),
                 "details": None, "request_id": current_request_id()},
    )


@app.exception_handler(FileNotFoundError)
async def missing_object(_: Request, exc: FileNotFoundError) -> JSONResponse:
    """كائن مفقود من التخزين — يحدث لو حُذف الملف من تحت النظام.

    كان يخرج كاستثناء غير ملتقَط (500 بلا رسالة عربية). اكتشفه حارس القاعدة
    الذهبية #2 عندما حذف الملف المعالَج عمداً.
    """
    log.warning("كائن مفقود من التخزين: %s", exc)
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"error_code": "storage_object_missing",
                 "message_ar": "تعذّر الوصول إلى بيانات هذا الملف. جرّب رفعه من جديد.",
                 "details": None, "request_id": current_request_id()},
    )


@app.exception_handler(Exception)
async def unexpected_error(_: Request, exc: Exception) -> JSONResponse:
    """آخر خط دفاع: لا يخرج من الـAPI ردٌّ بلا شكل موحّد ورسالة عربية.

    التفصيل التقني يبقى في السجل مربوطاً بـrequest_id — لا يُعرض للمستخدم.
    """
    log.exception("خطأ غير متوقَّع")
    return JSONResponse(
        status_code=500,
        content={"error_code": "internal_error",
                 "message_ar": "صار خطأ غير متوقَّع. جرّب مرة ثانية.",
                 "details": None, "request_id": current_request_id()},
    )


@app.get("/")
async def root() -> dict:
    s = get_settings()
    return {"name": "Phoenix AI API", "version": app.version,
            "storage_backend": "s3" if s.uses_s3 else "local-dev",
            "docs": "/docs"}
