"""تصحيح المستخدم داخل المحرك — apply_overrides.

قاعدة الخطة: قرار المستخدم يعلو على استنتاج المحرك. المُختبَر هنا أن التطبيق
لا يكتفي بتغيير الاسم، بل يضبط الدور والوحدة والثقة وطريقة الكشف معاً — وأن
مفهوماً لا يعرفه القاموس يُرفض بدل أن يُخزَّن ويُكسر لاحقاً.
"""
from __future__ import annotations

import pytest

from phoenix.models import SemanticColumn, SemanticSchema
from phoenix.semantic import (UnknownConceptError, apply_overrides,
                              available_concepts)


def _schema() -> SemanticSchema:
    return SemanticSchema(columns=[
        SemanticColumn(column_name="المبلغ ص", concept=None, role="unknown",
                       confidence=0.4, detection_method="none"),
        SemanticColumn(column_name="التاريخ", concept="date", role="time",
                       confidence=0.95, detection_method="dictionary"),
    ])


def test_override_sets_role_and_unit_from_the_dictionary():
    """المستخدم يحدّد المفهوم فقط — الدور والوحدة يأتيان من القاموس."""
    out = apply_overrides(_schema(), {"المبلغ ص": {"concept": "total_amount"}})
    col = next(c for c in out.columns if c.column_name == "المبلغ ص")
    assert col.concept == "total_amount"
    assert col.role == "measure"
    assert col.unit == "currency"
    assert col.confidence == 1.0
    assert col.detection_method == "user"
    assert col.user_overridden is True
    assert col.needs_review is False


def test_untouched_columns_stay_exactly_as_they_were():
    before = _schema()
    out = apply_overrides(before, {"المبلغ ص": {"concept": "total_amount"}})
    kept = next(c for c in out.columns if c.column_name == "التاريخ")
    assert kept == next(c for c in before.columns if c.column_name == "التاريخ")


def test_user_can_declare_a_column_meaningless():
    out = apply_overrides(_schema(), {"التاريخ": {"concept": None}})
    col = next(c for c in out.columns if c.column_name == "التاريخ")
    assert col.concept is None
    assert col.role == "unknown"
    assert col.user_overridden is True


def test_explicit_role_wins_over_the_dictionary_default():
    out = apply_overrides(_schema(),
                          {"المبلغ ص": {"concept": "total_amount", "role": "dimension"}})
    assert next(c for c in out.columns if c.column_name == "المبلغ ص").role == "dimension"


def test_unknown_concept_is_refused_not_stored():
    with pytest.raises(UnknownConceptError):
        apply_overrides(_schema(), {"المبلغ ص": {"concept": "مفهوم_مخترع"}})


def test_empty_overrides_return_the_same_schema():
    before = _schema()
    assert apply_overrides(before, {}) is before


def test_every_offered_concept_is_actually_applicable():
    """حارس: أي مفهوم تعرضه الواجهة يجب أن يُقبل فعلاً — بلا خيارات ميتة."""
    for c in available_concepts():
        out = apply_overrides(_schema(), {"المبلغ ص": {"concept": c["concept"]}})
        assert next(x for x in out.columns
                    if x.column_name == "المبلغ ص").concept == c["concept"]
