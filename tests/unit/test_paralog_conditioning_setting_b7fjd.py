# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""``avoid_paralogs=`` as a design SETTING — the three states. bd-LIGANDAI_ALPHA_V2-b7fjd.2.

The website, the unified orchestrator and this SDK now all drive ONE field. The
settings object the other surfaces carry is
``paralogConditioning: {enabled, paralogs}``, and the whole point of this file is
that the SDK's ``avoid_paralogs=`` means exactly the same three things:

1. **a named panel** -> the caller chooses the comparators
   (``cfg_reference_genes`` on the wire, both spellings);
2. **enabled, empty panel** -> the platform chooses the comparators.
   Conditioning STILL HAPPENS on a capable checkpoint. Nothing extra on the
   wire, and it is NOT an error;
3. **not enabled** -> the caller did not choose comparators. Byte-identical to
   (2), and the model STILL CONDITIONS on a capable checkpoint.

⛔ "OFF" MEANS "I DID NOT CHOOSE THE COMPARATORS", NOT "NO CONDITIONING". There
is no conditioning-disable switch on this surface, and a test that implied one
would be pinning a lie into the suite.

⛔ THE NEGATIVE CONTROL THIS FILE EXISTS FOR. An assertion that
``cfg_reference_genes`` is ABSENT also passes if the SDK silently dropped a panel
the caller named — the exact failure the capability must never have. So every
absence assertion here is PAIRED, in the same test, with the equivalent capable
case asserting a named panel IS present. Absence is only meaningful next to a
presence that was produced by the same code path.

⛔ ON WHAT THIS CAPABILITY IS WORTH. In our controlled test it was not more
selective than generate-and-filter. Use it as a different experiment, not as an
upgrade. ``PARALOG_CONDITIONING_NOTE`` is that sentence verbatim and a test below
pins it, so a later "tightening" of the copy fails a gate instead of shipping.

