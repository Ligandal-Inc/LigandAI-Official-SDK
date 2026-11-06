"""Regression: peptide search rows surface Target and deltaG (Tom ask D, item 4).

The live ``GET /api/v1/peptides/search`` select list keys the target as
``gene`` and the DeltaForge energy as ``deltaG`` (enterprise-api-routes.ts
select, verified 2026-11-05), while Peptide's typed fields carry the
``targetGene`` / ``deltaforgeDg`` aliases. Before the normalizer, a real
search row validated with ``target_gene=None`` and ``deltaforge_dg=None``
even though ``ipsae``/``predicted_kd`` from the SAME row populated — which is
what Tom saw as blank Target/deltaG across his 20 TNF designs.

The fix copies the server's value into the typed field ONLY when the
canonical alias is absent. Missing stays missing; nothing is fabricated.
[bd-LIGANDAI_ALPHA_V2-tom-D4]
"""

from __future__ import annotations

from ligandai.types import Peptide

# One row shaped exactly like the server's search select (subset of real keys).
SEARCH_ROW = {
    "peptide_id": 61811,
    "gene": "TNF",
    "sequence": "ACDEFGHIKLMNPQRSTVWY",
    "length": 22,
    "peptideIpsae": 0.31,
    "ipsae": 0.28,
    "iptm": 0.41,
    "ptm": 0.55,
    "plddt": 0.62,
    "deltaG": -8.4,
    "predictedKd": 4.2e-9,
    "isElite": True,
    "foldId": 909,
}


def test_search_row_populates_target_and_dg():
    p = Peptide.model_validate(SEARCH_ROW)
    assert p.target_gene == "TNF"          # was None before the fix
    assert p.deltaforge_dg == -8.4         # was None before the fix
    # The already-working fields keep working.
    assert p.ipsae == 0.28
    assert p.predicted_kd == 4.2e-9
    assert p.fold_id == "909"


def test_canonical_aliases_win_over_row_keys():
    row = dict(SEARCH_ROW, targetGene="TNFRSF1A", deltaforgeDg=-9.9)
    p = Peptide.model_validate(row)
    assert p.target_gene == "TNFRSF1A"
    assert p.deltaforge_dg == -9.9


def test_missing_values_stay_none_never_zero():
    row = {"sequence": "AAAA", "gene": None, "deltaG": None}
    p = Peptide.model_validate(row)
    assert p.target_gene is None
    assert p.deltaforge_dg is None


def test_no_target_gene_field_unaffected():
    p = Peptide.model_validate({"sequence": "CCCC"})
    assert p.target_gene is None and p.deltaforge_dg is None
