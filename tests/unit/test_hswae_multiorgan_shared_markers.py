# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Multi-organ (brain + heart + eye) H-SWAE v2 shared-marker regression.

WHY THIS FILE EXISTS
--------------------
"Find the best shared markers for brain, heart and eyes" is a question a user
actually asked, and the SDK answered it WRONGLY on the first pass — not with an
error, with a plausible wrong gene. Two independent reasons:

1. ``differential`` arrives sorted by ``delta_csi``, and
   ``delta_csi = target_csi − max_reference_csi``. Because the comparator panel
   is made of cell types that exist in every organ, ``delta_csi`` structurally
   PENALISES a gene for being broadly expressed — which is the defining property
   of the thing being searched for. Ranking a pan-organ query by ``delta_csi``
   therefore promotes scarce genes and buries abundant ones.
2. A selectivity margin says nothing about copy number. ``KCNJ4`` clears every
   selectivity filter at 5.8 TPM, where there is not enough protein to bind.

So the recipe is pinned here as executable behaviour over RECORDED PRODUCTION
ROWS, not as prose that can drift. ``tests/fixtures/hswae_multiorgan_recorded_20261118.json.gz``
holds 3400 rows captured from https://ligandai.com on 2026-11-18 with
``ligandai==0.16.8`` (``hswae.v2.compare_cell_types``, ``top_n=200``,
``reference_cell_types=["fibroblasts", "macrophages"]``). The SHA-256 of each
untouched original response is carried in the fixture beside its rows. Nothing
here is synthetic; the live counterpart is
``tests/integration/test_hswae_multiorgan_live.py``.

CELL-SUBSET GRANULARITY
-----------------------
A shared marker is only actionable if the organ it is shared into can be named
at the grain you would target. Brain resolves 7 subsets and the eye 9, so both
carry real sub-organ structure. The heart resolves exactly ONE cardiac-specific
parenchymal subset (``cardiomyocytes``), which is why the three-organ
intersection is capped by cardiac coverage rather than by biology. That gap is
asserted as a STRICT xfail against bead ``dre-ish9z``: when cardiac subsets land,
this file fails loudly and tells us to raise the bound, instead of silently
passing a stale limit.

Ubiquitous cell types (fibroblasts, macrophages, smooth muscle, vascular
endothelium, pericytes) are excluded from every organ panel on purpose. A gene
"shared" across generic endothelium in three organs is an endothelium marker,
not an organ-shared marker — that mistake put ``EPHA7`` in an early shortlist on
a heart signal carried by vascular smooth muscle.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

import pytest

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "hswae_multiorgan_recorded_20261118.json.gz"
)

# Pinned from the recorded capture. Any change to the recipe moves these.
EXPECTED_UNIONS = {"brain": 688, "heart": 200, "eye": 829}
EXPECTED_SHARED = 49
EXPECTED_SELECTIVE = 34

# The abundance floor below which a selectivity win is not a usable target.
MIN_TPM_FOR_A_BINDER = 50.0


@pytest.fixture(scope="module")
def recorded() -> dict:
    with gzip.open(FIXTURE, "rb") as fh:
        return json.loads(fh.read())


@pytest.fixture(scope="module")
def organ_sets(recorded: dict) -> dict[str, dict[str, dict]]:
    """Organ gene set = union over its cell types, keeping each gene's best row.

    "Best" is the highest ``target_csi``: the per-organ worst case is taken
    later, so each organ must be represented by its strongest carrier.
    """
    out: dict[str, dict[str, dict]] = {}
    for organ, cell_types in recorded["panels"].items():
        best: dict[str, dict] = {}
        for ct in cell_types:
            entry = recorded["cell_types"][ct]
            for row in entry["rows"]:
                gene = row["gene_name"]
                prev = best.get(gene)
                if prev is None or (row["target_csi"] or 0) > (prev["target_csi"] or 0):
                    best[gene] = {**row, "_carrier": ct}
        out[organ] = best
    return out


