"""سقف الإنفاق الشهري على النموذج — الخطر رقم 2 في الخطة.

القاعدة المُختبَرة: السقف يوقف **التصعيد** لا المنتج. المستخدم يبقى يحصل
على أرقامه من المسار الحتمي، ويُخبَر بوضوح أن الصياغة الذكية متوقفة.
"""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "packages" / "data-engine"))

from apps.api.src.config import get_settings  # noqa: E402
from apps.api.src.db.models import Conversation, Message  # noqa: E402
from apps.api.src.db.session import (create_all, dispose_engine,  # noqa: E402
                                     get_sessionmaker)
from apps.api.src.main import app  # noqa: E402
from apps.api.src.services.ai_budget import (cost_of,  # noqa: E402
                                             spent_this_month, within_budget)
from apps.api.src.services.dataset_service import run_processing_job  # noqa: E402

FIXTURE = ROOT / "packages" / "data-engine" / "fixtures" / "sales_ar_messy.xlsx"


@pytest_asyncio.fixture
async def client():
    await dispose_engine()
    await create_all()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    await dispose_engine()


@pytest_asyncio.fixture
async def ready(client):
    email = f"b_{uuid.uuid4().hex[:10]}@example.com"
    r = await client.post("/auth/register", json={"email": email, "password": "StrongPass123"})
    auth = {"Authorization": f"Bearer {r.json()['access_token']}"}
    with FIXTURE.open("rb") as fh:
        r = await client.post("/datasets", headers=auth, files={"file": (FIXTURE.name, fh)})
    body = r.json()
    async with get_sessionmaker()() as db:
        await run_processing_job(db, body["job_id"])
    return client, auth, body["dataset_id"]


async def _seed_spend(dataset_id: str, user_id: str | None, amount: float,
                      when: datetime | None = None) -> None:
    """يزرع رسالة بتكلفة محددة لمحاكاة إنفاق سابق."""
    async with get_sessionmaker()() as db:
        conv = Conversation(dataset_id=dataset_id, user_id=user_id, title="بذرة")
        db.add(conv)
        await db.flush()
        msg = Message(conversation_id=conv.id, role="assistant",
                      content="بذرة", cost_usd=amount)
        if when is not None:
            msg.created_at = when
        db.add(msg)
        await db.commit()


class TestCostEstimate:
    def test_cost_scales_with_calls(self):
        assert cost_of(0) == 0
        assert cost_of(2) == pytest.approx(2 * get_settings().llm_cost_per_call_usd)

    @pytest.mark.asyncio
    async def test_only_this_month_counts(self, ready):
        """إنفاق الشهر الماضي لا يخصم من ميزانية هذا الشهر."""
        client, auth, ds_id = ready
        me = (await client.get("/auth/me", headers=auth)).json()
        last_month = datetime.now(timezone.utc) - timedelta(days=45)

        async with get_sessionmaker()() as db:
            before = await spent_this_month(db)
        await _seed_spend(ds_id, me["id"], 100.0, when=last_month)
        async with get_sessionmaker()() as db:
            after = await spent_this_month(db)
        assert after == pytest.approx(before), "أُدخل إنفاق قديم في حساب هذا الشهر"


class TestBudgetStopsEscalationNotTheProduct:
    @pytest.mark.asyncio
    async def test_questions_still_answered_after_the_cap(self, ready, monkeypatch):
        """الأهم: تجاوز السقف لا يكسر المنتج — الأرقام تبقى تصل."""
        client, auth, ds_id = ready
        me = (await client.get("/auth/me", headers=auth)).json()
        await _seed_spend(ds_id, me["id"], get_settings().llm_monthly_budget_usd + 1)

        async with get_sessionmaker()() as db:
            allowed, spent = await within_budget(db)
        assert allowed is False
        assert spent > get_settings().llm_monthly_budget_usd

        r = await client.post(f"/datasets/{ds_id}/ask", headers=auth,
                              json={"question": "كم إجمالي المبيعات؟"})
        assert r.status_code == 200
        body = r.json()
        assert body["metrics"], "توقّف الذكاء أوقف الإجابة الحتمية أيضاً"
        assert body["evidence"], "رقم بلا دليل (قاعدة ذهبية #1)"

    @pytest.mark.asyncio
    async def test_zero_budget_means_no_cap(self, ready, monkeypatch):
        """0 = بلا سقف، لمن يريد إدارة التكلفة خارج المنتج."""
        client, auth, ds_id = ready
        me = (await client.get("/auth/me", headers=auth)).json()
        await _seed_spend(ds_id, me["id"], 999.0)

        # get_settings مُخبّأة، فتعديل الكائن نفسه يصل إلى كل من يناديها.
        # (استدعاء cache_clear هنا كان يُنشئ كائناً جديداً ويُلغي التعديل.)
        monkeypatch.setattr(get_settings(), "llm_monthly_budget_usd", 0.0)

        async with get_sessionmaker()() as db:
            allowed, _ = await within_budget(db)
        assert allowed is True

    @pytest.mark.asyncio
    async def test_a_normal_answer_records_its_cost(self, ready):
        """كل رسالة تحمل كلفتها — بلا ذلك لا يمكن حساب مجموع الشهر أصلاً."""
        client, auth, ds_id = ready
        r = await client.post(f"/datasets/{ds_id}/ask", headers=auth,
                              json={"question": "كم إجمالي المبيعات؟"})
        assert r.status_code == 200

        async with get_sessionmaker()() as db:
            from sqlalchemy import select
            rows = (await db.execute(
                select(Message).join(Conversation)
                .where(Conversation.dataset_id == ds_id,
                       Message.role == "assistant"))).scalars().all()
        assert rows, "لم تُحفَظ رسالة الإجابة"
        # الذكاء مغلق في الاختبارات ⇒ صفر نداء ⇒ صفر تكلفة، والحقل موجود
        assert all(m.cost_usd == 0.0 for m in rows)
