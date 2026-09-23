# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Run-until-criteria campaigns — ``client.objectives``.  [bd-LIGANDAI_ALPHA_V2-zwiz5.4]

This is a DIFFERENT, richer vocabulary from :class:`~ligandai.resources.goals.Goals`.
``Goals.estimate()``/``campaign_start()`` speak the ``hit-target-mode`` vocabulary
(``targetHits``/``criterion``/``maxRounds``/``maxCredits``) against
``/api/v1/goal-campaign/*`` and ``/api/objectives/estimate``. This module speaks the
production ``shared/campaign-criteria`` vocabulary the WEBSITE's objective runner
uses, against ``/api/ptf/campaigns*``. Nothing here duplicates that vocabulary's
semantics — every wire field name mirrors the server's own camelCase name, and every
verdict/progress number is read back verbatim, never recomputed client-side.

Endpoint mapping
-----------------

- :meth:`Objectives.estimate`      → ``POST /api/ptf/campaigns/estimate``
- :meth:`Objectives.start`         → ``POST /api/ptf/campaigns``
- :meth:`Objectives.declare`       → one-call ``estimate()`` + accept-or-refuse + ``start()``
- :meth:`Objectives.list`          → ``GET  /api/ptf/campaigns``
- :meth:`Objectives.get`           → ``GET  /api/ptf/campaigns/{id}``
- :meth:`Objectives.status`        → typed wrapper over :meth:`get`
- :meth:`Objectives.stop`          → ``POST /api/ptf/campaigns/{id}/stop``
- :meth:`Objectives.raise_ceiling` → ``POST /api/ptf/campaigns/{id}/raise-ceiling``

⛔ THE QUOTE-ACCEPTANCE GUARANTEE
---------------------------------
A customer is never charged more than the estimate they accepted, without a NEW
acceptance. ``estimate()`` is read-only and hands back a signed, expiring ``quote``
(``token``/``expiresAt``/``ceilingCredits``/``firstRoundCredits``/``inputsHash`` — see
:class:`CampaignQuote`). ``start()`` and ``raise_ceiling()`` (raising the ceiling is
itself a NEW spend authorization) both REQUIRE one and forward its ``token`` on the
wire as ``quoteToken``.

The SDK enforces this the same way the server does: it never silently refetches a
quote and relaunches at a new price. When the server reports the accepted quote is
stale — ``quote_expired`` / ``quote_inputs_changed`` / ``quote_superseded`` (409), or
missing/unrecognized — ``quote_required`` / ``quote_invalid`` (400) — this module
raises an actionable exception carrying BOTH the price you accepted and the server's
current price (``exc.response["acceptedQuote"]`` vs ``exc.response["quote"]`` /
``exc.response["estimate"]``) and stops. You decide whether to accept the new number;
nothing here decides it for you. Likewise :meth:`declare` — the one-call convenience
driver — takes a REQUIRED ``accept_up_to`` ceiling with no default and refuses to
start anything the estimate prices above it.

⛔ MEASUREMENT RULES BAKED INTO ``kd_selectivity_criterion_leg`` — READ BEFORE USE
-----------------------------------------------------------------------------------
- **peptide_ipsae is the interface gate**, checked on the ON-TARGET fold BEFORE any
  Kd is read (``on_target_ipsae_gate``, platform default 0.67). Never whole-complex
  ``ipsae``. A value above 1.0 on either column means the wrong column was plumbed
  in — the pre-fold predicted column emits values >1.0 and is never a valid source.
- **Kd comes from ``ptf_deltaforge_scores.kd_nm``, restricted to the V10-family
  scorers** (``deltaforge_v10_boltz2_calibrated_gbr``,
  ``deltaforge_v10_2_unified_parallel``). ⛔ NEVER ``ptf_fold_results.predicted_kd`` —
  that column pools at least two unit systems across eight scorer labels and is
  bimodal WITHIN a single label, so filtering by scorer does not rescue it.
- **Relaxation state is RECORDED PROVENANCE, never an asserted flag.** There is no
  ``do_relax=True`` quality gate on this codebase:
  ``compute_deltaforge_energy(..., do_relax=True)`` does not return a relaxed
  energy — it returns ``min(native_dG, relaxed_dG)``, a best-of-2 selection
  estimator with a downward bias, and the production writer of
  ``ptf_deltaforge_scores.kd_nm`` has no relax path at all. Asserting a relax flag
  would certify exactly the biased estimator this leg exists to keep out.
- **min(off-target Kd) / on-target Kd, and min-of-k over k off-targets NEEDS the
  null correction** (``null_corrected=True``), not a 1x null — a minimum over k
  comparators is a different statistic from a minimum over one, and comparing it to
  a threshold calibrated on k=1 systematically overstates selectivity.
- A missing/absent off-target fold is ``indeterminate``, never "selective" — an
  absent measurement is not evidence of selectivity.
- **A predicted-Kd ORDERING WITHIN one target is a reported number, not a
  validated affinity ranking** — the deployed Kd scorer has no measured-affinity
  evaluation on any row, so do not present "design A's Kd < design B's Kd, same
  target" as a validated claim. The CROSS-target ratio this leg actually computes
  (one binder, two receptors, ONE scorer) is a legitimate and different use and is
  not over-caveated by the above — but BOTH legs of the ratio must come from the
  SAME scorer, and whatever surfaces the underlying Kd numbers (``evaluation`` /
  ``ObjectiveStatus.raw``) should keep the design's length (the confound) visible
  beside it.

⛔ ``indeterminate`` IS NOT ``not_met``
----------------------------------------
"This design has not been measured against a leg" and "this design failed the leg"
are different claims. :class:`CriterionOutcome` and :class:`ObjectiveStatus` keep the
platform's own three-way ``LegVerdict`` (``"met"`` / ``"not_met"`` / ``"indeterminate"``,
from ``shared/campaign-criteria/types.ts``) intact end to end — nothing in this module
ever collapses it to a bool.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ligandai.errors import LigandAICreditError, LigandAIError, LigandAIValidationError
from ligandai.resources._base import AsyncResource, Resource

__all__ = [
    "AsyncObjectiveCampaign",
    "AsyncObjectives",
    "CampaignQuote",
    "CriterionOutcome",
    "ObjectiveCampaign",
    "ObjectiveStatus",
    "Objectives",
    "campaign_criteria",
    "ipsae_criterion_leg",
    "kd_selectivity_criterion_leg",
    "panel_comparator",
    "panel_completeness_criterion_leg",
    "seed_replication_criterion_leg",
    "units_clearing_all",
    "units_clearing_count",
    "units_clearing_fraction",
]

# ─── Wire-verdict discipline ─────────────────────────────────────────────────

#: The platform's own three-way verdict. ⛔ Never reduce this to a bool.
LEG_VERDICTS: tuple[str, ...] = ("met", "not_met", "indeterminate")

_VALID_COMPARATOR_ROLES = frozenset({"on_target", "isoform", "off_target"})
_VALID_UNITS_CLEARING_MODES = frozenset({"all", "count", "fraction"})
_VALID_IPSAE_STATISTICS = frozenset({"median", "best", "worst"})
_QUOTE_STALE_CODES = frozenset({"quote_expired", "quote_inputs_changed", "quote_superseded"})


# ─── Criterion-leg builders ───────────────────────────────────────────────────
# Thin, structural wire-shape helpers. They validate SHAPE (required fields,
# obvious ranges) so a typo is caught before a network round trip — exactly the
# level of client-side checking goals.py's _campaign_start_body already does for
# the goal-campaign vocabulary. They do NOT recompute verdicts, progress, cost
# projections, or scorer-provenance rules; those stay server-side and are read
# back, never re-derived.


def panel_comparator(
    comparator_id: str, *, label: str | None = None, role: str = "off_target"
) -> dict[str, Any]:
    """One comparator receptor in a selectivity/completeness panel.

    Wire shape: ``{"id", "label", "role"}`` — mirrors the server's
    ``PanelComparator`` (``shared/campaign-criteria/types.ts``). ``role`` is one
    of ``"on_target"``, ``"isoform"``, ``"off_target"``.
    """
    role_norm = str(role).strip().lower()
    if role_norm not in _VALID_COMPARATOR_ROLES:
        raise LigandAIValidationError(
            f"panel_comparator role must be one of {sorted(_VALID_COMPARATOR_ROLES)} — got {role!r}"
        )
    cid = str(comparator_id).strip()
    if not cid:
        raise LigandAIValidationError("panel_comparator needs a non-empty id")
    return {"id": cid, "label": str(label) if label else cid, "role": role_norm}


def ipsae_criterion_leg(*, min_peptide_ipsae: float, statistic: str = "median") -> dict[str, Any]:
    """Bar on ``peptide_ipsae`` (never whole-complex ``ipsae``).

    ``statistic`` picks which of a design's seed-units is compared to the bar:

    - ``"median"`` — stable as units accumulate.
    - ``"best"``   — ⛔ RISES monotonically with folding effort; a design can be
      promoted purely by being folded more.
    - ``"worst"``  — ⛔ FALLS monotonically with folding effort (measured: 42%
      pass at 2 units, 0% at 9). Prefer :func:`seed_replication_criterion_leg`
      with a ``"fraction"`` units-clearing rule when the intent is "holds up
      across seeds".
    """
    stat = str(statistic).strip().lower()
    if stat not in _VALID_IPSAE_STATISTICS:
        raise LigandAIValidationError(
            f"statistic must be one of {sorted(_VALID_IPSAE_STATISTICS)} — got {statistic!r}"
        )
    bar = float(min_peptide_ipsae)
    if not (0.0 <= bar <= 1.0):
        raise LigandAIValidationError("min_peptide_ipsae must be in [0, 1]")
    return {"kind": "peptide_ipsae", "minPeptideIpsae": bar, "statistic": stat}


def units_clearing_all() -> dict[str, Any]:
    """Every seed-unit must clear the bar. ⛔ Unit-count dependent — see
    :func:`ipsae_criterion_leg`'s ``"worst"`` warning; the same monotonic drift
    applies to a pure ``"all"`` rule as units accumulate."""
    return {"mode": "all"}


def units_clearing_count(k: int) -> dict[str, Any]:
    """At least ``k`` seed-units must clear the bar. Stable: a design cannot be
    demoted by extra folding, only promoted."""
    if k < 1:
        raise LigandAIValidationError("units_clearing_count requires k >= 1")
    return {"mode": "count", "k": int(k)}


def units_clearing_fraction(f: float) -> dict[str, Any]:
    """At least ``ceil(f * units)`` seed-units must clear the bar. Stable in the
    same sense as :func:`units_clearing_count`, and the recommended way to say
    "holds up across seeds"."""
    if not (0.0 < f <= 1.0):
        raise LigandAIValidationError("units_clearing_fraction requires 0 < f <= 1")
    return {"mode": "fraction", "f": float(f)}


def seed_replication_criterion_leg(
    *,
    min_independent_seed_units: int,
    unit_peptide_ipsae_bar: float,
    units_clearing: dict[str, Any],
) -> dict[str, Any]:
    """Replication across INDEPENDENT SEEDS — the criterion that separates a
    design that scored once from a design that holds up.

    ⛔ SEED-UNITS, NOT POSES. Four diffusion trajectories drawn under ONE seed
    satisfy ``min_independent_seed_units`` at 1, never at 4. Grounded rather than
    assumed: on a measured campaign 19/49 designs (38.8%) FLIPPED the 0.67 gate
    between two fresh seeds, so a one-seed verdict near the bar is close to a
    coin flip. Build ``units_clearing`` with :func:`units_clearing_all` /
    :func:`units_clearing_count` / :func:`units_clearing_fraction`.
    """
    if min_independent_seed_units < 1:
        raise LigandAIValidationError("min_independent_seed_units must be >= 1")
    bar = float(unit_peptide_ipsae_bar)
    if not (0.0 <= bar <= 1.0):
        raise LigandAIValidationError("unit_peptide_ipsae_bar must be in [0, 1]")
    if not isinstance(units_clearing, dict) or units_clearing.get("mode") not in _VALID_UNITS_CLEARING_MODES:
        raise LigandAIValidationError(
            "units_clearing must be built with units_clearing_all() / "
            "units_clearing_count() / units_clearing_fraction()"
        )
    return {
        "kind": "seed_replication",
        "minIndependentSeedUnits": int(min_independent_seed_units),
        "unitPeptideIpsaeBar": bar,
        "unitsClearing": units_clearing,
    }


def panel_completeness_criterion_leg(
    *,
    comparators: list[dict[str, Any]],
    required_legs: int | str = "all",
    require_selective_verdict: bool = False,
) -> dict[str, Any]:
    """Completeness of a design's comparator panel.

    Build ``comparators`` entries with :func:`panel_comparator`.
    ``require_selective_verdict=False`` (default) is a COVERAGE criterion —
    satisfied by measurement, whatever it found. ``True`` additionally requires
    the platform's own selectivity-gate verdict to be ``"selective"``, which is
    never returned for an absent leg — conflating the two is how an unmeasured
    design gets reported as selective.
    """
    if not comparators:
        raise LigandAIValidationError("panel_completeness needs at least one comparator")
    if required_legs != "all" and (not isinstance(required_legs, int) or required_legs < 1):
        raise LigandAIValidationError('required_legs must be "all" or an int >= 1')
    return {
        "kind": "panel_completeness",
        "comparators": list(comparators),
        "requiredLegs": required_legs,
        "requireSelectiveVerdict": bool(require_selective_verdict),
    }


def kd_selectivity_criterion_leg(
    *,
    off_targets: list[dict[str, Any]],
    min_fold_selectivity: float,
    null_corrected: bool = True,
    on_target_ipsae_gate: float = 0.67,
) -> dict[str, Any]:
    """Declare a Kd-selectivity acceptance leg.

    Wire shape: ``{"kind": "kd_selectivity", "offTargets", "minFoldSelectivity",
    "nullCorrected", "onTargetIpsaeGate"}`` — the server's OWN camelCase field
    names (``shared/campaign-criteria/types.ts`` ``KdSelectivityCriterionLeg``),
    never snake_cased on the wire even though this function's own arguments are.
    Build ``off_targets`` entries with :func:`panel_comparator`.

    ⛔ THE MEASUREMENT RULES THIS LEG GATES ON — read before use:

    - **Gating order is non-negotiable.** ``on_target_ipsae_gate`` (default
      0.67, the platform's own gate) is checked on the ON-TARGET fold's
      ``peptide_ipsae`` BEFORE any Kd is read. Below the gate there is no
      interface to score, and a design below it is ``indeterminate``, never
      ``not_met``.
    - **``peptide_ipsae`` ONLY, on BOTH sides** — never whole-complex ``ipsae``.
      A value above 1.0 on either column is the tell that the wrong column was
      plumbed in (the pre-fold predicted column emits values >1.0); such a leg
      resolves to ``indeterminate``, never gets silently clamped.
    - **Kd comes from ``ptf_deltaforge_scores.kd_nm``, restricted to the
      V10-family scorers** — ``deltaforge_v10_boltz2_calibrated_gbr`` and
      ``deltaforge_v10_2_unified_parallel``. ⛔ NEVER
      ``ptf_fold_results.predicted_kd``: that column pools at least two unit
      systems across eight scorer labels and is bimodal WITHIN a single label,
      so filtering by scorer does not rescue it.
    - **Relaxation state is RECORDED PROVENANCE, never an asserted flag.**
      There is no ``do_relax=True`` quality gate on this codebase — see the
      module docstring for why. An observation that cannot attest scorer +
      relaxation provenance is ``indeterminate`` for that Kd, never treated as
      clean.
    - **``null_corrected`` matters whenever ``off_targets`` has more than one
      entry.** A minimum over k off-targets is a DIFFERENT statistic from a
      minimum over one, and comparing an uncorrected min-of-k to a threshold
      calibrated on k=1 systematically OVERSTATES selectivity. Leave this
      ``True`` (default) unless you have a specific reason not to.
    - **A missing/absent off-target fold is ``indeterminate``, never
      "selective"** — an absent measurement is not evidence of selectivity.
      Because the leg's statistic is a MINIMUM over the panel, a MEASURED
      violation among completed folds is still a valid ``not_met`` even with
      another member missing — the missing member's unknown value can only
      lower the minimum further, never raise it above a bar already failed.
    - **This is a CROSS-target ratio (one binder, two receptors, ONE scorer) —
      a legitimate and different claim from a within-target Kd ORDERING**,
      which the deployed scorer has no measured-affinity evaluation to
      support and which this leg makes no attempt to validate. Both legs of
      the ratio must come from the SAME scorer, and the design's length (the
      confound that travels with any Kd-derived number) stays visible in the
      underlying evaluation payload rather than being summarized away.

    Parameters
    ----------
    off_targets
        The comparator panel this design must be selective over — a list of
        :func:`panel_comparator` dicts. Passed through byte-exact.
    min_fold_selectivity
        Minimum ``min(off-target Kd) / on-target Kd`` to satisfy this leg.
        > 1 means the on-target Kd must be the tighter binder by at least this
        factor.
    null_corrected
        Whether the min-of-k null correction is applied. Default ``True``.
    on_target_ipsae_gate
        ``peptide_ipsae`` floor on the on-target fold before ANY Kd is read
        for this leg. Default 0.67 — the platform's own gate, passed
        explicitly rather than assumed so it is visible on the wire.
    """
    if not off_targets:
        raise LigandAIValidationError(
            "kd_selectivity leg needs at least one off_target (build entries with panel_comparator())"
        )
    gate = float(on_target_ipsae_gate)
    if not (0.0 <= gate <= 1.0):
        raise LigandAIValidationError("on_target_ipsae_gate must be in [0, 1]")
    return {
        "kind": "kd_selectivity",
        "offTargets": list(off_targets),
        "minFoldSelectivity": float(min_fold_selectivity),
        "nullCorrected": bool(null_corrected),
        "onTargetIpsaeGate": gate,
    }


def campaign_criteria(
    *,
    target_qualified_designs: int,
    legs: list[dict[str, Any]],
    max_poses: int | None = None,
    max_rounds: int | None = None,
    max_credits: float | None = None,
    max_compute_usd: float | None = None,
    max_gpu_hours: float | None = None,
    min_qualified_gain_per_round: int | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Build the ``criteria`` object :meth:`Objectives.estimate` and
    :meth:`Objectives.start` take.

    ``legs`` entries come from :func:`ipsae_criterion_leg`,
    :func:`seed_replication_criterion_leg`, :func:`panel_completeness_criterion_leg`,
    and :func:`kd_selectivity_criterion_leg`.

    ⛔ AT LEAST ONE CEILING IS REQUIRED. A criterion with no ceiling is an
    open-ended order to spend a customer's money on a target that may not be
    reachable at all. Checked here client-side AND by the server's own
    ``validateCampaignCriteria()``.
    """
    if target_qualified_designs < 1:
        raise LigandAIValidationError("target_qualified_designs must be >= 1")
    if not legs:
        raise LigandAIValidationError("criteria needs at least one leg (see the *_criterion_leg builders)")
    ceilings: dict[str, Any] = {}
    if max_poses is not None:
        ceilings["maxPoses"] = int(max_poses)
    if max_rounds is not None:
        ceilings["maxRounds"] = int(max_rounds)
    if max_credits is not None:
        ceilings["maxCredits"] = float(max_credits)
    if max_compute_usd is not None:
        ceilings["maxComputeUsd"] = float(max_compute_usd)
    if max_gpu_hours is not None:
        ceilings["maxGpuHours"] = float(max_gpu_hours)
    if min_qualified_gain_per_round is not None:
        ceilings["minQualifiedGainPerRound"] = int(min_qualified_gain_per_round)
    if not ceilings:
        raise LigandAIValidationError(
            "criteria needs at least one ceiling (max_poses / max_rounds / "
            "max_credits / max_compute_usd / max_gpu_hours / "
            "min_qualified_gain_per_round) — a criterion with none is an "
            "open-ended spend order, and the server refuses it too."
        )
    out: dict[str, Any] = {
        "targetQualifiedDesigns": int(target_qualified_designs),
        "legs": list(legs),
        "ceilings": ceilings,
    }
    if description is not None:
        out["description"] = str(description)
    return out


# ─── Quote handle ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CampaignQuote:
    """The signed, expiring acceptance quote from :meth:`Objectives.estimate`.

    Pass this (or its ``.token``) as ``quote=`` to :meth:`Objectives.start` or
    :meth:`Objectives.raise_ceiling`. ⛔ THE GUARANTEE THIS CARRIES: the
    platform will never charge more than ``ceiling_credits`` for this campaign
    under THIS token — a price that moves after you accepted it needs a NEW
    quote and a NEW acceptance. Nothing in this SDK refetches one for you.
    """

    token: str
    expires_at: str
    ceiling_credits: int
    first_round_credits: int
    inputs_hash: str
    #: The raw wire dict this was built from — round-trips byte-exact.
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_wire(cls, raw: dict[str, Any]) -> CampaignQuote:
        if not isinstance(raw, dict) or not raw.get("token"):
            raise LigandAIValidationError(
                "not a campaign quote — expected the 'quote' block estimate() "
                f"returned (token/expiresAt/ceilingCredits/firstRoundCredits/"
                f"inputsHash), got {raw!r}"
            )
        return cls(
            token=str(raw["token"]),
            expires_at=str(raw.get("expiresAt", "")),
            ceiling_credits=int(raw.get("ceilingCredits") or 0),
            first_round_credits=int(raw.get("firstRoundCredits") or 0),
            inputs_hash=str(raw.get("inputsHash", "")),
            raw=dict(raw),
        )

    def to_wire(self) -> dict[str, Any]:
        """The exact dict to echo back on the wire."""
        if self.raw:
            return dict(self.raw)
        return {
            "token": self.token,
            "expiresAt": self.expires_at,
            "ceilingCredits": self.ceiling_credits,
            "firstRoundCredits": self.first_round_credits,
            "inputsHash": self.inputs_hash,
        }


def _quote_token(quote: CampaignQuote | dict[str, Any] | str) -> str:
    if isinstance(quote, CampaignQuote):
        return quote.token
    if isinstance(quote, dict):
        tok = quote.get("token")
        if not tok:
            raise LigandAIValidationError(
                "quote dict has no 'token' — pass the whole 'quote' block "
                "estimate() returned, or just its 'token' string"
            )
        return str(tok)
    if isinstance(quote, str) and quote.strip():
        return quote.strip()
    raise LigandAIValidationError(
        "quote must be the CampaignQuote (or raw 'quote' dict) returned by "
        "estimate(), or its token string directly"
    )


def _quote_dict(quote: CampaignQuote | dict[str, Any] | str) -> dict[str, Any] | None:
    if isinstance(quote, CampaignQuote):
        return quote.to_wire()
    if isinstance(quote, dict):
        return quote
    return None


# ─── Criteria roll-up (indeterminate ≠ not_met) ───────────────────────────────


@dataclass(frozen=True)
class CriterionOutcome:
    """One entry of the ``criteria`` roll-up on a campaign snapshot.

    ⛔ ``verdict`` is one of ``"met"`` / ``"not_met"`` / ``"indeterminate"`` — see
    :data:`LEG_VERDICTS`. NEVER collapse this to a bool. ``"indeterminate"``
    means the platform has not measured enough to judge this leg yet (an
    unfolded seed-unit, an unmeasured comparator panel, a Kd below the
    peptide_ipsae gate) — a DIFFERENT fact from ``"not_met"``, which means the
    leg was measured and failed. Collapsing the two silently reports an
    unmeasured design as either a pass or a fail.
    """

    kind: str
    verdict: str
    progress: float | None
    reasons: tuple[str, ...] = ()

    @classmethod
    def from_wire(cls, raw: dict[str, Any]) -> CriterionOutcome:
        verdict = str(raw.get("verdict") or "indeterminate")
        if verdict not in LEG_VERDICTS:
            verdict = "indeterminate"
        return cls(
            kind=str(raw.get("kind", "")),
            verdict=verdict,
            progress=raw.get("progress"),
            reasons=tuple(raw.get("reasons") or ()),
        )

    @property
    def is_met(self) -> bool:
        """True only for ``"met"``. ⛔ False for BOTH ``"not_met"`` AND
        ``"indeterminate"`` — read :attr:`verdict` itself before branching on
        this; this property cannot tell those two apart, and a caller who only
        checks ``is_met`` will silently treat an unmeasured leg as a plain
        failure."""
        return self.verdict == "met"

    @property
    def is_indeterminate(self) -> bool:
        return self.verdict == "indeterminate"


@dataclass(frozen=True)
class ObjectiveStatus:
    """Typed view over ``GET /api/ptf/campaigns/{id}``.

    ⛔ ``criteria`` keeps ``indeterminate`` separate from ``not_met`` on every
    entry — see :class:`CriterionOutcome`. Read :attr:`met`, :attr:`not_met`
    and :attr:`indeterminate` rather than reducing a leg to a bool.
    :attr:`raw` is the full untouched server response for anything not
    surfaced here (``campaign``, ``rounds``, ``position``, ``evaluation``'s
    full ``perDesign`` breakdown, etc).
    """

    campaign_id: str
    campaign: dict[str, Any]
    rounds: tuple[dict[str, Any], ...]
    #: The full ``CampaignEvaluation`` block, once the server emits one. ``None``
    #: on a server that has not started reporting per-design evaluation yet.
    evaluation: dict[str, Any] | None
    #: The ``criteria`` roll-up — one :class:`CriterionOutcome` per leg.
    criteria: tuple[CriterionOutcome, ...]
    ceiling_remaining: float | None
    credits_spent_from_ledger: float | None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def met(self) -> tuple[CriterionOutcome, ...]:
        return tuple(c for c in self.criteria if c.verdict == "met")

    @property
    def not_met(self) -> tuple[CriterionOutcome, ...]:
        return tuple(c for c in self.criteria if c.verdict == "not_met")

    @property
    def indeterminate(self) -> tuple[CriterionOutcome, ...]:
        return tuple(c for c in self.criteria if c.verdict == "indeterminate")

    @property
    def all_met(self) -> bool:
        """True only when EVERY criterion is ``"met"``.

        ⛔ False when even one is ``"indeterminate"`` — an unmeasured leg never
        counts as satisfied, and False here does NOT mean "failed"; check
        :attr:`not_met` to see whether anything actually failed versus is
        simply still unmeasured.
        """
        return bool(self.criteria) and all(c.verdict == "met" for c in self.criteria)

    @property
    def status(self) -> str | None:
        return self.campaign.get("status")

    @classmethod
    def from_wire(cls, campaign_id: str, raw: dict[str, Any]) -> ObjectiveStatus:
        campaign = raw.get("campaign") or {}
        rounds = raw.get("rounds") or []
        evaluation = raw.get("evaluation")
        criteria_raw = raw.get("criteria") or []
        return cls(
            campaign_id=campaign_id,
            campaign=campaign,
            rounds=tuple(rounds),
            evaluation=evaluation if isinstance(evaluation, dict) else None,
            criteria=tuple(
                CriterionOutcome.from_wire(c) for c in criteria_raw if isinstance(c, dict)
            ),
            ceiling_remaining=raw.get("ceilingRemaining"),
            credits_spent_from_ledger=raw.get("creditsSpentFromLedger"),
            raw=dict(raw),
        )


# ─── Error interpretation ─────────────────────────────────────────────────────


def _interpret_objective_error(
    exc: LigandAIError, *, accepted_quote: dict[str, Any] | None
) -> None:
    """Raise a clear, actionable exception for the objective-quote error codes.

    Precedent: :func:`~ligandai.resources.goals._interpret_campaign_400`.

    ⛔ NEVER refetches a quote or retries here — see :class:`CampaignQuote`'s
    docstring and the module-level "QUOTE-ACCEPTANCE GUARANTEE" section. This
    function only classifies the server's response and re-raises with the
    OLD (``accepted_quote``) and NEW (``exc.response["quote"]``/``["estimate"]``)
    numbers attached; the caller decides whether to accept the new price.
    Returns normally (raising nothing) when the error is not shaped like a
    quote/credit error, so the caller's own ``raise`` re-raises ``exc`` as-is.
    """
    payload = exc.response if isinstance(exc.response, dict) else {}
    err_code = str(payload.get("error") or "").strip().lower()
    status = exc.status_code

    if status == 402 and isinstance(exc, LigandAICreditError):
        est = payload.get("estimate") or {}
        required = est.get("firstRoundCredits")
        available = (est.get("position") or {}).get("available")
        raise LigandAICreditError(
            exc.message,
            required=int(required) if required is not None else exc.required,
            available=int(available) if available is not None else exc.available,
            code=exc.code,
            status_code=exc.status_code,
            request_id=exc.request_id,
            response=payload,
        ) from exc

    if status == 400 and err_code == "quote_required":
        raise LigandAIValidationError(
            "This objective needs an accepted quote before it can start. Call "
            "estimate() first and pass its 'quote' block (or quote['token']) as "
            f"quote= to start(). Server message: {exc.message}",
            code=exc.code,
            status_code=exc.status_code,
            request_id=exc.request_id,
            response=payload,
        ) from exc

    if status == 400 and err_code == "quote_invalid":
        raise LigandAIValidationError(
            f"The quote token was not accepted by the server ({exc.message}). It "
            "may be malformed, forged, or issued to a different account/campaign. "
            "Call estimate() again for a fresh quote.",
            code=exc.code,
            status_code=exc.status_code,
            request_id=exc.request_id,
            response=payload,
        ) from exc

    if status == 409 and err_code in _QUOTE_STALE_CODES:
        fresh_estimate = payload.get("estimate")
        fresh_quote = payload.get("quote")
        old_ceiling = (accepted_quote or {}).get("ceilingCredits")
        old_first_round = (accepted_quote or {}).get("firstRoundCredits")
        new_ceiling = (fresh_quote or {}).get("ceilingCredits")
        new_first_round = (fresh_quote or {}).get("firstRoundCredits")
        enriched_response = dict(payload)
        enriched_response["estimate"] = fresh_estimate
        enriched_response["quote"] = fresh_quote
        if accepted_quote is not None:
            enriched_response["acceptedQuote"] = accepted_quote
        raise LigandAIError(
            f"{err_code}: the price you accepted is no longer valid.\n"
            f"  accepted: ceiling={old_ceiling!r} credits, first round={old_first_round!r} credits\n"
            f"  current : ceiling={new_ceiling!r} credits, first round={new_first_round!r} credits\n"
            "The SDK will NOT relaunch at the new price automatically. Inspect "
            "exc.response['quote'] / exc.response['estimate'] (the fresh numbers) "
            "and exc.response['acceptedQuote'] (what you originally accepted), "
            "then call start() again with the fresh quote ONLY if the new "
            f"numbers are acceptable. Server message: {exc.message}",
            code=exc.code or err_code,
            status_code=exc.status_code,
            request_id=exc.request_id,
            response=enriched_response,
        ) from exc
    # Not shaped like a quote/credit error: return normally; caller re-raises exc.


# ─── Body builders ────────────────────────────────────────────────────────────


def _validate_criteria(criteria: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(criteria, dict) or not criteria.get("legs"):
        raise LigandAIValidationError(
            "criteria must be a dict with at least 'targetQualifiedDesigns' and "
            "'legs' — build one with campaign_criteria(), or pass the raw wire "
            "dict directly."
        )
    return criteria


def _estimate_body(
    *,
    criteria: dict[str, Any],
    target_gene: str | None,
    program_id: str | None,
    trajectories: int | None,
    sampling_steps: int | None,
    first_round_peptides: int | None,
    first_round_folds: int | None,
    spend_ceiling_credits: float | None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"criteria": _validate_criteria(criteria)}
    if target_gene is not None:
        body["targetGene"] = str(target_gene)
    if program_id is not None:
        body["programId"] = str(program_id)
    if trajectories is not None:
        body["trajectories"] = int(trajectories)
    if sampling_steps is not None:
        body["samplingSteps"] = int(sampling_steps)
    if first_round_peptides is not None:
        body["firstRoundPeptides"] = int(first_round_peptides)
    if first_round_folds is not None:
        body["firstRoundFolds"] = int(first_round_folds)
    if spend_ceiling_credits is not None:
        body["spendCeilingCredits"] = float(spend_ceiling_credits)
    return body


def _start_body(
    *,
    criteria: dict[str, Any],
    target_gene: str,
    program_id: str,
    quote_token: str,
    title: str | None,
    continuation_policy: dict[str, Any] | None,
    trajectories: int | None,
    sampling_steps: int | None,
    first_round_peptides: int | None,
    first_round_folds: int | None,
    spend_ceiling_credits: float | None,
) -> dict[str, Any]:
    tg = str(target_gene).strip()
    if not tg:
        raise LigandAIValidationError("target_gene is required to start an objective campaign")
    body: dict[str, Any] = {
        "criteria": _validate_criteria(criteria),
        "targetGene": tg,
        "programId": str(program_id),
        "quoteToken": quote_token,
    }
    if title is not None:
        body["title"] = str(title)
    if continuation_policy is not None:
        body["continuationPolicy"] = continuation_policy
    if trajectories is not None:
        body["trajectories"] = int(trajectories)
    if sampling_steps is not None:
        body["samplingSteps"] = int(sampling_steps)
    if first_round_peptides is not None:
        body["firstRoundPeptides"] = int(first_round_peptides)
    if first_round_folds is not None:
        body["firstRoundFolds"] = int(first_round_folds)
    if spend_ceiling_credits is not None:
        body["spendCeilingCredits"] = float(spend_ceiling_credits)
    return body


# ─── Campaign handles ─────────────────────────────────────────────────────────


class ObjectiveCampaign:
    """Handle for a running or completed objective campaign.

    Returned by :meth:`Objectives.declare`. Mirrors
    :class:`~ligandai.resources.goals.GoalCampaign`.
    """

    def __init__(
        self, campaign_id: str, objectives_resource: Objectives, *, target_gene: str | None = None
    ) -> None:
        self._campaign_id = campaign_id
        self._objectives = objectives_resource
        self._target_gene = target_gene

    @property
    def campaign_id(self) -> str:
        return self._campaign_id

    @property
    def target_gene(self) -> str | None:
        return self._target_gene

    def get(self) -> dict[str, Any]:
        """Raw snapshot. ``GET /api/ptf/campaigns/{campaign_id}``."""
        return self._objectives.get(self._campaign_id)

    def status(self) -> ObjectiveStatus:
        """Typed snapshot — see :meth:`Objectives.status`."""
        return self._objectives.status(self._campaign_id)

    def stop(self) -> dict[str, Any]:
        """Stop spending, keep everything. ``POST .../stop``."""
        return self._objectives.stop(self._campaign_id)

    def raise_ceiling(
        self, *, quote: CampaignQuote | dict[str, Any] | str, spend_ceiling_credits: float
    ) -> dict[str, Any]:
        """Resume a budget-stopped campaign at a new, freshly-accepted ceiling."""
        return self._objectives.raise_ceiling(
            self._campaign_id, quote=quote, spend_ceiling_credits=spend_ceiling_credits
        )

    def wait(self, *, timeout: float | None = None, poll_interval: float = 5.0) -> ObjectiveStatus:
        """Block until the campaign reaches a terminal state (``stopped`` /
        ``completed`` / ``failed``) and return the final typed snapshot."""
        import time

        from ligandai.errors import LigandAIWaitTimeout

        _TERMINAL = {"stopped", "completed", "failed"}
        deadline = (time.monotonic() + timeout) if timeout is not None else None
        while True:
            snap = self.status()
            if snap.status in _TERMINAL:
                return snap
            if deadline is not None and time.monotonic() >= deadline:
                raise LigandAIWaitTimeout(
                    f"Objective campaign {self._campaign_id!r} did not finish within "
                    f"{timeout}s (current status: {snap.status!r})"
                )
            time.sleep(poll_interval)

    def __repr__(self) -> str:
        return f"ObjectiveCampaign(campaign_id={self._campaign_id!r}, target_gene={self._target_gene!r})"


class AsyncObjectiveCampaign:
    """Async version of :class:`ObjectiveCampaign`."""

    def __init__(
        self, campaign_id: str, objectives_resource: AsyncObjectives, *, target_gene: str | None = None
    ) -> None:
        self._campaign_id = campaign_id
        self._objectives = objectives_resource
        self._target_gene = target_gene

    @property
    def campaign_id(self) -> str:
        return self._campaign_id

    @property
    def target_gene(self) -> str | None:
        return self._target_gene

    async def get(self) -> dict[str, Any]:
        return await self._objectives.get(self._campaign_id)

    async def status(self) -> ObjectiveStatus:
        return await self._objectives.status(self._campaign_id)

    async def stop(self) -> dict[str, Any]:
        return await self._objectives.stop(self._campaign_id)

    async def raise_ceiling(
        self, *, quote: CampaignQuote | dict[str, Any] | str, spend_ceiling_credits: float
    ) -> dict[str, Any]:
        return await self._objectives.raise_ceiling(
            self._campaign_id, quote=quote, spend_ceiling_credits=spend_ceiling_credits
        )

    async def wait(self, *, timeout: float | None = None, poll_interval: float = 5.0) -> ObjectiveStatus:
        import asyncio
        import time

        from ligandai.errors import LigandAIWaitTimeout

        _TERMINAL = {"stopped", "completed", "failed"}
        deadline = (time.monotonic() + timeout) if timeout is not None else None
        while True:
            snap = await self.status()
            if snap.status in _TERMINAL:
                return snap
            if deadline is not None and time.monotonic() >= deadline:
                raise LigandAIWaitTimeout(
                    f"Objective campaign {self._campaign_id!r} did not finish within "
                    f"{timeout}s (current status: {snap.status!r})"
                )
            await asyncio.sleep(poll_interval)

    def __repr__(self) -> str:
        return f"AsyncObjectiveCampaign(campaign_id={self._campaign_id!r}, target_gene={self._target_gene!r})"


# ─── Sync resource ────────────────────────────────────────────────────────────


class Objectives(Resource):
    """Sync ``client.objectives`` — run-until-criteria campaigns.

    See the module docstring for the quote-acceptance guarantee and the
    ``kd_selectivity`` measurement rules.
    """

    def estimate(
        self,
        *,
        criteria: dict[str, Any],
        target_gene: str | None = None,
        program_id: str | None = None,
        trajectories: int | None = None,
        sampling_steps: int | None = None,
        first_round_peptides: int | None = None,
        first_round_folds: int | None = None,
        spend_ceiling_credits: float | None = None,
    ) -> dict[str, Any]:
        """What will this objective cost, and what quote can I accept?

        ``POST /api/ptf/campaigns/estimate`` — read-only. No campaign is
        created and nothing is charged.

        Returns the server's response verbatim:
        ``{success, estimate, quote, criteriaDiagnostics, criteriaBlocking,
        defaultContinuationMode, defaultContinuationPolicy, tier}``. The
        ``quote`` block is what :meth:`start` requires — pass it (or its
        ``"token"``) as ``quote=``, or wrap it with
        :meth:`CampaignQuote.from_wire` for a typed handle.

        Parameters
        ----------
        criteria
            Wire-shaped ``CampaignCriteria`` — build with
            :func:`campaign_criteria`, or pass the raw dict directly.
        target_gene
            Optional here (required for :meth:`start`) — when supplied and the
            caller's program already holds folds for this gene, the estimate is
            computed against what has already been measured rather than from
            zero.
        program_id
            Program this objective would run against. Optional for a
            standalone estimate.
        trajectories, sampling_steps
            Boltz-2 settings the first round would use. Server defaults are
            1 trajectory / 50 sampling steps if omitted.
        first_round_peptides, first_round_folds
            Override the first round's size. The server's own default draws
            ten candidates per target design (a measured order-of-magnitude,
            not a round number).
        spend_ceiling_credits
            The ceiling you are considering accepting. Echoed back in
            ``quote.ceilingCredits`` if accepted at :meth:`start`.
        """
        body = _estimate_body(
            criteria=criteria,
            target_gene=target_gene,
            program_id=program_id,
            trajectories=trajectories,
            sampling_steps=sampling_steps,
            first_round_peptides=first_round_peptides,
            first_round_folds=first_round_folds,
            spend_ceiling_credits=spend_ceiling_credits,
        )
        return self._transport.request("POST", "/api/ptf/campaigns/estimate", json=body) or {}

    def start(
        self,
        *,
        criteria: dict[str, Any],
        target_gene: str,
        program_id: str,
        quote: CampaignQuote | dict[str, Any] | str,
        title: str | None = None,
        continuation_policy: dict[str, Any] | None = None,
        trajectories: int | None = None,
        sampling_steps: int | None = None,
        first_round_peptides: int | None = None,
        first_round_folds: int | None = None,
        spend_ceiling_credits: float | None = None,
    ) -> dict[str, Any]:
        """Commit the objective campaign. ``POST /api/ptf/campaigns``.

        Queued, not started — the background runner picks it up. REQUIRES an
        accepted ``quote`` from :meth:`estimate` — see the module-level
        QUOTE-ACCEPTANCE GUARANTEE.

        Raises
        ------
        LigandAIValidationError
            ``quote_required`` (no/missing quote) or ``quote_invalid``
            (unrecognized token) — both 400.
        LigandAIError
            ``quote_expired`` / ``quote_inputs_changed`` / ``quote_superseded``
            (409) — the accepted price is stale. Carries both the OLD
            (``exc.response["acceptedQuote"]``) and NEW
            (``exc.response["quote"]`` / ``["estimate"]``) numbers. This SDK
            never relaunches at the new price automatically — call
            :meth:`estimate` again and decide.
        LigandAICreditError
            402 ``insufficient_credits`` — the first round costs more than the
            available balance. ``exc.required`` / ``exc.available`` /
            ``exc.shortfall`` are populated from the server's estimate.
        """
        token = _quote_token(quote)
        accepted_quote_dict = _quote_dict(quote)
        body = _start_body(
            criteria=criteria,
            target_gene=target_gene,
            program_id=program_id,
            quote_token=token,
            title=title,
            continuation_policy=continuation_policy,
            trajectories=trajectories,
            sampling_steps=sampling_steps,
            first_round_peptides=first_round_peptides,
            first_round_folds=first_round_folds,
            spend_ceiling_credits=spend_ceiling_credits,
        )
        try:
            result = self._transport.request("POST", "/api/ptf/campaigns", json=body) or {}
        except LigandAIError as exc:
            _interpret_objective_error(exc, accepted_quote=accepted_quote_dict)
            raise
        return result

    def declare(
        self,
        *,
        criteria: dict[str, Any],
        target_gene: str,
        program_id: str,
        accept_up_to: float,
        title: str | None = None,
        continuation_policy: dict[str, Any] | None = None,
        trajectories: int | None = None,
        sampling_steps: int | None = None,
        first_round_peptides: int | None = None,
        first_round_folds: int | None = None,
    ) -> ObjectiveCampaign:
        """One-call: :meth:`estimate`, refuse anything priced above
        ``accept_up_to``, then :meth:`start` with the accepted quote.

        ⛔ ``accept_up_to`` IS REQUIRED AND HAS NO DEFAULT. This is the ONLY
        number this method will ever accept on your behalf — never the
        server's suggested ceiling, never a re-quoted higher price. Compared
        against the quote's ``ceiling_credits`` (the total this campaign is
        authorized to spend under this token — see :meth:`start`'s
        QUOTE-ACCEPTANCE GUARANTEE).

        Raises
        ------
        LigandAIValidationError
            If the estimated ceiling exceeds ``accept_up_to``, or the server
            has not deployed quote-acceptance (no ``quote`` in the estimate
            response). The estimate is attached as ``.response`` so the
            caller can inspect it and re-call with a higher ``accept_up_to``
            or a smaller objective — this method never silently proceeds at a
            higher price.
        """
        est = self.estimate(
            criteria=criteria,
            target_gene=target_gene,
            program_id=program_id,
            trajectories=trajectories,
            sampling_steps=sampling_steps,
            first_round_peptides=first_round_peptides,
            first_round_folds=first_round_folds,
            spend_ceiling_credits=accept_up_to,
        )
        quote_raw = est.get("quote")
        if not quote_raw:
            raise LigandAIValidationError(
                "estimate() returned no 'quote' block — the server has not "
                "deployed quote-acceptance yet, or the response shape changed. "
                "declare() refuses to start an objective with nothing to accept.",
                response=est,
            )
        quote = CampaignQuote.from_wire(quote_raw)
        if quote.ceiling_credits > accept_up_to:
            raise LigandAIValidationError(
                f"The quoted ceiling ({quote.ceiling_credits:,} credits) exceeds "
                f"accept_up_to ({accept_up_to:,.0f}). declare() will not accept a "
                "higher price than you named — call estimate() yourself, inspect "
                "the number, and either raise accept_up_to or shrink the objective.",
                response=est,
            )
        result = self.start(
            criteria=criteria,
            target_gene=target_gene,
            program_id=program_id,
            quote=quote,
            title=title,
            continuation_policy=continuation_policy,
            trajectories=trajectories,
            sampling_steps=sampling_steps,
            first_round_peptides=first_round_peptides,
            first_round_folds=first_round_folds,
            spend_ceiling_credits=accept_up_to,
        )
        campaign = result.get("campaign") or {}
        campaign_id = campaign.get("campaignId") or result.get("campaignId") or ""
        if not campaign_id:
            raise LigandAIError(
                "Server did not return a campaignId for the objective campaign", response=result
            )
        return ObjectiveCampaign(campaign_id, self, target_gene=target_gene)

    def list(self, *, limit: int = 50) -> dict[str, Any]:
        """This caller's objective campaigns, newest first.
        ``GET /api/ptf/campaigns``.
        """
        return self._transport.request("GET", f"/api/ptf/campaigns?limit={int(limit)}") or {}

    def get(self, campaign_id: str) -> dict[str, Any]:
        """Full raw snapshot. ``GET /api/ptf/campaigns/{campaign_id}``.

        ``{success, campaign, rounds, position, creditsSpentFromLedger,
        ceilingRemaining}``, plus (once the server emits them) ``evaluation``
        and the ``criteria`` per-leg roll-up. See :meth:`status` for a typed
        view that keeps ``indeterminate`` separate from ``not_met``.
        """
        return self._transport.request("GET", f"/api/ptf/campaigns/{campaign_id}") or {}

    def status(self, campaign_id: str) -> ObjectiveStatus:
        """Typed view of :meth:`get` — the per-criterion roll-up, WITHOUT
        collapsing ``indeterminate`` into ``not_met``. See
        :class:`ObjectiveStatus`.
        """
        raw = self.get(campaign_id)
        return ObjectiveStatus.from_wire(campaign_id, raw)

    def stop(self, campaign_id: str) -> dict[str, Any]:
        """Stop spending; destroys nothing. ``POST /api/ptf/campaigns/{campaign_id}/stop``.

        The round already in flight finishes and is kept. Every design the
        campaign produced remains in the program.
        """
        return self._transport.request("POST", f"/api/ptf/campaigns/{campaign_id}/stop") or {}

    def raise_ceiling(
        self,
        campaign_id: str,
        *,
        quote: CampaignQuote | dict[str, Any] | str,
        spend_ceiling_credits: float,
    ) -> dict[str, Any]:
        """Resume a budget-stopped campaign at a new ceiling.
        ``POST /api/ptf/campaigns/{campaign_id}/raise-ceiling``.

        Raising the ceiling is itself a NEW spend authorization, so it needs
        its OWN accepted quote — call :meth:`estimate` again with the higher
        ``spend_ceiling_credits`` and pass its quote here. The same
        QUOTE-ACCEPTANCE GUARANTEE and stale-quote exceptions documented on
        :meth:`start` apply identically.

        ⛔ Only resumes a campaign that stopped for BUDGET. A campaign that
        stopped because it plateaued (``not_converging``) is refused (409) by
        the server on purpose — more credits would not fix a campaign that
        stopped converging.
        """
        if spend_ceiling_credits < 1:
            raise LigandAIValidationError("spend_ceiling_credits must be >= 1")
        token = _quote_token(quote)
        accepted_quote_dict = _quote_dict(quote)
        body = {"spendCeilingCredits": float(spend_ceiling_credits), "quoteToken": token}
        try:
            return (
                self._transport.request(
                    "POST", f"/api/ptf/campaigns/{campaign_id}/raise-ceiling", json=body
                )
                or {}
            )
        except LigandAIError as exc:
            _interpret_objective_error(exc, accepted_quote=accepted_quote_dict)
            raise


# ─── Async resource ───────────────────────────────────────────────────────────


class AsyncObjectives(AsyncResource):
    """Async ``client.objectives`` — same API as :class:`Objectives`, fully
    awaitable. See :class:`Objectives` for complete documentation.
    """

    async def estimate(
        self,
        *,
        criteria: dict[str, Any],
        target_gene: str | None = None,
        program_id: str | None = None,
        trajectories: int | None = None,
        sampling_steps: int | None = None,
        first_round_peptides: int | None = None,
        first_round_folds: int | None = None,
        spend_ceiling_credits: float | None = None,
    ) -> dict[str, Any]:
        """Async: see :meth:`Objectives.estimate`."""
        body = _estimate_body(
            criteria=criteria,
            target_gene=target_gene,
            program_id=program_id,
            trajectories=trajectories,
            sampling_steps=sampling_steps,
            first_round_peptides=first_round_peptides,
            first_round_folds=first_round_folds,
            spend_ceiling_credits=spend_ceiling_credits,
        )
        return await self._transport.request("POST", "/api/ptf/campaigns/estimate", json=body) or {}

    async def start(
        self,
        *,
        criteria: dict[str, Any],
        target_gene: str,
        program_id: str,
        quote: CampaignQuote | dict[str, Any] | str,
        title: str | None = None,
        continuation_policy: dict[str, Any] | None = None,
        trajectories: int | None = None,
        sampling_steps: int | None = None,
        first_round_peptides: int | None = None,
        first_round_folds: int | None = None,
        spend_ceiling_credits: float | None = None,
    ) -> dict[str, Any]:
        """Async: see :meth:`Objectives.start`."""
        token = _quote_token(quote)
        accepted_quote_dict = _quote_dict(quote)
        body = _start_body(
            criteria=criteria,
            target_gene=target_gene,
            program_id=program_id,
            quote_token=token,
            title=title,
            continuation_policy=continuation_policy,
            trajectories=trajectories,
            sampling_steps=sampling_steps,
            first_round_peptides=first_round_peptides,
            first_round_folds=first_round_folds,
            spend_ceiling_credits=spend_ceiling_credits,
        )
        try:
            result = await self._transport.request("POST", "/api/ptf/campaigns", json=body) or {}
        except LigandAIError as exc:
            _interpret_objective_error(exc, accepted_quote=accepted_quote_dict)
            raise
        return result

    async def declare(
        self,
        *,
        criteria: dict[str, Any],
        target_gene: str,
        program_id: str,
        accept_up_to: float,
        title: str | None = None,
        continuation_policy: dict[str, Any] | None = None,
        trajectories: int | None = None,
        sampling_steps: int | None = None,
        first_round_peptides: int | None = None,
        first_round_folds: int | None = None,
    ) -> AsyncObjectiveCampaign:
        """Async: see :meth:`Objectives.declare`."""
        est = await self.estimate(
            criteria=criteria,
            target_gene=target_gene,
            program_id=program_id,
            trajectories=trajectories,
            sampling_steps=sampling_steps,
            first_round_peptides=first_round_peptides,
            first_round_folds=first_round_folds,
            spend_ceiling_credits=accept_up_to,
        )
        quote_raw = est.get("quote")
        if not quote_raw:
            raise LigandAIValidationError(
                "estimate() returned no 'quote' block — the server has not "
                "deployed quote-acceptance yet, or the response shape changed. "
                "declare() refuses to start an objective with nothing to accept.",
                response=est,
            )
        quote = CampaignQuote.from_wire(quote_raw)
        if quote.ceiling_credits > accept_up_to:
            raise LigandAIValidationError(
                f"The quoted ceiling ({quote.ceiling_credits:,} credits) exceeds "
                f"accept_up_to ({accept_up_to:,.0f}). declare() will not accept a "
                "higher price than you named — call estimate() yourself, inspect "
                "the number, and either raise accept_up_to or shrink the objective.",
                response=est,
            )
        result = await self.start(
            criteria=criteria,
            target_gene=target_gene,
            program_id=program_id,
            quote=quote,
            title=title,
            continuation_policy=continuation_policy,
            trajectories=trajectories,
            sampling_steps=sampling_steps,
            first_round_peptides=first_round_peptides,
            first_round_folds=first_round_folds,
            spend_ceiling_credits=accept_up_to,
        )
        campaign = result.get("campaign") or {}
        campaign_id = campaign.get("campaignId") or result.get("campaignId") or ""
        if not campaign_id:
            raise LigandAIError(
                "Server did not return a campaignId for the objective campaign", response=result
            )
        return AsyncObjectiveCampaign(campaign_id, self, target_gene=target_gene)

    async def list(self, *, limit: int = 50) -> dict[str, Any]:
        """Async: see :meth:`Objectives.list`."""
        return await self._transport.request("GET", f"/api/ptf/campaigns?limit={int(limit)}") or {}

    async def get(self, campaign_id: str) -> dict[str, Any]:
        """Async: see :meth:`Objectives.get`."""
        return await self._transport.request("GET", f"/api/ptf/campaigns/{campaign_id}") or {}

    async def status(self, campaign_id: str) -> ObjectiveStatus:
        """Async: see :meth:`Objectives.status`."""
        raw = await self.get(campaign_id)
        return ObjectiveStatus.from_wire(campaign_id, raw)

    async def stop(self, campaign_id: str) -> dict[str, Any]:
        """Async: see :meth:`Objectives.stop`."""
        return await self._transport.request("POST", f"/api/ptf/campaigns/{campaign_id}/stop") or {}

    async def raise_ceiling(
        self,
        campaign_id: str,
        *,
        quote: CampaignQuote | dict[str, Any] | str,
        spend_ceiling_credits: float,
    ) -> dict[str, Any]:
        """Async: see :meth:`Objectives.raise_ceiling`."""
        if spend_ceiling_credits < 1:
            raise LigandAIValidationError("spend_ceiling_credits must be >= 1")
        token = _quote_token(quote)
        accepted_quote_dict = _quote_dict(quote)
        body = {"spendCeilingCredits": float(spend_ceiling_credits), "quoteToken": token}
        try:
            return (
                await self._transport.request(
                    "POST", f"/api/ptf/campaigns/{campaign_id}/raise-ceiling", json=body
                )
                or {}
            )
        except LigandAIError as exc:
            _interpret_objective_error(exc, accepted_quote=accepted_quote_dict)
            raise