def _worst(organ_sets, gene: str, field: str) -> float:
    return min((organ_sets[o][gene].get(field) or 0.0) for o in organ_sets)


@pytest.fixture(scope="module")
def shared(organ_sets) -> set[str]:
    sets = [set(v) for v in organ_sets.values()]
    return set.intersection(*sets)


@pytest.fixture(scope="module")
def selective(organ_sets, shared) -> list[str]:
    """Shared AND positively selective in EVERY organ — the filter, not the rank."""
    return [
        g for g in shared
        if all((organ_sets[o][g].get("delta_csi") or 0.0) > 0 for o in organ_sets)
    ]


# --------------------------------------------------------------------------
# fixture integrity: the rows are the server's, and we can prove which ones
# --------------------------------------------------------------------------

def test_fixture_carries_provenance_and_is_not_synthetic(recorded):
    prov = recorded["_provenance"]
    assert prov["operation"] == "hswae.v2.compare_cell_types"
    assert prov["top_n"] == 200
    assert prov["reference_cell_types"] == ["fibroblasts", "macrophages"]
    assert "ligandai==0.16.8" in prov["sdk"]
    assert "ligandai.com" in prov["endpoint"]
    # A key class may be recorded; a key value may never be.
    assert "never recorded" in prov["key_class"]
    for ct, entry in recorded["cell_types"].items():
        assert len(entry["source_sha256"]) == 64, ct
        assert int(entry["source_sha256"], 16) >= 0, ct


def test_every_panel_cell_type_resolved_exactly_and_not_by_proxy(recorded):
    """A proxy match would mean the organ panel is not the organ we asked for."""
    for organ, cell_types in recorded["panels"].items():
        for ct in cell_types:
            res = recorded["cell_types"][ct]["resolution"]
            assert res["resolution_status"] == "resolved", (organ, ct, res)
            assert res["resolution_stage"] == "exact", (organ, ct, res)
            assert res["resolution_is_proxy"] is False, (organ, ct, res)
            assert res["resolution_is_ambiguous"] is False, (organ, ct, res)


def test_panels_exclude_cell_types_present_in_every_organ(recorded):
    """EPHA7 regression: a vascular/stromal carrier is not organ evidence."""
    ubiquitous = {
        "fibroblasts", "macrophages", "smooth muscle cells",
        "vascular endothelial cells", "pericytes",
    }
    for organ, cell_types in recorded["panels"].items():
        assert not (ubiquitous & set(cell_types)), (organ, cell_types)
    # and they are exactly the comparator, for every organ alike
    for entry in recorded["cell_types"].values():
        assert set(entry["reference_cell_types"]) <= ubiquitous


# --------------------------------------------------------------------------
# the multi-organ recipe itself
# --------------------------------------------------------------------------

def test_three_organ_union_sizes_are_pinned(organ_sets):
    assert {o: len(v) for o, v in organ_sets.items()} == EXPECTED_UNIONS


def test_shared_and_selective_counts_are_pinned(shared, selective):
    assert len(shared) == EXPECTED_SHARED
    assert len(selective) == EXPECTED_SELECTIVE
    assert len(selective) < len(shared), "delta_csi > 0 must actually filter"


def test_multi_organ_query_resolves_to_ncam1(organ_sets, selective):
    """The answer to the user's question, as a property rather than a literal.

    NCAM1 must be the UNIQUE gene that is shared across brain + heart + eye,
    positively selective in all three, and abundant enough to bind in the
    weakest of the three.
    """
    bindable = [
        g for g in selective
        if _worst(organ_sets, g, "target_mean_expression") > 700.0
    ]
    assert bindable == ["NCAM1"], bindable
    assert _worst(organ_sets, "NCAM1", "target_csi") == pytest.approx(0.551, abs=0.01)
    assert _worst(organ_sets, "NCAM1", "delta_csi") == pytest.approx(0.515, abs=0.01)
    for organ in organ_sets:
        assert organ_sets[organ]["NCAM1"]["is_surface_annotated"] is True, organ


