"""Regression: ExpressionProfile must accept the LIVE gene-expression shape.

Tom's SDK discovery report (2026-11-05, D-ask item 3): the live
``GET /api/transcriptomics/gene-expression/<gene>`` returns
``{gene, chartData: [{tissue, mean, ...}], maxExpression, minExpression}``
and ``organSystems`` as a LIST of system names, while the model documented a
``tissues`` dict and an ``organSystems`` dict — validation failed on live
payloads. The normalizer builds ``tissues`` from ``chartData`` rows that carry
a real numeric value and never fabricates one; documented shapes keep working.
[bd-LIGANDAI_ALPHA_V2-tom-D3]
"""

from __future__ import annotations

import pytest

from ligandai.types import ExpressionProfile

LIVE_SHAPE = {
    "gene": "CD177",
    "chartData": [
        {"tissue": "whole_blood", "mean": 12.4, "max": 30.1},
        {"tissue": "bone_marrow", "mean": 1.1},
        {"tissue": "liver", "mean": 0.0},
    ],
    "maxExpression": 12.4,
    "minExpression": 0.0,
    "organSystems": ["Immature", "Blood"],
}


def test_live_chartdata_shape_parses_into_tissues():
    p = ExpressionProfile.model_validate(LIVE_SHAPE)
    assert p.gene == "CD177"
    assert p.tissues == {"whole_blood": 12.4, "bone_marrow": 1.1, "liver": 0.0}
    # A real zero is preserved, not dropped (no imputation either way).
    assert p.tissues["liver"] == 0.0
    # organSystems arrives as a plain list on the live endpoint — accepted.
    assert p.organ_systems == ["Immature", "Blood"]


def test_documented_dict_shape_still_parses():
    doc = {
        "gene": "TNF",
        "tissues": {"whole_blood": 3.2, "spleen": 1.9},
        "organSystems": {"Immune": 3.2},
    }
    p = ExpressionProfile.model_validate(doc)
    assert p.tissues == {"whole_blood": 3.2, "spleen": 1.9}
    assert p.organ_systems == {"Immune": 3.2}


def test_documented_tissues_win_over_chartdata():
    both = dict(LIVE_SHAPE, tissues={"spleen": 5.0})
    p = ExpressionProfile.model_validate(both)
    assert p.tissues == {"spleen": 5.0}


def test_chartdata_rows_without_a_number_are_skipped_not_imputed():
    sparse = {
        "gene": "GAPDH",
        "chartData": [
            {"tissue": "muscle"},                    # no numeric field at all
            {"tissue": "lung", "mean": None},        # explicit null
            {"tissue": "heart", "mean": "n/a"},      # non-numeric
            {"tissue": "kidney", "tpm": 8.5},        # alternate key works
        ],
    }
    p = ExpressionProfile.model_validate(sparse)
    assert p.tissues == {"kidney": 8.5}
    assert "muscle" not in p.tissues and "lung" not in p.tissues


def test_all_rows_unusable_leaves_tissues_none():
    # A payload where NO row carries a real number must not synthesize a
    # tissues dict — MISSING stays MISSING, never zero-filled.
    sparse = {"gene": "X", "chartData": [{"tissue": "a"}, {"tissue": "b"}]}
    p = ExpressionProfile.model_validate(sparse)
    assert p.tissues is None


def test_empty_gene_payload_still_parses():
    # discovery.gene_expression() substitutes {"gene": gene} on empty bodies.
    p = ExpressionProfile.model_validate({"gene": "FOO"})
    assert p.tissues is None and p.organ_systems is None
