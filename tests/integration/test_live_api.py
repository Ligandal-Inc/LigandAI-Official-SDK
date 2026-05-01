# Copyright © 2025 Ligandal, Inc. All rights reserved.
"""Integration tests against a live LIGANDAI server.

Skipped unless ``LIGANDAI_TEST_API_KEY`` is set. To run against the dev server:

    LIGANDAI_TEST_API_KEY=lgai_pro_... \
    LIGANDAI_TEST_BASE_URL=http://localhost:5050 \
    pytest tests/integration/ -v
"""

from __future__ import annotations

import os

import pytest

from ligandai import LigandAI

pytestmark = pytest.mark.skipif(
    "LIGANDAI_TEST_API_KEY" not in os.environ,
    reason="LIGANDAI_TEST_API_KEY not set",
)


@pytest.fixture
def live_client() -> LigandAI:
    return LigandAI(
        api_key=os.environ["LIGANDAI_TEST_API_KEY"],
        base_url=os.environ.get("LIGANDAI_TEST_BASE_URL", "http://localhost:5050"),
        max_retries=2,
    )


@pytest.mark.integration
def test_health(live_client: LigandAI) -> None:
    res = live_client.health()
    assert isinstance(res, dict)


@pytest.mark.integration
def test_me(live_client: LigandAI) -> None:
    user = live_client.account.me()
    assert user.id


@pytest.mark.integration
def test_credits(live_client: LigandAI) -> None:
    c = live_client.account.credits()
    assert c.balance >= 0


@pytest.mark.integration
def test_search_receptordb(live_client: LigandAI) -> None:
    hits = live_client.receptors.search("EGFR", limit=3)
    assert all(h.gene or h.complex_name for h in hits)


@pytest.mark.integration
def test_tier_detected(live_client: LigandAI) -> None:
    assert live_client.tier in ("free", "academia", "pro", "enterprise", "superadmin")


# ----------------------------------------------------------------------------
# v0.2.0 paid-only /api/v1/peptides/* surface (LIGANDAI_ALPHA_V2-afspr)
# ----------------------------------------------------------------------------
# These require a paid (pro+) test key. Skipped automatically on free keys
# so a CI run that only has a free fixture key won't fail.

_paid_only = pytest.mark.skipif(
    os.environ.get("LIGANDAI_TEST_TIER", "").lower() not in ("pro", "enterprise", "superadmin"),
    reason="LIGANDAI_TEST_TIER not in (pro, enterprise, superadmin) — paid-only test",
)


@pytest.mark.integration
@_paid_only
def test_peptides_by_gene_returns_gene_summaries(live_client: LigandAI) -> None:
    rows = live_client.peptides.by_gene(limit=10)
    # Don't assert non-empty — a fresh paid account may have no folds yet.
    # But every row that does come back must validate as GeneSummary.
    for r in rows:
        assert isinstance(r.gene, str) and r.gene == r.gene.upper()
        assert r.folded_count >= 0
        assert 0 <= r.elite_count <= r.folded_count
        assert 0 <= r.great_plus_count <= r.folded_count
        assert r.session_count >= 0
        assert r.program_count >= 0


@pytest.mark.integration
@_paid_only
def test_peptides_list_for_known_gene(live_client: LigandAI) -> None:
    # Pick a gene from by_gene; if no folds exist, soft-skip.
    rows = live_client.peptides.by_gene(limit=5)
    if not rows:
        pytest.skip("Test account has no folds yet; nothing to list.")
    gene = rows[0].gene
    peptides = live_client.peptides.list(gene, limit=5)
    for p in peptides:
        assert p.sequence and isinstance(p.sequence, str)


@pytest.mark.integration
@_paid_only
def test_peptides_get_thin_response(live_client: LigandAI) -> None:
    rows = live_client.peptides.by_gene(limit=5)
    if not rows:
        pytest.skip("Test account has no folds yet.")
    gene = rows[0].gene
    peptides = live_client.peptides.list(gene, limit=1)
    if not peptides:
        pytest.skip(f"No peptide rows for {gene}.")
    # We need a valid ptf_fold_results.id. Peptide.fold_id is the PK alias.
    pid = peptides[0].fold_id
    if not pid:
        pytest.skip("Peptide row has no fold_id (pre-fold or external feed).")
    detail = live_client.peptides.get(int(pid))
    assert detail.id == int(pid)
    assert detail.sequence
    assert detail.gene == gene
    # Thin response: heavy fields must be None
    assert detail.pocket_features_48_dim is None
    assert detail.peptide_per_receptor is None
    assert detail.pdb_content is None


@pytest.mark.integration
@_paid_only
def test_peptides_get_with_pocket_features(live_client: LigandAI) -> None:
    rows = live_client.peptides.by_gene(limit=5)
    if not rows:
        pytest.skip("Test account has no folds yet.")
    peptides = live_client.peptides.list(rows[0].gene, limit=1)
    if not peptides or not peptides[0].fold_id:
        pytest.skip("No fold_id available.")
    detail = live_client.peptides.get(
        int(peptides[0].fold_id),
        include=["pocket_features"],
    )
    # pocket_features_48_dim is None when no generation pocket data was
    # captured (older folds); when present, it must be 2D float matrix.
    if detail.pocket_features_48_dim is not None:
        assert isinstance(detail.pocket_features_48_dim, list)
        assert all(isinstance(row, list) for row in detail.pocket_features_48_dim)


@pytest.mark.integration
@_paid_only
def test_peptides_get_with_pdb(live_client: LigandAI) -> None:
    rows = live_client.peptides.by_gene(limit=5)
    if not rows:
        pytest.skip("Test account has no folds yet.")
    peptides = live_client.peptides.list(rows[0].gene, limit=1)
    if not peptides or not peptides[0].fold_id:
        pytest.skip("No fold_id available.")
    detail = live_client.peptides.get(
        int(peptides[0].fold_id),
        include=["pdb"],
    )
    if detail.pdb_content is not None:
        assert detail.pdb_content.startswith("HEADER") or "ATOM " in detail.pdb_content


@pytest.mark.integration
@_paid_only
def test_peptides_get_rejects_unknown_include(live_client: LigandAI) -> None:
    """Client-side allowlist check — fails before the request."""
    with pytest.raises(ValueError, match="Unknown include value"):
        live_client.peptides.get(1, include=["bogus_field"])  # type: ignore[list-item]


@pytest.mark.integration
@_paid_only
def test_peptides_get_rejects_invalid_id(live_client: LigandAI) -> None:
    with pytest.raises(ValueError):
        live_client.peptides.get(0)
    with pytest.raises(ValueError):
        live_client.peptides.get(-5)
    with pytest.raises(ValueError):
        live_client.peptides.get("not-a-number")