def test_ranking_by_delta_csi_does_not_answer_a_pan_organ_query(organ_sets, selective):
    """Section A, as a failing-if-violated assertion.

    ``delta_csi`` is the order the API returns, and it is the wrong order for a
    pan-organ query because the margin is measured against cell types that exist
    in every organ. Measured on this capture, with only two ubiquitous
    references, the effect is not catastrophic but it is already decisive at the
    top of the list: ``KCNJ4`` — 5.8 TPM in its weakest organ, far too few
    copies to bind — ranks **3rd of 34 by delta_csi** and **11th by worst-case
    target_csi**. Rank by ``delta_csi`` and an unbindable gene is in the top
    three; rank by worst-case ``target_csi`` and it is not. (With the
    16-member auto panel the same inversion is larger, which is why the panel
    must be held identical across organs whichever field is used.)
    """
    by_delta = sorted(selective, key=lambda g: -_worst(organ_sets, g, "delta_csi"))
    by_csi = sorted(selective, key=lambda g: -_worst(organ_sets, g, "target_csi"))

    assert by_delta[0] != "NCAM1", (
        "fixture drifted: the delta_csi trap is what this test documents"
    )
    assert by_delta.index("KCNJ4") < 3, by_delta[:4]
    assert by_csi.index("KCNJ4") > by_delta.index("KCNJ4") + 5, (
        by_delta.index("KCNJ4"), by_csi.index("KCNJ4")
    )

    # The head of the delta-ranked list reaches down to single-digit TPM; the
    # head of the csi-ranked list does not. Neither head is a shortlist on its
    # own — the abundance floor below is still required — but the delta head is
    # the one that needs rescuing.
    head_delta = [_worst(organ_sets, g, "target_mean_expression") for g in by_delta[:5]]
    head_csi = [_worst(organ_sets, g, "target_mean_expression") for g in by_csi[:5]]
    assert min(head_delta) < 10.0 <= min(head_csi), (head_delta, head_csi)


def test_selectivity_alone_admits_a_gene_with_no_material_to_bind(organ_sets, selective):
    """KCNJ4 clears every selectivity filter and is still not a target."""
    assert "KCNJ4" in selective
    kcnj4 = _worst(organ_sets, "KCNJ4", "target_mean_expression")
    ncam1 = _worst(organ_sets, "NCAM1", "target_mean_expression")
    assert kcnj4 < MIN_TPM_FOR_A_BINDER < ncam1
    assert ncam1 / kcnj4 > 100, (ncam1, kcnj4)
    # and it is NOT filtered out by selectivity, which is precisely the problem
    assert _worst(organ_sets, "KCNJ4", "delta_csi") > 0


def test_abundant_but_antiselective_genes_are_liabilities_not_targets(organ_sets, shared):
    """The inverse trap: high TPM with a NEGATIVE margin is off-target load.

    Abundance alone is not a target either. In the heart rows, ``PLPP3``
    (304.6 TPM, Δ −0.095) and ``NRP1`` (272.7 TPM, Δ −0.165) are more abundant
    than most of the shortlist while being LESS specific than the ubiquitous
    comparator — i.e. a binder against them loads fibroblasts and macrophages
    preferentially. They must never reach a shortlist.
    """
    liabilities = {
        gene for gene, row in organ_sets["heart"].items()
        if (row.get("target_mean_expression") or 0) > 250.0
        and (row.get("delta_csi") or 0) < 0
    }
    assert {"PLPP3", "NRP1"} <= liabilities, sorted(liabilities)

    # The selectivity filter is what excludes them, and it must keep doing so.
    selective_everywhere = {
        g for g in shared
        if all((organ_sets[o][g].get("delta_csi") or 0.0) > 0 for o in organ_sets)
    }
    assert not (liabilities & selective_everywhere)

    # A gene can also be abundant in one organ and anti-selective in another;
    # that is still disqualifying, and the per-organ filter catches it.
    mixed = {
        g for g in shared
        if any((organ_sets[o][g].get("delta_csi") or 0.0) < 0 for o in organ_sets)
        and max((organ_sets[o][g].get("target_mean_expression") or 0) for o in organ_sets) > 250.0
    }
    assert {"PTPRA", "UBAC2"} <= mixed, sorted(mixed)
    assert not (mixed & selective_everywhere)


