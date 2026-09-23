# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""``avoid_paralogs=`` — the one customer-reachable contrastive surface. bd-dre-siz9d.3.

Before this, the only *named* contrastive surface was
``client.campaigns.contrastive_references()``, and it is a dead end for a
paying customer: it needs the 107/65-dim pocket-feature width, and
``server/receptor-intelligence-routes.ts`` 403s that width for anyone without
the internal entitlement (``POCKET_DIM_INTERNAL``). ``avoid_paralogs=`` names
the off-target GENES instead of fetching their raw pocket matrix, so it never
needs that width at all.

Covers:

1. the kwarg reaches the request body as ``cfg_reference_genes`` under both
   spellings;
2. normalisation — uppercase, order-preserving de-dupe, the >8 cap, and a
   blank entry — all refuse or normalise client-side, before any HTTP call;
3. refused on a non-CFG (v6.5-line) model, naming the models that DO take it;
4. forwarded, not vetoed, on a model id this SDK build has never heard of —
   a published SDK cannot know a model released after it;
5. refused alongside ``contrastive=`` — two ways to name one panel, and
   silently preferring one is how a caller ends up steered away from
   something they did not choose;
6. threaded through EVERY round of a ``recursive()`` multi-round refinement,
   unchanged, not re-derived per round;
7. the real production wall — a 403 ``POCKET_DIM_INTERNAL`` from
   ``Campaigns.pocket_features()`` / ``contrastive_references()`` — raises an
   error that names ``avoid_paralogs=`` as the reachable alternative, which is
   the single most important line of text this bead produces;
8. ``cfg_reference_provenance`` parses off a generation result, and absence
   (this server build did not report it) is distinguishable from "no
   conditioning happened" — the two must never render the same way.

