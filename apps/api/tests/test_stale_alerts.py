"""تنبيهات انقطاع المزامنة عبر تيليغرام (المرحلة ٢).

تيليغرام مستبدَل بناقل وهمي يسجّل الرسائل — الاختبار لا يلمس الشبكة.
"""
from __future__ import annotations

import json
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
from apps.api.src.db.models import Warehouse  # noqa: E402
from apps.api.src.db.session import create_all, dispose_engine, get_sessionmaker  # noqa: E402
from apps.api.src.main import app  # noqa: E402
from apps.api.src.services import telegram  # noqa: E402

SECRET = "test-cron-secret"


class FakeTelegram:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        self.updates: list[dict] = []
        self.fail = False

    async def call(self, method, payload):
        if self.fail:
            raise RuntimeError("telegram down")
        if method == "sendMessage":
            self.sent.append((str(payload["chat_id"]), payload["text"]))
            return {}
        if method == "getUpdates":
            return self.updates
        raise AssertionError(method)


@pytest.fixture
def tg(monkeypatch):
    fake = FakeTelegram()
    s = get_settings()
    monkeypatch.setattr(s, "telegram_bot_token", "123:abc")
    monkeypatch.setattr(s, "telegram_bot_username", "feniq_test_bot")
    monkeypatch.setattr(s, "cron_secret", SECRET)
    monkeypatch.setattr(telegram, "transport", fake)
    return fake


@pytest_asyncio.fixture
async def client():
    await dispose_engine()
    await create_all()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    await dispose_engine()


async def owner(client) -> dict:
    r = await client.post("/auth/register", json={
        "email": f"st_{uuid.uuid4().hex[:10]}@example.com", "password": "StrongPass123"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def link(client, auth, tg, chat="777") -> None:
    url = (await client.post("/v1/alerts/telegram/link", headers=auth)).json()["url"]
    code = url.split("start=")[1]
    tg.updates = [{"update_id": 1, "message": {"text": f"/start {code}", "chat": {"id": int(chat)}}}]
    r = await client.post("/v1/alerts/telegram/verify", headers=auth)
    assert r.json()["linked"] is True


async def warehouse_synced(client, auth, name="مستودع الشمال") -> tuple[str, dict]:
    wh = (await client.post("/v1/warehouses", headers=auth, json={"name": name})).json()
    tok = (await client.post(f"/v1/warehouses/{wh['id']}/tokens", headers=auth, json={})).json()
    agent = {"Authorization": f"Bearer {tok['token']}"}
    body = json.dumps({"count": 1, "items": [{"item_id": "a", "price": 1.0}]}).encode()
    assert (await client.post("/v1/sync/catalog", headers=agent, content=body)).status_code == 202
    return wh["id"], agent


async def age(wh_id: str, hours: float) -> None:
    async with get_sessionmaker()() as db:
        w = await db.get(Warehouse, wh_id)
        w.last_sync_at = datetime.now(timezone.utc) - timedelta(hours=hours)
        await db.commit()


async def check(client):
    return await client.post("/v1/internal/check-stale", headers={"X-Cron-Secret": SECRET})


class TestLinking:
    @pytest.mark.asyncio
    async def test_link_and_confirm(self, client, tg):
        auth = await owner(client)
        assert (await client.get("/v1/alerts/telegram", headers=auth)).json() == {
            "configured": True, "linked": False}
        url = (await client.post("/v1/alerts/telegram/link", headers=auth)).json()["url"]
        assert url.startswith("https://t.me/feniq_test_bot?start=")
        await link(client, auth, tg)
        assert tg.sent and tg.sent[-1][0] == "777"

    @pytest.mark.asyncio
    async def test_verify_without_pressing_start_stays_unlinked(self, client, tg):
        auth = await owner(client)
        await client.post("/v1/alerts/telegram/link", headers=auth)
        tg.updates = [{"update_id": 1, "message": {"text": "/start someone-else", "chat": {"id": 9}}}]
        r = await client.post("/v1/alerts/telegram/verify", headers=auth)
        assert r.json()["linked"] is False, "رمز مستخدم آخر ربط حسابي"

    @pytest.mark.asyncio
    async def test_not_configured_is_a_clear_error(self, client, monkeypatch):
        monkeypatch.setattr(get_settings(), "telegram_bot_token", "")
        auth = await owner(client)
        r = await client.post("/v1/alerts/telegram/link", headers=auth)
        assert r.status_code == 503 and r.json()["message_ar"]


class TestStaleCheck:
    @pytest.mark.asyncio
    async def test_needs_the_secret(self, client, tg):
        assert (await client.post("/v1/internal/check-stale")).status_code == 404
        r = await client.post("/v1/internal/check-stale", headers={"X-Cron-Secret": "wrong"})
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_disabled_when_no_secret_configured(self, client, tg, monkeypatch):
        monkeypatch.setattr(get_settings(), "cron_secret", "")
        r = await client.post("/v1/internal/check-stale", headers={"X-Cron-Secret": ""})
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_alert_once_then_recovery_message(self, client, tg):
        auth = await owner(client)
        await link(client, auth, tg)
        wh, agent = await warehouse_synced(client, auth)
        await age(wh, 30)
        tg.sent.clear()

        assert (await check(client)).status_code == 200
        mine = [t for c, t in tg.sent if c == "777"]
        assert len(mine) == 1 and "مستودع الشمال" in mine[0] and "30" in mine[0]

        await check(client)                      # الساعة التالية: لا تكرار
        assert len([t for c, t in tg.sent if c == "777"]) == 1, "تنبيه مكرّر كل ساعة"

        body = json.dumps({"count": 1, "items": [{"item_id": "a", "price": 2.0}]}).encode()
        assert (await client.post("/v1/sync/catalog", headers=agent, content=body)).status_code == 202
        assert "عادت" in tg.sent[-1][1]
        async with get_sessionmaker()() as db:
            assert (await db.get(Warehouse, wh)).stale_alerted_at is None

    @pytest.mark.asyncio
    async def test_fresh_and_never_synced_are_quiet(self, client, tg):
        auth = await owner(client)
        await link(client, auth, tg)
        wh, _ = await warehouse_synced(client, auth)
        await age(wh, 25)                         # تحت ٢٦ ساعة
        await client.post("/v1/warehouses", headers=auth, json={"name": "جديد لم يرسل"})
        tg.sent.clear()
        await check(client)
        assert not [t for c, t in tg.sent if c == "777"]

    @pytest.mark.asyncio
    async def test_failed_send_is_retried_next_hour(self, client, tg):
        auth = await owner(client)
        await link(client, auth, tg)
        wh, _ = await warehouse_synced(client, auth)
        await age(wh, 40)
        tg.fail = True
        await check(client)
        async with get_sessionmaker()() as db:
            assert (await db.get(Warehouse, wh)).stale_alerted_at is None, \
                "خُتم كأنه أُرسل وهو لم يصل — لن يُعاد أبداً"
        tg.fail = False
        tg.sent.clear()
        await check(client)
        assert [t for c, t in tg.sent if c == "777"]