def test_each_shared_gene_names_the_cell_subset_that_carries_it(organ_sets, selective):
    """A shared marker is only actionable with the carrier named per organ."""
    for gene in selective:
        for organ in organ_sets:
            carrier = organ_sets[organ][gene]["_carrier"]
            assert carrier, (gene, organ)
    assert organ_sets["heart"]["NCAM1"]["_carrier"] == "cardiomyocytes"
    assert organ_sets["brain"]["NCAM1"]["_carrier"] == "oligodendrocyte progenitor cells"
    assert organ_sets["eye"]["NCAM1"]["_carrier"] == "müller glia"


# --------------------------------------------------------------------------
# cell-subset granularity — what you can actually go after inside each organ
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "organ,minimum",
    [("brain", 7), ("eye", 9)],
)
def test_brain_and_eye_expose_targetable_cell_subsets(recorded, organ, minimum):
    subsets = recorded["panels"][organ]
    assert len(subsets) >= minimum, (organ, subsets)
    assert len(set(subsets)) == len(subsets), organ
    for ct in subsets:
        assert recorded["cell_types"][ct]["rows"], ct


def test_eye_granularity_spans_distinct_retinal_layers(recorded):
    """The eye is not one compartment: neural, glial, pigment and epithelial."""
    eye = set(recorded["panels"]["eye"])
    assert {"rod photoreceptor cells", "cone photoreceptor cells"} <= eye
    assert {"retinal ganglion cells", "retinal bipolar cells"} <= eye
    assert "müller glia" in eye, "umlaut spelling is load-bearing for resolution"
    assert "retinal pigment epithelial cells" in eye


@pytest.mark.xfail(
    strict=True,
    reason=(
        "dre-ish9z: the heart resolves exactly ONE cardiac-specific parenchymal "
        "subset. 'cardiac endothelial cells' falls back to generic 'vascular "
        "endothelial cells' (0.72) and 'cardiac fibroblasts' to generic "
        "'fibroblasts' (0.48), neither of which is heart evidence. When atrial / "
        "ventricular cardiomyocytes, conduction tissue and cardiac-resident "
        "stroma land in cell_type_global_specificity, this xpasses and the bound "
        "below must be raised."
    ),
)
def test_heart_exposes_at_least_three_cardiac_specific_subsets(recorded):
    assert len(recorded["panels"]["heart"]) >= 3


def test_the_heart_is_the_binding_constraint_on_the_intersection(organ_sets, recorded):
    """Pins WHY the answer is bounded, so the bound cannot be quietly inherited."""
    heart, brain, eye = (len(organ_sets[o]) for o in ("heart", "brain", "eye"))
    assert heart < brain and heart < eye
    assert len(recorded["panels"]["heart"]) == 1
    # Heart is capped by depth as well as by granularity: its single cell type
    # returns a truncated list, so the union cannot exceed top_n.
    entry = recorded["cell_types"]["cardiomyocytes"]
    assert entry["differential_truncated_at_top_n"] is True
    assert heart == recorded["_provenance"]["top_n"]


def test_no_organ_differential_came_back_empty_or_unreferenced(recorded):
    """An empty differential is a reference artefact, never evidence of nothing."""
    for ct, entry in recorded["cell_types"].items():
        assert entry["rows"], ct
        assert entry["differential_status"] != "no_references_resolved", ct
