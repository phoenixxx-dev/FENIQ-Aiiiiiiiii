"""مزامنة الكتالوج (المرحلة ١) — الدمج نقي وحتمي، والرفض برسالة عربية.

كل اختبار يمثّل حادثة محتملة بمستودع حقيقي على إنترنت ضعيف: رفع مبتور،
صنف مكرّر، صنف اختفى، سعر تغيّر مرتين.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import polars as pl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from phoenix.catalog_sync import (CatalogPayloadError, merge_catalog,  # noqa: E402
                                  parse_catalog)

T0 = datetime(2026, 9, 10, 3, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(days=1)
T2 = T0 + timedelta(days=2)


def payload(items, **head) -> bytes:
    body = {"source": "Amin", "warehouse_id": "WH-1", "generated_at": "2026-09-10T06:00:00+03:00",
            "count": len(items), "items": items}
    body.update(head)
    return json.dumps(body, ensure_ascii=False).encode()


def item(iid, price=100.0, qty=5, name="بنادول"):
    return {"item_id": iid, "name_raw": name, "price": price, "qty": qty,
            "aliases": ["panadol", "PANADOL 500"]}


BASE = [item("A", 100), item("B", 200), item("C", 300)]


class TestParsing:
    def test_header_and_items(self):
        s = parse_catalog(payload(BASE))
        assert s.items.height == 3 and s.warehouse_id == "WH-1" and s.mode == "full"
        assert s.generated_at == datetime(2026, 9, 10, 3, 0, tzinfo=timezone.utc)
        assert s.items["price"].dtype == pl.Float64
        assert s.items.filter(pl.col("item_id") == "A")["aliases"][0] == "panadol | PANADOL 500"

    def test_bom_and_numbers_as_text_are_accepted(self):
        raw = "﻿".encode() + payload([item("A", "1250.5", "3")])
        assert parse_catalog(raw).items["price"][0] == 1250.5

    def test_items_under_another_single_key(self):
        raw = json.dumps({"warehouse_id": "WH-1", "catalog": BASE}).encode()
        assert parse_catalog(raw).items.height == 3

    @pytest.mark.parametrize("raw,code", [
        (b'{"items": [{"item_id": "A"', "invalid_catalog"),          # انقطع الرفع
        (b"[]", "invalid_catalog"),
        (payload([]), "empty_catalog"),
    ])
    def test_broken_payloads(self, raw, code):
        with pytest.raises(CatalogPayloadError) as e:
            parse_catalog(raw)
        assert e.value.code == code and e.value.message_ar

    def test_truncated_upload_is_rejected_not_half_applied(self):
        """أخطر حالة: لو قُبلت لصار ثلثا الأصناف «محذوفة» في وضع full."""
        raw = payload(BASE[:1], count=3)
        with pytest.raises(CatalogPayloadError) as e:
            parse_catalog(raw)
        assert e.value.code == "truncated_catalog"
        assert "3" in e.value.message_ar and "1" in e.value.message_ar

    def test_duplicate_ids_are_rejected(self):
        with pytest.raises(CatalogPayloadError) as e:
            parse_catalog(payload([item("A"), item("A", 5)]))
        assert e.value.code == "duplicate_items"

    def test_missing_id_and_bad_price(self):
        with pytest.raises(CatalogPayloadError, match="item_id"):
            parse_catalog(payload([{"name_raw": "x"}]))
        with pytest.raises(CatalogPayloadError, match="ليس رقماً"):
            parse_catalog(payload([item("A", "1,250 ل.س")]))

    def test_sender_cannot_forge_tracking_fields(self):
        forged = dict(item("A"), active=False, first_seen_at="1999-01-01")
        s = parse_catalog(payload([forged]))
        assert "active" not in s.items.columns and "first_seen_at" not in s.items.columns


class TestMerging:
    def test_first_sync(self):
        r = merge_catalog(None, parse_catalog(payload(BASE)), T0)
        assert r.stats.added == 3 and r.stats.active_total == 3
        assert r.price_changes.height == 0
        assert r.state["first_seen_at"].to_list() == [T0] * 3

    def test_same_snapshot_again_changes_nothing_but_last_seen(self):
        s = parse_catalog(payload(BASE))
        first = merge_catalog(None, s, T0).state
        r = merge_catalog(first, s, T1)
        assert (r.stats.added, r.stats.price_changed, r.stats.removed) == (0, 0, 0)
        assert r.state["first_seen_at"].to_list() == [T0] * 3
        assert r.state["last_seen_at"].to_list() == [T1] * 3

    def test_price_change_is_recorded_not_overwritten(self):
        s0 = merge_catalog(None, parse_catalog(payload(BASE)), T0).state
        r1 = merge_catalog(s0, parse_catalog(payload([item("A", 120), item("B", 200), item("C", 300)])), T1)
        r2 = merge_catalog(r1.state, parse_catalog(payload([item("A", 150), item("B", 200), item("C", 300)])), T2)

        assert r1.price_changes.to_dicts() == [
            {"item_id": "A", "old_price": 100.0, "new_price": 120.0, "changed_at": T1}]
        assert r2.price_changes.to_dicts() == [
            {"item_id": "A", "old_price": 120.0, "new_price": 150.0, "changed_at": T2}]
        a = r2.state.filter(pl.col("item_id") == "A").to_dicts()[0]
        assert a["price"] == 150.0 and a["price_changed_at"] == T2 and a["first_seen_at"] == T0
        b = r2.state.filter(pl.col("item_id") == "B").to_dicts()[0]
        assert b["price_changed_at"] is None

    def test_price_becoming_empty_is_a_change(self):
        s0 = merge_catalog(None, parse_catalog(payload(BASE)), T0).state
        r = merge_catalog(s0, parse_catalog(payload([item("A", None), *BASE[1:]])), T1)
        assert r.price_changes.to_dicts()[0]["new_price"] is None

    def test_full_mode_marks_missing_as_inactive_never_deletes(self):
        s0 = merge_catalog(None, parse_catalog(payload(BASE)), T0).state
        r = merge_catalog(s0, parse_catalog(payload(BASE[:2])), T1)
        assert r.stats.removed == 1 and r.stats.active_total == 2
        assert r.state.height == 3, "صنف حُذف من الحالة — ضاع تاريخه"
        c = r.state.filter(pl.col("item_id") == "C").to_dicts()[0]
        assert c["active"] is False and c["last_seen_at"] == T0 and c["price"] == 300.0

    def test_item_that_comes_back_is_reactivated(self):
        s0 = merge_catalog(None, parse_catalog(payload(BASE)), T0).state
        s1 = merge_catalog(s0, parse_catalog(payload(BASE[:2])), T1).state
        r = merge_catalog(s1, parse_catalog(payload(BASE)), T2)
        assert r.stats.reactivated == 1 and r.stats.active_total == 3
        c = r.state.filter(pl.col("item_id") == "C").to_dicts()[0]
        assert c["first_seen_at"] == T0, "صنف عاد فعومل كأنه جديد"

    def test_delta_mode_keeps_absent_items_as_they_were(self):
        s0 = merge_catalog(None, parse_catalog(payload(BASE)), T0).state
        r = merge_catalog(s0, parse_catalog(payload([item("D", 50), item("A", 110)], mode="delta")), T1)
        assert (r.stats.added, r.stats.price_changed, r.stats.removed) == (1, 1, 0)
        assert r.state.height == 4 and r.stats.active_total == 4

    def test_new_fields_from_a_newer_agent_do_not_break_old_state(self):
        s0 = merge_catalog(None, parse_catalog(payload(BASE)), T0).state
        newer = [dict(i, barcode=f"62{n}") for n, i in enumerate(BASE)]
        r = merge_catalog(s0, parse_catalog(payload(newer)), T1)
        assert "barcode" in r.state.columns and r.stats.price_changed == 0

    def test_survives_a_parquet_round_trip(self, tmp_path):
        """الحالة تُحفظ Parquet بين المزامنات — الأنواع يجب أن تعود كما هي."""
        s0 = merge_catalog(None, parse_catalog(payload(BASE)), T0).state
        p = tmp_path / "state.parquet"
        s0.write_parquet(p)
        r = merge_catalog(pl.read_parquet(p), parse_catalog(payload([item("A", 99), *BASE[1:]])), T1)
        assert r.stats.price_changed == 1 and r.stats.added == 0

    def test_naive_timestamp_is_refused(self):
        with pytest.raises(ValueError):
            merge_catalog(None, parse_catalog(payload(BASE)), datetime(2026, 9, 10))