No network: every transport is a recorder or a stub, following the pattern in
test_contrastive_width_request_f4mc9.py and test_recursive_contrastive_campaign.py.
"""

from __future__ import annotations

import inspect

import pytest

from ligandai.contrastive import (
    MAX_AVOID_PARALOGS,
    V6_9_POCKET_FEATURE_DIM,
    CfgReferenceProvenance,
    ContrastiveGuidance,
    ContrastiveReference,
    PocketFeatureMatrix,
    avoid_paralogs_to_wire,
    normalize_avoid_paralogs,
    require_avoid_paralogs_model,
)
from ligandai.errors import LigandAIForbidden, LigandAIValidationError
from ligandai.resources.campaigns import Campaigns
from ligandai.resources.peptides import Peptides, _generation_body, _parse_generation


def _rows(dim: int = V6_9_POCKET_FEATURE_DIM, n: int = 6, fill: float = 0.25):
    return [[fill + 0.001 * i + 0.0001 * j for j in range(dim)] for i in range(n)]


def _ref(gene: str, dim: int = V6_9_POCKET_FEATURE_DIM) -> ContrastiveReference:
    return ContrastiveReference(gene=gene, features=PocketFeatureMatrix(rows=_rows(dim)))


class _FakeClient:
    pass


def _generate_body(*, model=None, **kw):
    """Build a real request body through the ONE builder both generate() paths use.

    Mirrors the helper of the same name in test_contrastive_width_request_f4mc9.py:
    every parameter takes generate()'s OWN default so this cannot drift into
    exercising a shape no caller can produce.
    """
    gen_defaults = {
        name: p.default
        for name, p in inspect.signature(Peptides.generate).parameters.items()
        if p.default is not inspect.Parameter.empty
    }
    args: dict = {}
    for name, p in inspect.signature(_generation_body).parameters.items():
        if name in kw:
            args[name] = kw[name]
        elif name in gen_defaults:
            args[name] = gen_defaults[name]
        elif p.default is not inspect.Parameter.empty:
            args[name] = p.default
        else:
            args[name] = None
    args["model"] = model
    args.update(kw)
    return _generation_body(**args)


# ─────────────────────────────────────────────────────────────────────────────
# 1 — the kwarg reaches the wire as cfg_reference_genes, under both spellings
# ─────────────────────────────────────────────────────────────────────────────


def test_avoid_paralogs_reaches_the_body_under_both_spellings():
    body = _generate_body(
        gene="BRD4", num_peptides=4, model="v6.9.21",
        avoid_paralogs=["BRD2", "BRD3", "BRDT"],
    )
    for key in ("cfg_reference_genes", "cfgReferenceGenes"):
        assert key in body, key
        assert body[key] == ["BRD2", "BRD3", "BRDT"]


def test_a_body_without_avoid_paralogs_carries_no_reference_gene_keys():
    body = _generate_body(gene="BRD4", num_peptides=4, model="v6.9.21")
    assert "cfg_reference_genes" not in body
    assert "cfgReferenceGenes" not in body


def test_avoid_paralogs_to_wire_emits_only_the_two_spellings():
    wire = avoid_paralogs_to_wire(("BRD2", "BRD3"))
    assert set(wire) == {"cfg_reference_genes", "cfgReferenceGenes"}
    assert wire["cfg_reference_genes"] == ["BRD2", "BRD3"]
    assert wire["cfgReferenceGenes"] == ["BRD2", "BRD3"]


# ─────────────────────────────────────────────────────────────────────────────
# 2 — normalisation: uppercase, order-preserving de-dupe, the cap, blanks
# ─────────────────────────────────────────────────────────────────────────────


def test_normalize_avoid_paralogs_uppercases_and_dedupes_preserving_order():
    assert normalize_avoid_paralogs(["brd3", "BRD2", " brd3 ", "Brd2"]) == ("BRD3", "BRD2")


def test_normalize_avoid_paralogs_none_and_empty_are_both_no_op():
    assert normalize_avoid_paralogs(None) is None
    assert normalize_avoid_paralogs([]) is None


def test_normalize_avoid_paralogs_refuses_a_blank_entry():
    with pytest.raises(LigandAIValidationError, match="blank"):
        normalize_avoid_paralogs(["BRD2", "   "])
    with pytest.raises(LigandAIValidationError, match="blank"):
        normalize_avoid_paralogs(["BRD2", ""])


def test_normalize_avoid_paralogs_refuses_more_than_the_cap():
    assert MAX_AVOID_PARALOGS == 8
    ok = normalize_avoid_paralogs([f"G{i}" for i in range(MAX_AVOID_PARALOGS)])
    assert len(ok) == MAX_AVOID_PARALOGS
    with pytest.raises(LigandAIValidationError, match=str(MAX_AVOID_PARALOGS)):
        normalize_avoid_paralogs([f"G{i}" for i in range(MAX_AVOID_PARALOGS + 1)])


def test_the_cap_is_not_silently_truncated_it_is_refused():
    """The SDK never picks which N of the caller's genes to keep."""
    genes = [f"G{i}" for i in range(MAX_AVOID_PARALOGS + 3)]
    with pytest.raises(LigandAIValidationError):
        _generate_body(gene="BRD4", num_peptides=4, model="v6.9.21", avoid_paralogs=genes)


# ─────────────────────────────────────────────────────────────────────────────
# 3 — refused on a non-CFG model, naming the models that DO take it
# ─────────────────────────────────────────────────────────────────────────────


def test_avoid_paralogs_is_refused_on_a_v65_model_naming_the_cfg_models():
    with pytest.raises(LigandAIValidationError) as exc:
        _generate_body(
            gene="BRD4", num_peptides=4, model="v6.5.21", avoid_paralogs=["BRD2"],
        )
    msg = str(exc.value)
    assert "v6.9.21" in msg and "v6.9.22" in msg
    assert "cfg_references_not_supported" in msg


def test_require_avoid_paralogs_model_directly():
    with pytest.raises(LigandAIValidationError, match="does not consume"):
        require_avoid_paralogs_model("v6.5.21")
    # No-op for a CFG-capable model.
    require_avoid_paralogs_model("v6.9.21")
    require_avoid_paralogs_model("v6.9.22")


# ─────────────────────────────────────────────────────────────────────────────
# 4 — forwarded, not vetoed, on a model this build has never heard of
# ─────────────────────────────────────────────────────────────────────────────


