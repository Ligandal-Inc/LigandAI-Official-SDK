# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Live production acceptance for the multi-organ H-SWAE path.

This is the gate for closing beads dre-w7tu9.1/.2/.3/.5/.6. The recorded
counterpart (``tests/unit/test_hswae_multiorgan_shared_markers.py``) pins the
science; this file asks production whether the defects that forced the recorded
workaround are gone.

Run it deliberately::

    LIGANDAI_API_KEY=... pytest tests/integration/test_hswae_multiorgan_live.py -m integration -v

Every test here is skipped without a key. Several are EXPECTED TO FAIL against
production as measured on 2026-11-20 — that is the point: they are the
acceptance criteria, not a description of current behaviour. Each one names its
bead so a failure reads as "this defect is still live", and each is a strict
xfail so the first green run tells us the fix reached production instead of
sitting on a preview.
"""

from __future__ import annotations

import os
import time

import pytest

from ligandai import LigandAI

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (os.environ.get("LIGANDAI_API_KEY") or os.environ.get("LIGANDAL_API_KEY")),
        reason="live production test: set LIGANDAI_API_KEY",
    ),
]

BRAIN = ["astrocytes", "oligodendrocyte progenitor cells", "microglia"]
HEART = ["cardiomyocytes"]
EYE = ["müller glia", "rod photoreceptor cells", "retinal pigment epithelial cells"]

SKIN_PANEL_MARKERS = {"basal keratinocytes", "langerhans cells", "cdc"}


@pytest.fixture(scope="module")
def client() -> LigandAI:
    return LigandAI()


def _compare(client: LigandAI, cell_type: str, *, top_n: int = 100, **kw) -> dict:
    payload = {"target_cell_type": cell_type, "top_n": top_n, **kw}
    last: Exception | None = None
    for attempt in range(1, 4):  # a 502 here is an unfinished query, not a verdict
        try:
            return client.discovery.invoke_capability(
                "hswae.v2.compare_cell_types", payload
            ).result.model_dump()
        except Exception as exc:  # noqa: BLE001 - the status is the measurement
            last = exc
            time.sleep(2 * attempt)
    raise AssertionError(f"{cell_type}: 3 attempts failed, last={type(last).__name__}")


# --------------------------------------------------------------------------
# the multi-organ path, end to end
# --------------------------------------------------------------------------

def test_all_three_organs_resolve_their_cell_subsets(client):
    """Brain, heart and eye must each name subsets exactly, with no proxying."""
    for organ, cell_types in (("brain", BRAIN), ("heart", HEART), ("eye", EYE)):
        for ct in cell_types:
            d = _compare(client, ct, top_n=10,
                         reference_cell_types=["fibroblasts", "macrophages"])
            res = d["target_resolution"]
            assert res["canonical"] == ct, (organ, ct, res)
            assert res["resolution_stage"] == "exact", (organ, ct, res)
            assert res["resolution_is_proxy"] is False, (organ, ct, res)


def test_shared_marker_recipe_returns_ncam1_across_brain_heart_eye(client):
    """The user's question, asked against live production."""
    organ_sets: dict[str, dict[str, dict]] = {}
    for organ, cell_types in (("brain", BRAIN), ("heart", HEART), ("eye", EYE)):
        best: dict[str, dict] = {}
        for ct in cell_types:
            d = _compare(client, ct, top_n=100,
                         reference_cell_types=["fibroblasts", "macrophages"])
            for row in d["differential"]:
                gene = row["gene_name"]
                prev = best.get(gene)
                if prev is None or (row["target_csi"] or 0) > (prev["target_csi"] or 0):
                    best[gene] = row
        assert best, organ
        organ_sets[organ] = best

    shared = set.intersection(*(set(v) for v in organ_sets.values()))
    assert shared, "no gene shared across all three organs"

    def worst(gene: str, field: str) -> float:
        return min((organ_sets[o][gene].get(field) or 0.0) for o in organ_sets)

    selective = [g for g in shared if all(
        (organ_sets[o][g].get("delta_csi") or 0.0) > 0 for o in organ_sets)]
    assert "NCAM1" in selective, sorted(selective)
    assert worst("NCAM1", "target_mean_expression") > 500.0

    # The ranking rule: NCAM1 must win on worst-case target_csi among the
    # genes that are abundant enough to bind.
    bindable = sorted(
        (g for g in selective if worst(g, "target_mean_expression") > 500.0),
        key=lambda g: -worst(g, "target_csi"),
    )
    assert bindable[0] == "NCAM1", bindable[:5]


# --------------------------------------------------------------------------
# acceptance criteria for the open defects
# --------------------------------------------------------------------------

@pytest.mark.xfail(strict=True, reason="dre-w7tu9.1: prod emits no definitionDigest")
def test_capabilities_registry_validates_for_a_pinned_client(client):
    reg = client.discovery.capabilities()
    caps = reg.capabilities
    assert caps
    assert all(getattr(c, "definition_digest", None) for c in caps)


@pytest.mark.xfail(strict=True, reason="dre-w7tu9.3: canonical_cell_type/confidence null")
def test_resolve_cell_type_populates_its_receipt(client):
    d = client.discovery.invoke_capability(
        "hswae.v2.resolve_cell_type", {"query": "cardiomyocytes"}
    ).result.model_dump()
    assert d["canonical_cell_type"] == "cardiomyocytes"
    assert (d["confidence"] or 0) >= 0.9


@pytest.mark.xfail(strict=True, reason="dre-w7tu9.5: topical/tissue_resident serve skin")
@pytest.mark.parametrize("scope", ["topical", "tissue_resident"])
def test_auto_comparator_is_organ_native_not_skin(client, scope):
    d = _compare(client, "cardiomyocytes", top_n=25, comparator_scope=scope)
    refs = {r.lower() for r in (d["reference_cell_types"] or [])}
    assert refs, (scope, d["differential_status"])
    assert not (refs & SKIN_PANEL_MARKERS), (scope, sorted(refs))


@pytest.mark.xfail(strict=True, reason="dre-w7tu9.6: ~15s wall 502s the schema ceiling")
def test_schema_ceiling_top_n_is_actually_servable(client):
    t0 = time.time()
    d = client.discovery.invoke_capability(
        "hswae.v2.compare_cell_types",
        {"target_cell_type": "cardiomyocytes",
         "reference_cell_types": ["fibroblasts", "macrophages"], "top_n": 200},
    ).result.model_dump()
    elapsed = time.time() - t0
    assert len(d["differential"]) == 200
    assert elapsed < 5.0, f"served, but in {elapsed:.1f}s"


@pytest.mark.xfail(strict=True, reason="dre-w7tu9.2: v1 search index lacks 'eye'")
def test_eye_is_reachable_from_the_v1_layer_too(client):
    d = client.discovery.invoke_capability(
        "hswae.v1.semantic_search", {"query": "eye"}
    ).result.model_dump()
    assert (d.get("count") or 0) > 0


@pytest.mark.xfail(strict=True, reason="dre-ish9z: no cardiac-specific subsets beyond cardiomyocytes")
@pytest.mark.parametrize("query", ["cardiac endothelial cells", "cardiac fibroblasts"])
def test_cardiac_subsets_resolve_without_falling_back_to_generic_stroma(client, query):
    d = _compare(client, query, top_n=10,
                 reference_cell_types=["fibroblasts", "macrophages"])
    res = d["target_resolution"]
    assert res["resolution_is_proxy"] is False, res
    assert "cardiac" in (res["canonical"] or "").lower(), res
