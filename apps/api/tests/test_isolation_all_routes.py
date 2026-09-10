"""عزل البيانات على **كل** مسار — بالتعداد لا بقائمة يدوية.

قائمة مكتوبة بالـيد تشيخ: كل مسار جديد يُضاف لاحقاً يبقى خارج الحارس حتى
ينتبه أحد. هنا نقرأ مسارات التطبيق من OpenAPI نفسه، فالمسار الجديد يدخل
الفحص تلقائياً يوم إضافته.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "packages" / "data-engine"))

from apps.api.src.db.session import (create_all, dispose_engine,  # noqa: E402
                                     get_sessionmaker)
from apps.api.src.main import app  # noqa: E402
from apps.api.src.services.dataset_service import run_processing_job  # noqa: E402

FIXTURE = ROOT / "packages" / "data-engine" / "fixtures" / "sales_ar_messy.xlsx"

# أجسام الطلبات للمسارات التي تحتاجها. بلا جسم صالح يرد الخادم 422 قبل أن
# يصل إلى فحص الملكية، فيمرّ الاختبار بلا أن يفحص شيئاً.
BODIES: dict[tuple[str, str], dict] = {
    ("post", "/datasets/{dataset_id}/ask"): {"question": "كم إجمالي المبيعات؟"},
    ("patch", "/datasets/{dataset_id}/schema"):
        {"columns": [{"column_name": "العميل", "concept": "customer"}]},
    ("patch", "/datasets/{dataset_id}/cleaning"): {"disabled": []},
    ("post", "/datasets/{dataset_id}/complete"): {},
}


@pytest_asyncio.fixture
async def client():
    await dispose_engine()
    await create_all()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    await dispose_engine()


async def _user(client) -> dict:
    email = f"iso_{uuid.uuid4().hex[:10]}@example.com"
    r = await client.post("/auth/register", json={"email": email, "password": "StrongPass123"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _owned_paths() -> list[tuple[str, str]]:
    out = []
    for path, methods in app.openapi()["paths"].items():
        if "{dataset_id}" not in path and "{job_id}" not in path:
            continue
        for method in methods:
            if method in ("get", "post", "patch", "delete", "put"):
                out.append((method, path))
    return sorted(out)


class TestEveryOwnedRouteIsIsolated:
    @pytest.mark.asyncio
    async def test_no_route_leaks_another_users_data(self, client):
        owner = await _user(client)
        with FIXTURE.open("rb") as fh:
            r = await client.post("/datasets", headers=owner,
                                  files={"file": (FIXTURE.name, fh)})
        ds_id, job_id = r.json()["dataset_id"], r.json()["job_id"]
        async with get_sessionmaker()() as db:
            await run_processing_job(db, job_id)

        intruder = await _user(client)
        checked, leaks, needs_body = [], [], []

        for method, template in _owned_paths():
            url = template.replace("{dataset_id}", ds_id).replace("{job_id}", job_id)
            body = BODIES.get((method, template))
            resp = await client.request(method.upper(), url, headers=intruder,
                                        json=body if body is not None else None)
            if resp.status_code == 422:
                needs_body.append(f"{method.upper()} {template}")
                continue
            checked.append(f"{method.upper()} {template}")
            if resp.status_code != 404:
                leaks.append(f"{method.upper()} {template} → {resp.status_code}")

        assert not needs_body, (
            "مسارات لم تُفحص لأنها تحتاج جسم طلب — أضفها إلى BODIES: "
            + "، ".join(needs_body))
        assert leaks == [], f"تسريب بيانات مستخدم آخر: {leaks}"
        assert len(checked) >= 12, f"عدد المسارات المفحوصة قليل ({len(checked)})"

    @pytest.mark.asyncio
    async def test_the_owner_still_gets_through(self, client):
        """الحد المقابل: حارس يمنع الجميع ليس عزلاً بل تعطيلاً."""
        owner = await _user(client)
        with FIXTURE.open("rb") as fh:
            r = await client.post("/datasets", headers=owner,
                                  files={"file": (FIXTURE.name, fh)})
        ds_id = r.json()["dataset_id"]
        async with get_sessionmaker()() as db:
            await run_processing_job(db, r.json()["job_id"])

        for path in (f"/datasets/{ds_id}", f"/datasets/{ds_id}/schema",
                     f"/datasets/{ds_id}/profile", f"/datasets/{ds_id}/dashboard",
                     f"/datasets/{ds_id}/rows", f"/datasets/{ds_id}/suggestions"):
            resp = await client.get(path, headers=owner)
            assert resp.status_code == 200, f"{path} حجب المالك نفسه: {resp.text[:120]}"
