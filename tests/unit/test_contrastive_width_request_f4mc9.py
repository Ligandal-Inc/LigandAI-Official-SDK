# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""The contrastive panel is buildable and forwardable. bd-dre-f4mc9.2.

Two defects met here and neither was in the SDK's own logic:

1. ``pocket_features(dim=107)`` ASSERTED a width it never ASKED for. The
   request went out with no width, the featurizer answered with its native 48,
   and the SDK — correctly — refused to pad. So every v6.9 panel failed a check
   that could not have passed, and the failure read as "the SDK cannot do
   contrastive". The width is now a request parameter, resolved server-side by
   the one implementation that owns width selection.

2. ``require_forwardable()`` raised UNCONDITIONALLY. That was true when it was
   written: the hop in front of the generation request model forwarded a fixed
   key list that omitted the references. That list now carries them. A refusal
   that outlives its cause keeps a working capability unreachable.

What must NOT have been lost in either fix is the set of refusals, because each
one is a silent-corruption mode rather than an error the run would report: an
empty panel, an all-zero reference, a width that is not the model's declared
width, a duplicate gene, and a generator with no CFG reference operator.

No network: the transport is a recorder.
"""

from __future__ import annotations

import pytest

from ligandai.contrastive import (
    ContrastiveGuidance,
    ContrastiveReference,
    PocketFeatureMatrix,
    V6_5_POCKET_FEATURE_DIM,
    V6_9_22_POCKET_FEATURE_DIM,
    V6_9_POCKET_FEATURE_DIM,
)
from ligandai.errors import (
    LigandAIError,
    LigandAINotFoundError,
    LigandAIParameterNotForwarded,
    LigandAIValidationError,
)
from ligandai.resources.campaigns import Campaigns


def _rows(dim: int, n: int = 6, fill: float = 0.25):
    return [[fill + 0.001 * i + 0.0001 * j for j in range(dim)] for i in range(n)]


def _ref(gene: str, dim: int = V6_9_POCKET_FEATURE_DIM) -> ContrastiveReference:
    return ContrastiveReference(gene=gene, features=PocketFeatureMatrix(rows=_rows(dim)))


class _Recorder:
    """Answers at whatever width the request ASKED for, and records the asks.

    Deliberately honours ``dim``: that is what the real featurizer does now, and
    a stub that ignored it would let the "assert a width you never requested"
    defect back in while the suite stayed green.
    """

    def __init__(self, native_dim: int = V6_5_POCKET_FEATURE_DIM, n_rows: int = 6):
        self.calls: list[tuple[str, str, dict]] = []
        self.native_dim = native_dim
        self.n_rows = n_rows

    def request(self, method, path, **kw):
        params = dict(kw.get("params") or {})
        self.calls.append((method, path, params))
        served = int(params.get("dim") or self.native_dim)
        return {
            "gene": path.rsplit("/", 1)[-1],
            "features": _rows(served, n=self.n_rows),
            "feature_dim": served,
            "native_feature_dim": self.native_dim,
            "pdb_code": "1ABC",
            "pocket_source": "pocket_extractor",
        }


class _FakeClient:
    pass


# ─────────────────────────────────────────────────────────────────────────────
# 1 — the width is REQUESTED, not merely asserted
# ─────────────────────────────────────────────────────────────────────────────


def test_an_explicit_dim_is_sent_to_the_featurizer():
    t = _Recorder()
    camp = Campaigns(transport=t, client=_FakeClient())  # type: ignore[arg-type]
    m = camp.pocket_features("BRD2", dim=V6_9_POCKET_FEATURE_DIM)
    assert m.dim == V6_9_POCKET_FEATURE_DIM
    assert t.calls[0][2].get("dim") == V6_9_POCKET_FEATURE_DIM


def test_a_model_resolves_to_its_declared_width_on_the_wire():
    for model, want in (
        ("v6.9.21", V6_9_POCKET_FEATURE_DIM),
        ("v6.9.22", V6_9_22_POCKET_FEATURE_DIM),
        ("v6.5.21", V6_5_POCKET_FEATURE_DIM),
    ):
        t = _Recorder()
        camp = Campaigns(transport=t, client=_FakeClient())  # type: ignore[arg-type]
        m = camp.pocket_features("BRD2", model=model)
        assert m.dim == want, model
        assert t.calls[0][2].get("dim") == want, model


def test_no_dim_and_no_known_model_asks_for_nothing():
    """A width nobody chose must NOT be pinned in the request.

    ``dim`` absent means "the featurizer's native width", which is the pre-P2
    behaviour and the only honest answer when the caller named neither a width
    nor a model this build knows.
    """
    t = _Recorder()
    camp = Campaigns(transport=t, client=_FakeClient())  # type: ignore[arg-type]
    m = camp.pocket_features("BRD2")
    assert "dim" not in t.calls[0][2]
    assert m.dim == V6_5_POCKET_FEATURE_DIM

    t2 = _Recorder()
    camp2 = Campaigns(transport=t2, client=_FakeClient())  # type: ignore[arg-type]
    camp2.pocket_features("BRD2", model="v7.0.0-unreleased")
    assert "dim" not in t2.calls[0][2]


def test_a_server_that_ignores_the_width_still_fails_loudly():
    """⛔ The client-side check is the backstop, not the mechanism.

    Requesting the width does not make the assertion redundant: a server that
    predates the parameter answers 48 to a request for 107, and padding it is
    the one thing that must never happen.
    """

    class _IgnoresDim(_Recorder):
        def request(self, method, path, **kw):
            self.calls.append((method, path, dict(kw.get("params") or {})))
            return {"gene": "BRD2", "features": _rows(48), "pdb_code": "1ABC"}

    camp = Campaigns(transport=_IgnoresDim(), client=_FakeClient())  # type: ignore[arg-type]
    with pytest.raises(LigandAIValidationError, match="NOT convertible"):
        camp.pocket_features("BRD2", dim=V6_9_POCKET_FEATURE_DIM)


# ─────────────────────────────────────────────────────────────────────────────
# 2 — contrastive_references builds a real panel at the model's width
# ─────────────────────────────────────────────────────────────────────────────


def test_contrastive_references_builds_a_107_panel_for_v6921():
    t = _Recorder()
    camp = Campaigns(transport=t, client=_FakeClient())  # type: ignore[arg-type]
    g = camp.contrastive_references(
        target="BRD4", paralogs=["BRD2", "BRD3", "BRDT"], model="v6.9.21",
    )
    assert g.genes == ["BRD2", "BRD3", "BRDT"]
    assert g.expected_dim == V6_9_POCKET_FEATURE_DIM
    assert all(r.dim == V6_9_POCKET_FEATURE_DIM for r in g.references)
    # One fetch per paralog, each asking for the model's width.
    assert [c[2].get("dim") for c in t.calls] == [V6_9_POCKET_FEATURE_DIM] * 3
    # And the panel is forwardable to the model it was built for.
    g.require_forwardable(model_id="v6.9.21")


def test_contrastive_references_builds_a_65_panel_for_v6922():
    t = _Recorder()
    camp = Campaigns(transport=t, client=_FakeClient())  # type: ignore[arg-type]
    g = camp.contrastive_references(target="BRD4", paralogs=["BRD2"], model="v6.9.22")
    assert g.expected_dim == V6_9_22_POCKET_FEATURE_DIM
    assert t.calls[0][2].get("dim") == V6_9_22_POCKET_FEATURE_DIM
    g.require_forwardable(model_id="v6.9.22")
    # ⛔ 65 and 107 are different vectors — a v6.9.22 panel is NOT a v6.9.21 one.
    with pytest.raises(LigandAIValidationError, match="not convertible"):
        g.require_forwardable(model_id="v6.9.21")


def test_the_v65_line_still_refuses_a_panel():
    """The refusal that must survive the guard rewrite.

    v6.5 has no CFG reference operator, so a panel sent to it is read by
    nothing. The server rejects it as ``cfg_references_not_supported``; the
    point of refusing here is that the caller finds out before GPU spend.
    """
    g = ContrastiveGuidance([_ref("BRD2")])
    with pytest.raises(LigandAIValidationError, match="does not use a CFG reference"):
        g.require_forwardable(model_id="v6.5.21")
    # ⛔ And the forwarding override must not be able to push it through — it
    # answers a question about the SERVER, never about the model.
    with pytest.raises(LigandAIValidationError, match="does not use a CFG reference"):
        g.require_forwardable(model_id="v6.5.21", allow_unforwarded=True)


def test_an_unknown_model_is_forwarded_not_vetoed():
    # A published SDK cannot know a model released after it was built.
    ContrastiveGuidance([_ref("BRD2")]).require_forwardable(model_id="v7.0.0-unreleased")


# ─────────────────────────────────────────────────────────────────────────────
# 3 — check_invariants: the refusals survive a panel that was tampered with
# ─────────────────────────────────────────────────────────────────────────────
#
# A frozen dataclass is not tamper-proof — object.__setattr__ reaches straight
# past __init__ — and a campaign loop rebuilds panels between rounds. Every
# case below would otherwise reach the worker as a well-formed request.


def _tamper(g: ContrastiveGuidance, refs) -> ContrastiveGuidance:
    object.__setattr__(g, "references", tuple(refs))
    return g


def test_a_panel_emptied_after_construction_is_caught():
    g = _tamper(ContrastiveGuidance([_ref("BRD2")]), [])
    with pytest.raises(LigandAIValidationError, match="empty"):
        g.require_forwardable()


def test_an_all_zero_reference_injected_after_construction_is_caught():
    g = ContrastiveGuidance([_ref("BRD2")])
    zero = ContrastiveReference(gene="BRD3", features=PocketFeatureMatrix(rows=_rows(107)))
    object.__setattr__(
        zero.features, "rows", [[0.0] * V6_9_POCKET_FEATURE_DIM for _ in range(4)]
    )
    g = _tamper(g, [*g.references, zero])
    with pytest.raises(LigandAIValidationError, match="all zeros"):
        g.require_forwardable()


def test_a_width_drift_introduced_after_construction_is_caught():
    g = ContrastiveGuidance([_ref("BRD2")])
    g = _tamper(g, [*g.references, _ref("BRD3", dim=48)])
    with pytest.raises(LigandAIValidationError, match="NOT convertible"):
        g.require_forwardable()


def test_a_duplicate_gene_introduced_after_construction_is_caught():
    g = ContrastiveGuidance([_ref("BRD2")])
    g = _tamper(g, [*g.references, _ref("brd2")])
    with pytest.raises(LigandAIValidationError, match="duplicate"):
        g.require_forwardable()


def test_a_non_paralog_mean_operator_introduced_after_construction_is_caught():
    g = ContrastiveGuidance([_ref("BRD2")])
    object.__setattr__(g, "cfg_reference", "uncond")
    with pytest.raises(LigandAIValidationError, match="paralog_mean"):
        g.require_forwardable()


# ─────────────────────────────────────────────────────────────────────────────
# 4 — the request body actually carries the panel
# ─────────────────────────────────────────────────────────────────────────────


def _generate_body(*, model=None, **kw):
    """Build a real request body through the ONE builder both generate() paths use.

    Every parameter generate() exposes takes generate()'s OWN default rather
    than one invented here, so this helper cannot drift into exercising a shape
    no caller can produce; the few internal bookkeeping params generate()
    computes for itself are passed as None. ``model=`` rather than a pre-resolved generator id on purpose:
    _generation_body resolves the generator, and skipping that resolution would
    skip the model/guidance compatibility check the panel check sits next to.
    """
    import inspect

    from ligandai.resources.peptides import Peptides, _generation_body

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
            # Internal bookkeeping params (deprecation-alias trails and the
            # like) that generate() computes rather than exposes. None is what
            # "the caller said nothing" looks like for all of them.
            args[name] = None
    args["model"] = model
    args.update(kw)
    return _generation_body(**args)


def test_a_v69_body_carries_the_panel_under_both_spellings():
    g = ContrastiveGuidance([_ref("BRD2"), _ref("BRD3")])
    body = _generate_body(
        gene="BRD4", num_peptides=4, model="v6.9.21", contrastive=g,
    )
    for key in ("reference_pocket_features", "referencePocketFeatures"):
        assert key in body, key
        assert len(body[key]) == 2
        assert len(body[key][0][0]) == V6_9_POCKET_FEATURE_DIM
    # ⛔ Names read by nothing are not emitted — a request key that looks like
    # configuration and is discarded is the defect this module exists to stop.
    assert "cfg_reference" not in body
    assert "pocket_feature_dim" not in body


def test_a_body_without_a_panel_carries_no_reference_keys():
    body = _generate_body(gene="BRD4", num_peptides=4, model="v6.9.21")
    assert "reference_pocket_features" not in body
    assert "referencePocketFeatures" not in body


def test_a_v65_body_with_a_panel_never_gets_built():
    g = ContrastiveGuidance([_ref("BRD2")])
    with pytest.raises(LigandAIValidationError, match="does not use a CFG reference"):
        _generate_body(
            gene="BRD4", num_peptides=4, model="v6.5.21", contrastive=g,
        )


# ─────────────────────────────────────────────────────────────────────────────
# 5 — "I could not look" vs "there is nothing there"
# ─────────────────────────────────────────────────────────────────────────────
#
# The mounted featurizer and an unmounted route both answer 404. They are
# different facts with different fixes, and they shared one error message that
# blamed the platform for an unresolvable gene name.


def test_a_gene_the_featurizer_cannot_resolve_is_not_reported_as_a_missing_route():
    class _NamedFailure:
        def request(self, method, path, **kw):
            raise LigandAINotFoundError(
                "not found",
                status_code=404,
                response={
                    "error": "pocket_features_unavailable",
                    "gene": "BRD4ET",
                    "message": "Could not extract pocket features for BRD4ET",
                },
            )

    camp = Campaigns(transport=_NamedFailure(), client=_FakeClient())  # type: ignore[arg-type]
    with pytest.raises(LigandAIError) as exc:
        camp.pocket_features("BRD4ET", dim=V6_9_POCKET_FEATURE_DIM)
    assert not isinstance(exc.value, LigandAIParameterNotForwarded)
    assert "gene SYMBOL" in str(exc.value)


def test_a_bare_404_is_still_reported_as_an_unmounted_route():
    class _BareNotFound:
        def request(self, method, path, **kw):
            raise LigandAINotFoundError("not found", status_code=404)

    camp = Campaigns(transport=_BareNotFound(), client=_FakeClient())  # type: ignore[arg-type]
    with pytest.raises(LigandAIParameterNotForwarded) as exc:
        camp.pocket_features("BRD2", dim=V6_9_POCKET_FEATURE_DIM)
    assert exc.value.parameter == "pocket_features"


def test_an_spa_shell_is_still_reported_as_an_unmounted_route():
    class _SpaShell:
        def request(self, method, path, **kw):
            return {"index": "<!doctype html>"}

    camp = Campaigns(transport=_SpaShell(), client=_FakeClient())  # type: ignore[arg-type]
    with pytest.raises(LigandAIParameterNotForwarded):
        camp.pocket_features("BRD2", dim=V6_9_POCKET_FEATURE_DIM)


def test_v6_5_24_pocket_width_is_65_and_not_a_cfg_reference_model():
    from ligandai.contrastive import model_pocket_feature_dim, model_takes_cfg_reference

    assert model_pocket_feature_dim("v6.5.24") == 65
    assert model_takes_cfg_reference("v6.5.24") is False