def test_avoid_paralogs_is_forwarded_not_vetoed_on_an_unknown_model():
    # A published SDK cannot know a model released after it was built.
    require_avoid_paralogs_model("v7.0.0-unreleased")
    body = _generate_body(
        gene="BRD4", num_peptides=4, model="v7.0.0-unreleased", avoid_paralogs=["BRD2"],
    )
    assert body["cfg_reference_genes"] == ["BRD2"]


# ─────────────────────────────────────────────────────────────────────────────
# 5 — refused alongside contrastive= — two ways to name one panel
# ─────────────────────────────────────────────────────────────────────────────


def test_avoid_paralogs_is_refused_together_with_contrastive():
    g = ContrastiveGuidance([_ref("BRD3")])
    with pytest.raises(LigandAIValidationError, match="not both"):
        _generate_body(
            gene="BRD4", num_peptides=4, model="v6.9.21",
            avoid_paralogs=["BRD2"], contrastive=g,
        )


def test_recursive_refuses_avoid_paralogs_with_contrastive():
    camp = Campaigns(transport=None, client=_FakeClient())  # type: ignore[arg-type]
    g = ContrastiveGuidance([_ref("ENPP1")])
    with pytest.raises(LigandAIValidationError, match="not both"):
        camp.recursive(
            target="ENPP3", off_targets=["ENPP6"], band=0.1, target_leg_min=0.66,
            avoid_paralogs=["ENPP1"], contrastive=g,
        )


def test_recursive_refuses_avoid_paralogs_with_contrastive_by_target():
    camp = Campaigns(transport=None, client=_FakeClient())  # type: ignore[arg-type]
    g = ContrastiveGuidance([_ref("ENPP1")])
    with pytest.raises(LigandAIValidationError, match="not both"):
        camp.recursive(
            target="ENPP3", off_targets=["ENPP6"], band=0.1, target_leg_min=0.66,
            avoid_paralogs=["ENPP1"], contrastive_by_target={"ENPP3": g},
        )


# ─────────────────────────────────────────────────────────────────────────────
# 6 — threaded through EVERY round of recursive(), unchanged
# ─────────────────────────────────────────────────────────────────────────────


class _FakeGenerationResult:
    def __init__(self, seqs):
        self.peptides = [{"sequence": s} for s in seqs]


class _FakeJob:
    def __init__(self, seqs):
        self._seqs = seqs

    def wait(self):
        return _FakeGenerationResult(self._seqs)


class _RecordingPeptides:
    """Records every kwarg client.campaigns._generate() passes to generate()."""

    def __init__(self, seqs=("ACDEFGHIKLMNP",)):
        self.calls: list[dict] = []
        self._seqs = list(seqs)

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeJob(self._seqs)


class _RecordingClient:
    def __init__(self):
        self.peptides = _RecordingPeptides()


def test_avoid_paralogs_normalizes_and_stores_on_the_campaign():
    client = _RecordingClient()
    camp = Campaigns(transport=None, client=client)  # type: ignore[arg-type]
    run = camp.recursive(
        target="ENPP3", off_targets=["ENPP6", "ENPP1"], band=0.1, target_leg_min=0.66,
        avoid_paralogs=["enpp1", "ENPP6"],
    )
    assert run.avoid_paralogs == ("ENPP1", "ENPP6")


def test_avoid_paralogs_is_carried_into_every_generation_leg():
    client = _RecordingClient()
    camp = Campaigns(transport=None, client=client)  # type: ignore[arg-type]
    run = camp.recursive(
        target="ENPP3", off_targets=["ENPP6", "ENPP1"], band=0.1, target_leg_min=0.66,
        avoid_paralogs=["ENPP1", "ENPP6"],
    )
    notes: list[str] = []
    run._generate(notes)
    assert len(client.peptides.calls) == 1
    call = client.peptides.calls[0]
    assert call["avoid_paralogs"] == ["ENPP1", "ENPP6"]
    assert "contrastive" not in call or call["contrastive"] is None
    assert any("avoid_paralogs" in n for n in notes)