No network: bodies are built through the one real builder both generate() paths
use, following test_avoid_paralogs_siz9d.py.
"""

from __future__ import annotations

import inspect

import pytest

from ligandai.contrastive import (
    AVOID_PARALOGS_GENE_RE,
    MAX_AVOID_PARALOGS,
    PARALOG_CONDITIONING_NOTE,
    V6_9_POCKET_FEATURE_DIM,
    ContrastiveGuidance,
    ContrastiveReference,
    PocketFeatureMatrix,
    normalize_avoid_paralogs,
)
from ligandai.errors import (
    LigandAICfgReferencesRefused,
    LigandAIInvalidConfig,
    LigandAIValidationError,
    error_from_response,
)
from ligandai.resources.peptides import Peptides, _generation_body

CAPABLE = "v6.9.21"
INCAPABLE = "v6.5.21"
_GENE_KEYS = ("cfg_reference_genes", "cfgReferenceGenes")


def _generate_body(*, model=None, **kw):
    """Build a real request body through the ONE builder both generate() paths use."""
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


def _rows(dim: int = V6_9_POCKET_FEATURE_DIM, n: int = 6, fill: float = 0.25):
    return [[fill + 0.001 * i + 0.0001 * j for j in range(dim)] for i in range(n)]


def _guidance(gene: str = "BRD3") -> ContrastiveGuidance:
    """A raw pocket-feature panel — the thing a named panel is exclusive WITH."""
    return ContrastiveGuidance(
        references=[
            ContrastiveReference(
                gene=gene, features=PocketFeatureMatrix(rows=_rows())
            )
        ]
    )


# ─────────────────────────────────────────────────────────────────────────────
# STATE 1 vs 2 vs 3 — each absence assertion PAIRED with a presence
# ─────────────────────────────────────────────────────────────────────────────


def test_a_named_panel_serialises_and_an_empty_one_does_not_in_the_same_breath():
    """State 1 vs state 2, asserted against each other.

    The pairing is the control: if the SDK silently dropped a named panel, the
    `empty` half would still pass while the `named` half would fail, so the two
    cannot both pass unless presence and absence are each produced on purpose.
    """
    named = _generate_body(
        gene="BRD4", num_peptides=4, model=CAPABLE,
        avoid_paralogs=["BRD2", "BRD3"],
    )
    empty = _generate_body(
        gene="BRD4", num_peptides=4, model=CAPABLE, avoid_paralogs=[],
    )
    for key in _GENE_KEYS:
        assert named[key] == ["BRD2", "BRD3"], f"named panel lost from {key}"
        assert key not in empty, f"empty panel fabricated {key}"


def test_an_empty_panel_is_not_an_error_it_means_the_platform_picks():
    """State 2. Empty is a REAL choice, not a validation failure."""
    body = _generate_body(
        gene="BRD4", num_peptides=4, model=CAPABLE, avoid_paralogs=[],
    )
    assert body["peptidesPerTarget"] == 4  # the run is intact, not refused
    assert body["generatorModelId"] == CAPABLE
    for key in _GENE_KEYS:
        assert key not in body
    # ...and the same call with a panel DOES send one, so the absence above is
    # a decision rather than a dropped field.
    assert _generate_body(
        gene="BRD4", num_peptides=4, model=CAPABLE, avoid_paralogs=["BRD3"],
    )["cfg_reference_genes"] == ["BRD3"]


def test_not_enabled_sends_nothing_and_is_not_an_error():
    """State 3. Omitting the argument is byte-identical to the empty panel."""
    omitted = _generate_body(gene="BRD4", num_peptides=4, model=CAPABLE)
    empty = _generate_body(
        gene="BRD4", num_peptides=4, model=CAPABLE, avoid_paralogs=[],
    )
    assert omitted == empty, (
        "omitting avoid_paralogs must be indistinguishable on the wire from an "
        "empty panel — both mean 'the platform picks', and a difference here "
        "would make 'off' a second, undocumented wire state"
    )
    for key in _GENE_KEYS:
        assert key not in omitted
    # Paired presence: the same path still carries a panel when one is named.
    assert "cfg_reference_genes" in _generate_body(
        gene="BRD4", num_peptides=4, model=CAPABLE, avoid_paralogs=["BRD3"],
    )


def test_none_and_empty_normalise_identically_and_a_panel_does_not():
    assert normalize_avoid_paralogs(None) is None
    assert normalize_avoid_paralogs([]) is None
    assert normalize_avoid_paralogs(["brd3"]) == ("BRD3",)


# ─────────────────────────────────────────────────────────────────────────────
# CLIENT-SIDE REFUSALS — the cap and the symbol shape
# ─────────────────────────────────────────────────────────────────────────────


def test_nine_distinct_genes_are_refused_client_side_with_the_count():
    nine = [f"GENE{i}" for i in range(9)]
    with pytest.raises(LigandAIValidationError) as ei:
        normalize_avoid_paralogs(nine)
    msg = str(ei.value)
    assert "9" in msg and str(MAX_AVOID_PARALOGS) in msg
    # Paired control: exactly the cap is ACCEPTED, so the refusal is about the
    # ninth gene and not about panels in general.
    assert len(normalize_avoid_paralogs(nine[:MAX_AVOID_PARALOGS])) == MAX_AVOID_PARALOGS


def test_the_cap_counts_distinct_genes_not_repetitions():
    """Nine repetitions of one gene is ONE gene, and must not trip the cap."""
    assert normalize_avoid_paralogs(["BRD3"] * 9) == ("BRD3",)


@pytest.mark.parametrize(
    "bad",
    [
        "BRD 3",        # whitespace inside a symbol
        "BRD3,BRD4",    # two genes crammed into one entry
        "BRD3;BRD4",
        "BRD_3",        # underscore is outside the server's character class
        "BRD3!",
        "X" * 33,       # one over the 32-char limit
    ],
)
def test_a_malformed_symbol_is_refused_client_side_and_named(bad):
    with pytest.raises(LigandAIValidationError) as ei:
        normalize_avoid_paralogs([bad])
    msg = str(ei.value)
    assert repr(bad) in msg, "the refusal must name the offending entry"
    assert "32" in msg  # states the actual rule, not just 'invalid'


def test_the_symbol_shape_mirrors_the_servers_character_class():
    """⛔ A SHAPE, NOT AN ALLOW-LIST — a real symbol list would reject genes the
    paralog producer can serve. Hyphens and digits are legal; separators are not.
    """
    for good in ("BRD3", "HLA-A", "H2-K1", "A", "X" * 32):
        assert AVOID_PARALOGS_GENE_RE.match(good), good
        assert normalize_avoid_paralogs([good]) == (good.upper(),)
    for bad in ("", "BRD 3", "BRD,4", "X" * 33):
        assert not AVOID_PARALOGS_GENE_RE.match(bad), bad


def test_a_blank_entry_is_refused_rather_than_read_as_no_restriction():
    with pytest.raises(LigandAIValidationError):
        normalize_avoid_paralogs(["BRD3", ""])


# ─────────────────────────────────────────────────────────────────────────────
# MUTUAL EXCLUSION — a named panel and raw pocket features
# ─────────────────────────────────────────────────────────────────────────────


def test_a_named_panel_plus_raw_pocket_features_is_refused_not_resolved():
    """Neither wins. Silent precedence is how a caller is steered away from
    comparators they did not choose, so the request is refused client-side.
    """
    with pytest.raises(LigandAIValidationError) as ei:
        _generate_body(
            gene="BRD4", num_peptides=4, model=CAPABLE,
            avoid_paralogs=["BRD2"], contrastive=_guidance(),
        )
    msg = str(ei.value)
    assert "avoid_paralogs" in msg and "contrastive" in msg
    assert "cfg_reference_genes_with_explicit_features" in msg, (
        "the client-side refusal must name the server code it mirrors, so a "
        "caller searching the error text finds the same contract either way"
    )


def test_either_one_alone_is_accepted_so_the_refusal_is_the_combination():
    """Paired control for the test above: the refusal is about BOTH, not about
    either parameter being unusable.
    """
    panel_only = _generate_body(
        gene="BRD4", num_peptides=4, model=CAPABLE, avoid_paralogs=["BRD2"],
    )
    assert panel_only["cfg_reference_genes"] == ["BRD2"]

    features_only = _generate_body(
        gene="BRD4", num_peptides=4, model=CAPABLE, contrastive=_guidance(),
    )
    assert "reference_pocket_features" in features_only
    for key in _GENE_KEYS:
        assert key not in features_only


# ─────────────────────────────────────────────────────────────────────────────
# INCAPABLE GENERATOR — refused, never silently ignored
# ─────────────────────────────────────────────────────────────────────────────


def test_a_named_panel_on_an_incapable_generator_is_refused_not_dropped():
    with pytest.raises(LigandAIValidationError) as ei:
        _generate_body(
            gene="BRD4", num_peptides=4, model=INCAPABLE,
            avoid_paralogs=["BRD2"],
        )
    msg = str(ei.value)
    assert "cfg_references_not_supported" in msg
    assert "v6.9.21" in msg or "v6.9.22" in msg, "must name a capable checkpoint"


def test_an_empty_panel_on_an_incapable_generator_is_still_fine():
    """Nothing was named, so there is nothing the generator cannot honour."""
    body = _generate_body(
        gene="BRD4", num_peptides=4, model=INCAPABLE, avoid_paralogs=[],
    )
    for key in _GENE_KEYS:
        assert key not in body


# ─────────────────────────────────────────────────────────────────────────────
# SERVER REFUSAL CODES -> a typed error, not a generic 4xx
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("code", list(LigandAICfgReferencesRefused.CODES))
@pytest.mark.parametrize("field", ["code", "error_code"])
@pytest.mark.parametrize("status", [400, 422])
def test_both_refusal_codes_surface_as_a_typed_error(code, field, status):
    err = error_from_response(status, {field: code, "message": "refused"})
    assert isinstance(err, LigandAICfgReferencesRefused), (
        f"{code} via {field!r} degraded to {type(err).__name__} — the caller "
        f"cannot branch on which refusal happened"
    )
    assert err.code == code, "the code must survive onto the attribute"
    assert isinstance(err, LigandAIInvalidConfig)


def test_the_two_codes_are_distinguishable_because_the_remedies_are_opposite():
    not_supported = error_from_response(
        400, {"code": "cfg_references_not_supported", "message": "x"}
    )
    both_supplied = error_from_response(
        400, {"code": "cfg_reference_genes_with_explicit_features", "message": "y"}
    )
    assert not_supported.code != both_supplied.code
    assert type(not_supported) is type(both_supplied)


def test_an_unrelated_refusal_code_is_not_relabeled_as_a_cfg_refusal():
    """Negative control on the dispatch itself: the new branch must not swallow
    every coded 4xx on its way past.
    """
    err = error_from_response(400, {"code": "some_other_thing", "message": "z"})
    assert not isinstance(err, LigandAICfgReferencesRefused)


def test_the_refusal_class_is_exported_from_the_package_root():
    import ligandai

    assert ligandai.LigandAICfgReferencesRefused is LigandAICfgReferencesRefused
    assert "LigandAICfgReferencesRefused" in ligandai.__all__


# ─────────────────────────────────────────────────────────────────────────────
# THE HONEST-RESULT NOTE — pinned so a later softening fails a gate
# ─────────────────────────────────────────────────────────────────────────────


def test_the_measured_result_note_is_carried_verbatim():
    assert PARALOG_CONDITIONING_NOTE == (
        "In our controlled test it was not more selective than "
        "generate-and-filter. Use it as a different experiment, not as an "
        "upgrade."
    )


def test_no_surface_claims_this_improves_selectivity():
    """⛔ Andre's standing instruction: do not ship copy implying it improves
    selectivity. Conditioning did NOT beat generate-and-filter and all arms sat
    inside the permutation null band.
    """
    from ligandai import contrastive as _c
    from ligandai.resources import peptides as _p

    for mod in (_c, _p):
        text = (mod.__doc__ or "") + "".join(
            (getattr(obj, "__doc__", "") or "")
            for obj in vars(mod).values()
            if callable(obj)
        )
        low = text.lower()
        for claim in (
            "more selective than generate-and-filter",
            "improves selectivity",
            "better selectivity",
        ):
            # The one legal occurrence is the measured NEGATIVE result.
            bad = low.count(claim) - low.count("not " + claim)
            assert bad <= 0, f"{mod.__name__} claims {claim!r}"


def test_the_docstring_says_off_does_not_mean_no_conditioning():
    doc = normalize_avoid_paralogs.__doc__ or ""
    assert "does NOT mean" in doc and "no conditioning" in doc.lower()
    assert "platform" in doc.lower()
