"""إعدادات الـAPI — مصدر واحد لكل قيمة قابلة للضبط، تُقرأ من البيئة.

لا قيمة سرية مكتوبة داخل الكود. الافتراضات هنا للتطوير المحلي فقط.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- قاعدة البيانات (ميتاداتا فقط — قاعدة ذهبية #2) ---
    database_url: str = "postgresql+asyncpg://phoenix:phoenix@127.0.0.1:5432/phoenix"

    @field_validator("database_url")
    @classmethod
    def _asyncpg_driver(cls, v: str) -> str:
        # الاستضافات (Render وغيرها) تعطي postgres:// أو postgresql:// —
        # SQLAlchemy غير المتزامن يحتاج اسم المشغّل صراحةً.
        for prefix in ("postgres://", "postgresql://"):
            if v.startswith(prefix):
                return "postgresql+asyncpg://" + v[len(prefix):]
        return v

    # --- Redis / الطابور ---
    redis_url: str = "redis://127.0.0.1:6379/0"

    # --- التخزين ---
    # لو s3_endpoint فاضي ⇒ نستخدم القرص المحلي (تطوير فقط، انظر storage/)
    s3_endpoint: str = ""
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_bucket_raw: str = "phoenix-raw"
    s3_bucket_processed: str = "phoenix-processed"
    local_storage_dir: str = "/tmp/phoenix-storage"

    # --- المصادقة ---
    jwt_secret: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    jwt_access_ttl_minutes: int = 30
    jwt_refresh_ttl_days: int = 14

    # --- طبقة الذكاء ---
    # مغلقة افتراضياً. مغلقة = سلوك اليوم بالضبط: RuleRouter وحده، وأي سؤال
    # غير مفهوم يرجع «لم أفهم» بلا أي استدعاء خارجي ولا تكلفة.
    ai_layer_enabled: bool = False
    gemini_api_key: str = ""
    ai_explain_answers: bool = True
    # سقف الإنفاق الشهري التقديري. 0 = بلا سقف.
    # الخطر رقم 2 في الخطة: «تكلفة الـAI تنفجر». السقف يوقف **التصعيد**
    # للنموذج فقط — المسار الحتمي يبقى يعمل، فلا ينقطع المنتج بل تنقطع
    # التكلفة.
    llm_monthly_budget_usd: float = 5.0
    # كلفة تقديرية لنداء واحد. تقدير معلن لا فاتورة: نراقب به الاتجاه
    # ونوقف قبل المفاجأة، ويُضبط من البيئة حسب المزوّد والسعر الفعلي.
    llm_cost_per_call_usd: float = 0.0004

    @property
    def ai_ready(self) -> bool:
        """الطبقة لا تعمل إلا بمفتاح فعلي — لا تفعيل نصف مكتمل."""
        return self.ai_layer_enabled and bool(self.gemini_api_key)

    # --- تنبيهات انقطاع المزامنة (المرحلة ٢) ---
    # بوت تيليغرام: التوكن يُضبط من لوحة الاستضافة فقط، لا بالكود ولا بالمحادثة.
    telegram_bot_token: str = ""
    telegram_bot_username: str = ""
    # سرّ نقطة الفحص الدوري — المنبّه الخارجي (GitHub Actions) يرسله بترويسة.
    # فارغ = النقطة معطّلة (لا فحص بلا سر).
    cron_secret: str = ""
    # مزامنة يومية + هامش ساعتين (قرار عمار).
    stale_after_hours: int = 26

    @property
    def telegram_ready(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_bot_username)

    # --- الحدود ---
    max_file_size_mb: int = 100
    # سجلات رفع لم يُستكمل (طُلب رابط ولم يصل الملف) تُنظَّف بعد هذه المدة.
    abandoned_upload_hours: int = 24
    # سياسة الاحتفاظ (الخطة §9.4). 0 = بلا حذف تلقائي، وهو الافتراضي عمداً:
    # حذف بيانات المستخدم تلقائياً قرارٌ لا يجوز أن يقع بالسهو (انظر ق-14).
    retention_days: int = 0
    query_timeout_seconds: int = 30

    # --- تحديد المحاولات (عبر Redis — يعمل مع أكثر من نسخة من الـAPI) ---
    rate_limit_login_per_minute: int = 5
    rate_limit_general_per_minute: int = 100
    # السؤال أغلى مسار: DuckDB + احتمال نداء نموذج. حدّه أضيق (الخطة §9.4)
    rate_limit_ask_per_minute: int = 20

    # --- التشغيل ---
    # إنشاء الجداول تلقائياً عند الإقلاع: مناسب للتطوير والاختبار فقط.
    # في الإنتاج تُطبَّق الهجرات: alembic upgrade head
    auto_create_tables: bool = True

    # النطاقات المسموح لها بمناداة الـAPI من المتصفح (مفصولة بفاصلة).
    # ⚠️ localhost و127.0.0.1 نطاقان مختلفان عند المتصفح — لا بد من ذكرهما
    # معاً، وإلا فشل الطلب بخطأ CORS غامض (عيب حقيقي اكتشفه اختبار المتصفح).
    cors_origins: str = (
        "http://localhost:3000,http://127.0.0.1:3000,"
        "http://localhost:3010,http://127.0.0.1:3010"
    )

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def max_file_size_bytes(self) -> int:
        return self.max_file_size_mb * 1024 * 1024

    @property
    def uses_s3(self) -> bool:
        return bool(self.s3_endpoint)


@lru_cache
def get_settings() -> Settings:
    return Settings()
