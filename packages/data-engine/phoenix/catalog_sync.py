"""مزامنة كتالوج المستودع — لقطة FeniqSync تُدمج مع الحالة السابقة.

مكتبة نقية (قاعدة ذهبية #4): بايتات + DataFrame سابق ⇒ DataFrame جديد وتغيّرات.
لا تعرف من أرسل، ولا أين تُخزَّن النتيجة، ولا قاعدة بيانات.

لماذا ليست جدولاً في Postgres (رغم أن الخطة الأولى قالت «جدول price_history»):
أصناف المستودع وأسعاره بيانات المستخدم لا ميتاداتا — مكانها Parquet في التخزين
(قاعدة ذهبية #2). و«upsert» هنا = لقطة حالة جديدة ثابتة لكل مزامنة لا كتابة فوق
القديمة، فتبقى كل حالة سابقة قابلة للاسترجاع (روح القاعدة #3).

قواعد الدمج:
  • المفتاح: `item_id` (GUID الأمين) — مستقر مهما تغيّر الاسم.
  • صنف جديد ⇒ first_seen_at = وقت المزامنة.
  • سعر تغيّر ⇒ سطر في price_changes (القديم، الجديد، الوقت) — لا يُكتب فوق التاريخ.
  • وضع `full` (الافتراضي): صنف غاب عن اللقطة لا يُحذف أبداً، بل active=False.
    وضع `delta`: الغائب لم يُرسَل أصلاً، فيبقى كما هو.
  • `count` في الرأس لا يطابق عدد الأصناف ⇒ رفض: رفعٌ مبتور على إنترنت ضعيف،
    ولو قُبل لصارت نصف الأصناف «محذوفة» في وضع full.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

import polars as pl

KEY = "item_id"
NUMERIC_FIELDS = ("price", "qty")
TRACKING = ("active", "first_seen_at", "last_seen_at", "price_changed_at")
_TS = pl.Datetime("us", "UTC")


class CatalogPayloadError(ValueError):
    """حمولة مزامنة مرفوضة — الرسالة عربية تُعرض كما هي لصاحب الوكيل."""

    def __init__(self, message_ar: str, code: str = "invalid_catalog"):
        super().__init__(message_ar)
        self.message_ar = message_ar
        self.code = code


@dataclass
class CatalogSnapshot:
    items: pl.DataFrame
    warehouse_id: str | None
    generated_at: datetime | None
    mode: Literal["full", "delta"]
    declared_count: int | None


@dataclass
class MergeStats:
    received: int = 0
    added: int = 0
    price_changed: int = 0
    removed: int = 0
    reactivated: int = 0
    active_total: int = 0


@dataclass
class MergeResult:
    state: pl.DataFrame
    price_changes: pl.DataFrame
    stats: MergeStats = field(default_factory=MergeStats)


# ------------------------------------------------------------------ التحليل

def _decode(raw: bytes) -> Any:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise CatalogPayloadError("الحمولة ليست نصاً بترميز UTF-8.") from None
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise CatalogPayloadError(
            f"الحمولة ليست JSON صالحاً (السطر {e.lineno}، العمود {e.colno}) — "
            "غالباً رفعٌ انقطع في منتصفه.") from None


def _find_items(payload: dict) -> tuple[str, list]:
    if isinstance(payload.get("items"), list):
        return "items", payload["items"]
    lists = [k for k, v in payload.items()
             if isinstance(v, list) and v and all(isinstance(x, dict) for x in v)]
    if len(lists) == 1:
        return lists[0], payload[lists[0]]
    if not lists:
        raise CatalogPayloadError("لا توجد قائمة أصناف في الحمولة (المتوقَّع حقل \"items\").")
    raise CatalogPayloadError(
        f"أكثر من قائمة في الحمولة ({'، '.join(lists)}) — لا نخمّن أيها الأصناف. "
        "أرسلها تحت حقل \"items\" في الرأس.")


def _scalar(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        return v
    if isinstance(v, (list, tuple)):
        return " | ".join(str(x) for x in v if x is not None) or None
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False, sort_keys=True)
    return str(v)


def _number(v: Any, field_name: str, item_id: str) -> float | None:
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if isinstance(v, bool):
        raise CatalogPayloadError(f"«{field_name}» للصنف {item_id} قيمة منطقية لا رقم.")
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise CatalogPayloadError(
            f"«{field_name}» للصنف {item_id} ليس رقماً: {str(v)[:40]!r}. "
            "الأرقام تُرسل بلا فواصل آلاف وبلا رمز عملة.") from None
    if math.isnan(x) or math.isinf(x):
        raise CatalogPayloadError(f"«{field_name}» للصنف {item_id} قيمة غير منتهية.")
    return x


def _parse_ts(v: Any) -> datetime | None:
    if v in (None, ""):
        return None
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        raise CatalogPayloadError(f"«generated_at» ليس تاريخاً بصيغة ISO: {v!r}.") from None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_catalog(raw: bytes) -> CatalogSnapshot:
    payload = _decode(raw)
    if not isinstance(payload, dict):
        raise CatalogPayloadError("الحمولة يجب أن تكون كائناً فيه رأس وقائمة أصناف.")

    list_key, items = _find_items(payload)
    if not items:
        raise CatalogPayloadError("قائمة الأصناف فارغة — لا شيء يُزامَن.", "empty_catalog")

    mode = payload.get("mode", "full")
    if mode not in ("full", "delta"):
        raise CatalogPayloadError(f"«mode» يجب أن يكون full أو delta، لا {mode!r}.")

    declared = payload.get("count")
    if declared is not None:
        try:
            declared = int(declared)
        except (TypeError, ValueError):
            raise CatalogPayloadError(f"«count» ليس عدداً صحيحاً: {declared!r}.") from None
        if declared != len(items):
            raise CatalogPayloadError(
                f"الرأس يعلن {declared} صنفاً ووصل {len(items)} — الحمولة مبتورة. "
                "لم يُقبل شيء؛ أعد الإرسال.", "truncated_catalog")

    rows: list[dict] = []
    seen: dict[str, int] = {}
    dups: list[str] = []
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            raise CatalogPayloadError(f"العنصر رقم {i + 1} في القائمة ليس كائناً.")
        iid = _scalar(it.get(KEY))
        iid = iid.strip() if iid else iid
        if not iid:
            raise CatalogPayloadError(f"العنصر رقم {i + 1} بلا «{KEY}» — لا هوية له.")
        if iid in seen:
            dups.append(iid)
        seen[iid] = i
        row: dict[str, Any] = {}
        for k, v in it.items():
            if k in TRACKING:          # حقول نديرها نحن — لا يكتبها المرسِل
                continue
            row[k] = (_number(v, k, iid) if k in NUMERIC_FIELDS else _scalar(v))
        row[KEY] = iid
        rows.append(row)
    if dups:
        raise CatalogPayloadError(
            f"أصناف مكرّرة بنفس «{KEY}» ({len(dups)}): {'، '.join(dups[:5])}. "
            "لا نختار عنك أيها الصحيح.", "duplicate_items")

    columns = sorted({k for r in rows for k in r})
    schema = {c: (pl.Float64 if c in NUMERIC_FIELDS else pl.Utf8) for c in columns}
    df = pl.DataFrame([{c: r.get(c) for c in columns} for r in rows], schema=schema)

    header_wh = payload.get("warehouse_id")
    return CatalogSnapshot(
        items=df.select([KEY] + [c for c in columns if c != KEY]),
        warehouse_id=None if header_wh is None else str(header_wh),
        generated_at=_parse_ts(payload.get("generated_at")),
        mode=mode,
        declared_count=declared,
    )


# ------------------------------------------------------------------ الدمج

_EMPTY_CHANGES = pl.DataFrame(schema={KEY: pl.Utf8, "old_price": pl.Float64,
                                      "new_price": pl.Float64, "changed_at": _TS})


def merge_catalog(previous: pl.DataFrame | None, snap: CatalogSnapshot,
                  synced_at: datetime) -> MergeResult:
    if synced_at.tzinfo is None:
        raise ValueError("synced_at يجب أن يحمل منطقة زمنية")
    now = pl.lit(synced_at).cast(_TS)
    new = snap.items
    has_price = "price" in new.columns
    stats = MergeStats(received=new.height)

    if previous is None or previous.height == 0:
        state = new.with_columns(
            pl.lit(True).alias("active"), now.alias("first_seen_at"),
            now.alias("last_seen_at"), pl.lit(None, dtype=_TS).alias("price_changed_at"))
        stats.added = new.height
        stats.active_total = new.height
        return MergeResult(state.sort(KEY), _EMPTY_CHANGES.clone(), stats)

    prev_cols = [KEY, "active", "first_seen_at", "price_changed_at"]
    prev_small = previous.select(
        prev_cols + (["price"] if "price" in previous.columns else [])
    ).rename({c: f"__prev_{c}" for c in prev_cols[1:] + ["price"] if c in previous.columns})
    j = new.join(prev_small, on=KEY, how="left")

    is_new = pl.col("__prev_first_seen_at").is_null()
    if has_price and "__prev_price" in j.columns:
        changed = ~is_new & pl.col("price").ne_missing(pl.col("__prev_price"))
    else:
        changed = pl.lit(False)

    j = j.with_columns(is_new.alias("__new"), changed.alias("__changed"))
    stats.added = int(j["__new"].sum())
    stats.price_changed = int(j["__changed"].sum())
    stats.reactivated = int((~j["__new"] & (j["__prev_active"] == False)).sum())  # noqa: E712

    changes = (
        j.filter(pl.col("__changed"))
        .select(pl.col(KEY), pl.col("__prev_price").alias("old_price"),
                pl.col("price").alias("new_price"), now.alias("changed_at"))
        if stats.price_changed else _EMPTY_CHANGES.clone()
    )

    current = j.with_columns(
        pl.lit(True).alias("active"),
        pl.coalesce(pl.col("__prev_first_seen_at"), now).alias("first_seen_at"),
        now.alias("last_seen_at"),
        pl.when(pl.col("__changed")).then(now)
        .otherwise(pl.col("__prev_price_changed_at")).cast(_TS).alias("price_changed_at"),
    ).drop([c for c in j.columns if c.startswith("__")])

    absent = previous.join(new.select(KEY), on=KEY, how="anti")
    if snap.mode == "full":
        stats.removed = int(absent["active"].sum()) if absent.height else 0
        absent = absent.with_columns(pl.lit(False).alias("active"))

    state = pl.concat([current, absent], how="diagonal_relaxed").sort(KEY)
    stats.active_total = int(state["active"].sum())
    return MergeResult(state, changes, stats)