def test_avoid_paralogs_is_carried_into_every_round_not_just_the_first():
    client = _RecordingClient()
    camp = Campaigns(transport=None, client=client)  # type: ignore[arg-type]
    run = camp.recursive(
        target="ENPP3", off_targets=["ENPP6"], band=0.1, target_leg_min=0.66,
        avoid_paralogs=["ENPP6"],
    )
    for _ in range(3):
        notes: list[str] = []
        run._generate(notes)
    assert len(client.peptides.calls) == 3
    assert all(c.get("avoid_paralogs") == ["ENPP6"] for c in client.peptides.calls)


def test_a_per_target_contrastive_panel_wins_over_campaign_avoid_paralogs_default():
    """RecursiveCampaign() constructed directly (not via recursive()) can still
    combine the two IF contrastive_by_target only covers some targets — the
    conflict guard is at the recursive()/RecursiveCampaign() construction
    boundary, not a per-leg override. This test pins the per-leg precedence
    that _generate() actually implements: an explicit per-leg panel wins.
    """
    from ligandai.resources.campaigns import RecursiveCampaign

    client = _RecordingClient()
    camp = Campaigns(transport=None, client=client)  # type: ignore[arg-type]
    g = ContrastiveGuidance([_ref("ENPP1")])
    run = RecursiveCampaign(
        campaigns=camp,
        target="ENPP3",
        off_targets=["ENPP6"],
        band=0.1,
        target_leg_min=0.66,
        contrastive=g,
    )
    # avoid_paralogs was never set on this campaign; contrastive wins as usual.
    notes: list[str] = []
    run._generate(notes)
    assert client.peptides.calls[0]["contrastive"] is g
    assert "avoid_paralogs" not in client.peptides.calls[0]


# ─────────────────────────────────────────────────────────────────────────────
# 7 — the 403 POCKET_DIM_INTERNAL wall, and the error that points past it
# ─────────────────────────────────────────────────────────────────────────────


def _forbidden_pocket_dim(gene: str = "BRD4", dim: int = 107) -> LigandAIForbidden:
    return LigandAIForbidden(
        f"Pocket feature width {dim} is Ligandal-internal.",
        code="POCKET_DIM_INTERNAL",
        status_code=403,
        response={
            "error": "pocket_dim_not_entitled",
            "code": "POCKET_DIM_INTERNAL",
            "gene": gene,
            "requested_dim": dim,
            "public_dims": [48],
        },
    )


class _EntitlementWallTransport:
    def request(self, method, path, **kw):
        raise _forbidden_pocket_dim()


def test_pocket_features_403_names_avoid_paralogs_as_the_alternative():
    camp = Campaigns(transport=_EntitlementWallTransport(), client=_FakeClient())  # type: ignore[arg-type]
    with pytest.raises(LigandAIForbidden) as exc:
        camp.pocket_features("BRD4", dim=107)
    assert exc.value.reason == "POCKET_DIM_INTERNAL" or exc.value.code == "POCKET_DIM_INTERNAL"
    assert "avoid_paralogs" in str(exc.value)


def test_contrastive_references_403_also_names_avoid_paralogs():
    """contrastive_references() calls pocket_features() per paralog — the same
    wall, reached through the SDK's only named contrastive surface.
    """
    camp = Campaigns(transport=_EntitlementWallTransport(), client=_FakeClient())  # type: ignore[arg-type]
    with pytest.raises(LigandAIForbidden) as exc:
        camp.contrastive_references(target="BRD4", paralogs=["BRD2"], model="v6.9.21")
    assert "avoid_paralogs" in str(exc.value)


def test_a_different_403_is_not_relabeled_as_the_entitlement_wall():
    """A pilot-restricted / legal-acceptance 403 must pass through unchanged —
    this guard is specific to POCKET_DIM_INTERNAL, not every 403.
    """

    class _OtherForbiddenTransport:
        def request(self, method, path, **kw):
            raise LigandAIForbidden(
                "pilot restricted", reason="pilot_restricted",
                code="pilot_restricted", status_code=403,
            )

    camp = Campaigns(transport=_OtherForbiddenTransport(), client=_FakeClient())  # type: ignore[arg-type]
    with pytest.raises(LigandAIForbidden) as exc:
        camp.pocket_features("BRD4", dim=107)
    assert "avoid_paralogs" not in str(exc.value)
    assert exc.value.reason == "pilot_restricted"


# ─────────────────────────────────────────────────────────────────────────────
# 8 — provenance: absent is not "no conditioning happened"
# ─────────────────────────────────────────────────────────────────────────────


def test_cfg_reference_provenance_is_none_when_the_server_does_not_report_it():
    """Today's production reality (2026-09-23): no job record carries this
    field yet. None must mean "not reported", and the caller must be able to
    tell that apart from a genuinely uncontrasted run — this test is the
    guard against the two ever rendering the same way.
    """
    payload = {
        "jobId": "job1", "gene": "BRD4", "peptides": [],
        "parameters": {"generatorModelId": "v6.9.21"},
    }
    result = _parse_generation(payload)
    assert result.cfg_reference_provenance is None


def test_cfg_reference_provenance_parses_a_reported_panel():
    payload = {
        "jobId": "job2", "gene": "BRD4", "peptides": [],
        "parameters": {"cfgReferenceGenes": ["BRD2", "BRD3"]},
        "cfg_reference_provenance": {
            "generator_model_id": "v6.9.21",
            "expected_dim": 107,
            "paralogs": [
                {
                    "paralog_gene": "BRD2",
                    "source_pdb_id": "1ABC",
                    "source_chain": "A",
                    "identity_to_target_domain": 0.71,
                    "n_rows": 13,
                },
                {
                    "paralog_gene": "BRD3",
                    "source_pdb_id": "2XYZ",
                    "source_chain": "B",
                    "identity_to_target_domain": 0.64,
                    "n_rows": 11,
                },
            ],
            "rule_version": "paralog_ref_v3",
        },
    }
    result = _parse_generation(payload)
    prov = result.cfg_reference_provenance
    assert prov is not None
    assert isinstance(prov, CfgReferenceProvenance)
    assert prov.generator_model_id == "v6.9.21"
    assert prov.expected_dim == 107
    assert prov.rule_version == "paralog_ref_v3"
    assert [p.paralog_gene for p in prov.paralogs] == ["BRD2", "BRD3"]
    assert prov.paralogs[0].source_pdb_id == "1ABC"
    assert prov.paralogs[0].n_rows == 13
    # The request named the panel (echoed in parameters) -> caller_requested.
    assert prov.caller_requested is True


def test_cfg_reference_provenance_caller_requested_false_when_platform_derived():
    payload = {
        "jobId": "job3", "gene": "BRD4", "peptides": [],
        "parameters": {"generatorModelId": "v6.9.21"},  # no cfgReferenceGenes
        "cfg_reference_provenance": {
            "generator_model_id": "v6.9.21",
            "expected_dim": 107,
            "paralogs": [
                {"paralog_gene": "BRD3", "n_rows": 9},
            ],
            "rule_version": "paralog_ref_v3",
        },
    }
    result = _parse_generation(payload)
    prov = result.cfg_reference_provenance
    assert prov is not None
    assert prov.caller_requested is False


def test_cfg_reference_provenance_explicit_flag_wins_over_the_hint():
    payload = {
        "jobId": "job4", "gene": "BRD4", "peptides": [],
        "parameters": {"cfgReferenceGenes": ["BRD3"]},  # would hint True
        "cfg_reference_provenance": {
            "generator_model_id": "v6.9.21",
            "paralogs": [{"paralog_gene": "BRD3"}],
            "caller_requested": False,  # but the payload says otherwise
        },
    }
    result = _parse_generation(payload)
    assert result.cfg_reference_provenance.caller_requested is False


def test_cfg_reference_provenance_from_wire_is_none_for_empty_or_non_dict():
    assert CfgReferenceProvenance.from_wire(None) is None
    assert CfgReferenceProvenance.from_wire({}) is None
    assert CfgReferenceProvenance.from_wire("not a dict") is None  # type: ignore[arg-type]
