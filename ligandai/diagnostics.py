# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Structured, machine-readable diagnostics — the SDK half of the platform
diagnostics contract.  [bd-LIGANDAI_ALPHA_V2-x4vt8.3.2]

WHY THIS EXISTS
    A customer excluded surface residues on their target. The exclusion was
    silently a no-op (a residue-numbering bug, since fixed) and 25/25 designed
    peptides bound the excluded region. **Nothing anywhere in the product told
    them** — not the SDK, not the API response, not the website. They found out
    by eyeballing structures in a viewer. The numbering bug was the shallow
    defect; the SILENCE was the deep one.

    This module makes server-side diagnostic signal reach a Python SDK user, as
    data they can branch on — not just as a ``warnings.warn`` they may have
    filtered away. Every job now exposes :attr:`~ligandai.jobs.Job.diagnostics`,
    and every client-side targeting validation returns a
    :class:`~ligandai._targeting.TargetingValidationResult` whose
    ``.diagnostics`` carries the same envelopes.

THE WIRE ENVELOPE (snake_case, identical across FastAPI / Express / SSE / REST)::

    {
      "schema_version": 1,
      "code": "avoid_residues_none_resolved",
      "severity": "info" | "warning" | "error",
      "stage": "request" | "generate" | "fold" | "results" | "client",
      "title": "None of your excluded residues were found",
      "message": "11 of 12 residues you asked to avoid ...",
      "remediation": "Residue numbers must use PDB author numbering ...",
      "details": {...},
      "doc_anchor": "sdk/python/allosteric-targeting#event-avoid",
      "occurred_at": "2026-08-04T18:22:01Z",
      "subject_id": "design_17"      // optional
    }

DESIGN RULES (these are the contract, not preferences)
    1. ``code`` is the join key across FastAPI, Express, both SDKs, and the
       website. Adding a code is additive. Renaming or removing one is a
       breaking wire change.
    2. **UNKNOWN CODES MUST PASS THROUGH UNCHANGED.** This layer never filters
       to a known-code allowlist — a diagnostic the SDK has never heard of is
       exactly the diagnostic most likely to matter, and dropping it is the
       precise failure mode this module exists to prevent. The same applies to
       unknown ``severity`` values and unknown envelope keys (preserved in
       :attr:`Diagnostic.raw`).
    3. The envelope's own ``severity`` is authoritative; the registry supplies
       only the default, so a caller may escalate without inventing a code.
    4. Nothing in here may raise into a request or stream path. A diagnostics
       layer that can fail a job is worse than no diagnostics layer. Every
       public helper is defensive.

REGISTRY PROVENANCE
    :data:`DIAGNOSTIC_CODES` is a **copy** of the platform registry
    (``shared/warnings.json`` in the server repo, also read by
    ``fastapi_backend/core/warnings.py`` and ``shared/warnings.ts``). It is
    copied rather than imported so the published SDK has no build-time or
    runtime dependency on the server repo. Every code, severity, stage, title,
    remediation and doc anchor is byte-identical to that file, and
    ``test_registry_is_byte_identical_to_shared_warnings_json`` fails the build
    if the two ever drift. Entries below the ``CLIENT-ONLY`` marker are the ones
    this SDK RAISES itself (before any HTTP call); they live in the shared
    registry like every other code, because a code only one layer knows about is
    a code the other layers will drop.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

__all__ = [
    "DIAGNOSTIC_CODES",
    "DIAGNOSTIC_EVENT",
    "DIAGNOSTIC_SCHEMA_VERSION",
    "SEVERITIES",
    "STAGES",
    "Diagnostic",
    "diagnostic_from_payload",
    "diagnostic_key",
    "diagnostics_from_container",
    "is_diagnostic_envelope",
    "is_targeting_code",
    "known_codes",
    "make_diagnostic",
    "merge_diagnostics",
    "severity_rank",
    "spec_for",
    "worst_severity",
]

#: Canonical SSE event name for one diagnostic envelope. One event = one
#: :class:`Diagnostic`.
DIAGNOSTIC_EVENT = "diagnostic"

DIAGNOSTIC_SCHEMA_VERSION = 1

SEVERITIES = ("info", "warning", "error")
STAGES = ("request", "generate", "fold", "results", "client")

_SEVERITY_ORDER: dict[str, int] = {"info": 0, "warning": 1, "error": 2}

#: Copy of ``shared/warnings.json``'s ``codes`` map. See REGISTRY PROVENANCE
#: above — keep byte-identical with that file for every shared code.
DIAGNOSTIC_CODES: dict[str, dict[str, str]] = {
    "avoid_residues_unresolved": {
        "severity": "warning",
        "stage": "generate",
        "title": "Some excluded residues were not found",
        "remediation": "Residue numbers must match the receptor structure's PDB author numbering, including the chain ID. Check the numbering of the structure you targeted and re-run with corrected residue numbers.",
        "doc_anchor": "sdk/python/allosteric-targeting#event-avoid",
    },
    "avoid_residues_none_resolved": {
        "severity": "error",
        "stage": "generate",
        "title": "None of your excluded residues were found",
        "remediation": "Every residue number you asked to avoid is absent from the receptor's featurized residue set - almost always a numbering mismatch (PDB author numbering vs. sequence index, or the wrong chain). Re-run with residue numbers taken from the structure itself so the request describes the residues you meant. Correcting the numbering will not, on its own, keep designs off that surface: row deletion is a measured no-op on the generated sequence distribution even when every residue resolves. Where a design actually binds is established by folding it and checking contacts.",
        "doc_anchor": "sdk/python/allosteric-targeting#event-avoid",
    },
    "target_residues_unresolved": {
        "severity": "warning",
        "stage": "generate",
        "title": "Some target residues were not found",
        "remediation": "Residue numbers must match the receptor structure's PDB author numbering, including the chain ID. The run proceeded using only the residues that resolved.",
        "doc_anchor": "sdk/python/allosteric-targeting#event-target",
    },
    "target_residues_none_resolved": {
        "severity": "error",
        "stage": "generate",
        "title": "None of your target residues were found",
        "remediation": "Targeting had NO EFFECT: generation fell back to the unfiltered pocket instead of conditioning on the site you asked for. Re-run with residue numbers taken from the structure's own author numbering.",
        "doc_anchor": "sdk/python/allosteric-targeting#event-target",
    },
    "degenerate_pocket_after_avoid": {
        "severity": "error",
        "stage": "generate",
        "title": "Exclusion left almost no pocket to condition on",
        "remediation": "After removing the excluded residues and their packing shell, too few pocket residues remain to condition generation reliably. This does not by itself keep designs off the excluded surface — verify on the folded structure. Widen the target face, narrow the avoid set, or reduce avoid_shell_angstrom.",
        "doc_anchor": "sdk/python/allosteric-targeting#event-warning",
    },
    "avoid_conformance_violation": {
        "severity": "warning",
        "stage": "fold",
        "title": "Designs contacted residues you excluded",
        "remediation": "Excluding residues does not steer generation. Removing them from the pocket features leaves the sequence distribution unchanged, so nothing at generation time keeps a design off the excluded region. Selecting residues to target does change which sequences are produced, but it has not been shown to aim a design at the face you name, so it is not a substitute for this check. Use the measurement instead: generate, fold, then rank or reject designs on the contacts reported here before synthesis. Re-folding with fold_pocket_residues set biases the pose toward a chosen site, but that pocket constraint is a soft prior rather than a geometric restraint, so re-check the contacts on the re-folded complex.",
        "doc_anchor": "sdk/python/allosteric-targeting#prior-not-constraint",
    },
    "avoid_conformance_clean": {
        "severity": "info",
        "stage": "fold",
        "title": "Designs avoided the excluded residues",
        "remediation": "",
        "doc_anchor": "sdk/python/allosteric-targeting#prior-not-constraint",
    },
    "avoid_conformance_unavailable": {
        "severity": "info",
        "stage": "fold",
        "title": "Exclusion could not be verified on the structure",
        "remediation": "The folded complex was not available for a contact check, so the exclusion was not verified. Inspect the structure, or re-run the conformance check on the finished complex.",
        "doc_anchor": "sdk/python/allosteric-targeting#prior-not-constraint",
    },
    "param_ignored": {
        "severity": "warning",
        "stage": "request",
        "title": "A parameter you set was ignored",
        "remediation": "The named parameter is accepted by the request schema but does not change pipeline behavior. Remove it from your request, or use the documented parameter that does apply.",
        "doc_anchor": "sdk/python/api-reference",
    },
    "param_dropped_in_transport": {
        "severity": "warning",
        "stage": "request",
        "title": "A parameter you set did not reach the pipeline",
        "remediation": "The named parameter was lost between API layers, so the server default was used instead. This is a platform bug - the value you sent was not applied.",
        "doc_anchor": "sdk/python/api-reference",
    },
    "sdk_outdated": {
        "severity": "warning",
        "stage": "client",
        "title": "Your LigandAI SDK is out of date",
        "remediation": "Upgrade to pick up current parameter handling, diagnostics, and bug fixes. Older clients can silently drop parameters that newer server versions expect.",
        "doc_anchor": "",
    },
    "sdk_unsupported": {
        "severity": "error",
        "stage": "client",
        "title": "Your LigandAI SDK is below the minimum supported version",
        "remediation": "Upgrade now. Versions this old are known to mis-send or drop request parameters, which can silently change what your job actually ran.",
        "doc_anchor": "",
    },
    "sdk_version_unknown": {
        "severity": "info",
        "stage": "client",
        "title": "SDK version could not be determined",
        "remediation": "Requests from an unrecognized client cannot be checked for version compatibility. Use an official LigandAI SDK, or set a User-Agent of the form ligandai-python/<version> or ligandai-node/<version>.",
        "doc_anchor": "",
    },
    "targeting_region_swapped": {
        "severity": "info",
        "stage": "client",
        "title": "A targeting region had start > end",
        "remediation": "The pair was swapped automatically to match server behaviour. Pass start <= end to silence this.",
        "doc_anchor": "sdk/python/allosteric-targeting#numbering",
    },
    "targeting_chain_ambiguous": {
        "severity": "warning",
        "stage": "client",
        "title": "A targeting region did not name a chain",
        "remediation": "This request names more than one chain, so a region without a chain matches any chain carrying that residue number. Pass chain= on every region to disambiguate.",
        "doc_anchor": "sdk/python/allosteric-targeting#numbering",
    },
    "avoid_shell_disabled": {
        "severity": "info",
        "stage": "client",
        "title": "The avoid packing shell is disabled",
        "remediation": "avoid_shell_angstrom=0 excludes only the named residues, not their packing neighbours. Leave it unset to use the server default.",
        "doc_anchor": "sdk/python/allosteric-targeting#shell",
    },
    "degenerate_pocket_risk": {
        "severity": "warning",
        "stage": "client",
        "title": "Exclusion may leave almost no pocket to condition on",
        "remediation": "The avoid span is at least as large as the target span, which usually leaves a degenerate (near-empty) design pocket. Check for overlapping or transposed ranges, widen the target face, or narrow the avoid set.",
        "doc_anchor": "sdk/python/allosteric-targeting#event-warning",
    },
    "targeting_contradiction": {
        "severity": "warning",
        "stage": "client",
        "title": "A residue is both required and excluded",
        "remediation": "The same residue appears in the must-contact set and the must-avoid set, so no design can satisfy both criteria. Remove it from one of the two lists.",
        "doc_anchor": "sdk/python/allosteric-targeting#event-warning",
    },
    "guidance_composition_collapse": {
        "severity": "error",
        "stage": "request",
        "title": "This guidance combination produces designs that cannot bind",
        "remediation": "De-immunization above strength 0.5, stacked with any expression or stability head, collapses the generator's amino-acid alphabet. Measured output of that stack is 12.7% hydrophobic / 0.2% aromatic / 0.1% K+R against 38.5% / 9.8% / 8.9% for designs that fold into binders. Lower the de-immunization strength to 0.5 or disable the other heads. To run it anyway, set acknowledgeCompositionRisk=true.",
        "doc_anchor": "guides/guidance-knobs#composition-collapse",
    },
    "guidance_composition_risk": {
        "severity": "warning",
        "stage": "request",
        "title": "This guidance strength pushes designs to the edge of the binder band",
        "remediation": "Measured output at de-immunization strength 2.0 is 25.2% hydrophobic / 2.0% aromatic / 0.95% K+R; the K+R fraction is already below the composition gate's floor, so part of this run will be withheld from folding. Strength 0.5 stays inside the reference band and still lowers predicted MHC-II epitope burden.",
        "doc_anchor": "guides/guidance-knobs#composition-collapse",
    },
    "composition_gate_rejected": {
        "severity": "warning",
        "stage": "generate",
        "title": "A design was withheld from folding on composition",
        "remediation": "This design's amino-acid composition falls below the floor measured on designs that fold into binders (25% hydrophobic, 2% aromatic, 2% K+R). No fold-time confidence score detects this defect - pDockQ actively rewards it - so the check has to run before GPU time is spent. Relax the guidance stack that produced it, or lower the gate thresholds deliberately.",
        "doc_anchor": "guides/guidance-knobs#composition-gate",
    },
    "composition_gate_cohort_collapsed": {
        "severity": "error",
        "stage": "generate",
        "title": "Most of this run falls outside the binder composition band",
        "remediation": "A majority of designs, or the pooled output alphabet itself, has collapsed. This is what an over-constrained guidance stack looks like, and fold confidence scores will not reveal it. Re-run with a weaker guidance stack before spending fold budget on this cohort.",
        "doc_anchor": "guides/guidance-knobs#composition-gate",
    },
    "pocket_auto_selected": {
        "severity": "warning",
        "stage": "generate",
        "title": "No target residues supplied - a pocket was chosen for you",
        "remediation": "With no target_residues, hotspot_residues or pocket_residues, generation falls back to the largest geometric surface cluster, which is ranked by residue count alone and carries no functional evidence. Supply target residues in the structure's PDB author numbering to steer the run deliberately.",
        "doc_anchor": "sdk/python/pocket-hotspot#auto-selection",
    },
    "pocket_outside_functional_domain": {
        "severity": "error",
        "stage": "generate",
        "title": "The auto-selected pocket is outside the target's known functional domain",
        "remediation": "The geometric fallback chose residues that fall outside the annotated functional or ligand-binding region of this receptor. Designs steered here will bind a site with no known function. Supply target_residues inside the functional domain, or confirm deliberately that you want this site.",
        "doc_anchor": "sdk/python/pocket-hotspot#auto-selection",
    },
    "binder_frame_mixed_ranking": {
        "severity": "warning",
        "stage": "results",
        "title": "These results span two structural frames and are not directly comparable",
        "remediation": "ipSAE's d0 normalisation scales with chain length, so a full-construct fold (binder plus expression cassette) and a binder-only fold of the same design do not produce comparable ipSAE values. Compare and rank within one binder_frame at a time; the frame is on every row.",
        "doc_anchor": "guides/guidance-knobs#binder-frame",
    },
    "selector_not_a_ranking": {
        "severity": "info",
        "stage": "results",
        "title": "Binding energy orders these designs but does not rank them",
        "remediation": "The generator's binding_energy does not predict downstream fold success - measured Spearman rho -0.003 (p 0.98) against binder ipSAE over 100 folds. Use it to browse, not to choose which designs to fold. The composition gate is the pre-fold filter that carries measured signal.",
        "doc_anchor": "guides/guidance-knobs#selection",
    },
    "acceptance_metric_missing": {
        "severity": "warning",
        "stage": "results",
        "title": "A required acceptance metric was never computed",
        "remediation": "This fold is reported as indeterminate and excluded from the gate, not failed. peptide_ipsae is NULL on folds where the binder-specific interface was never scored. Re-score the fold to bring it into the ranked set.",
        "doc_anchor": "guides/conjugate-design#indeterminate",
    },
    "acceptance_frame_unverified": {
        "severity": "warning",
        "stage": "results",
        "title": "Construct frame could not be verified",
        "remediation": "The designed region could not be located inside the folded chain, so contact attribution is not trustworthy and the designed-region check cannot be applied. The fold is reported as indeterminate, never as scaffold-mediated.",
        "doc_anchor": "guides/conjugate-design#linker-leg",
    },
    "acceptance_scaffold_mediated": {
        "severity": "error",
        "stage": "results",
        "title": "Interface is carried by the purification cassette, not the design",
        "remediation": "Every receptor contact falls on the tag/linker rather than the designed region. This fold measures the cassette, not your binder, and must not be presented as a hit.",
        "doc_anchor": "guides/conjugate-design#linker-leg",
    },
    "acceptance_engine_result_absent": {
        "severity": "warning",
        "stage": "results",
        "title": "Co-validation engine has no result for this design",
        "remediation": "Ensemble co-validation was requested but a required engine produced no comparable row in the same fold frame. The design is reported as NOT co-validated; it is never counted as a dual-gate pass.",
        "doc_anchor": "guides/ensemble-folding#why",
    },
    "acceptance_engine_frame_mismatch": {
        "severity": "warning",
        "stage": "results",
        "title": "Co-validation engines folded different construct frames",
        "remediation": "One engine folded the full construct and the other the bare binder. iPSAE is normalised by d0, a function of chain length, so those scores are not comparable. Re-fold both engines in the same frame before co-validating.",
        "doc_anchor": "guides/conjugate-design#frames",
    },
    "acceptance_ensemble_provenance_unverified": {
        "severity": "warning",
        "stage": "results",
        "title": "Co-validation engine provenance is unverified",
        "remediation": "The co-validation engine ran under conditions that are not provenance-checked: its per-fold artifacts are not retained, so the alignment depth it actually used cannot be confirmed for this fold. Treat the agreement as provisional until the realized MSA depth is recorded alongside the result.",
        "doc_anchor": "guides/ensemble-folding#why",
    },
    # ─── selectivity panel (mirrored from shared/warnings.json) ──────────────
    #
    # Added platform-side 2026-08-31 by the shared selectivity-verdict evaluator
    # (bd-LIGANDAI_ALPHA_V2-6ef28.1) and never mirrored here, so every one of
    # them was dropped on the floor by an SDK caller running a selectivity
    # panel. These are precisely the codes that say a selectivity verdict is
    # INDETERMINATE rather than negative — "we could not measure this panel
    # member" is not "this design is not selective" — so losing them turns an
    # unmeasured panel into a silent clean bill of health.
    "selectivity_on_target_unmeasured": {
        "severity": "warning",
        "stage": "results",
        "title": "On-target fold could not be evaluated",
        "remediation": "The on-target fold has no usable result, so no selectivity verdict can be produced against its off-target panel. Re-run the on-target fold before drawing a selectivity conclusion.",
        "doc_anchor": "guides/ensemble-folding#why",
    },
    "selectivity_panel_member_absent": {
        "severity": "warning",
        "stage": "results",
        "title": "Off-target panel member has no fold result",
        "remediation": "This panel member's fold failed, was skipped, or never ran, so its selectivity relative to the on-target could not be measured. It is reported as indeterminate, never as evidence the design is selective.",
        "doc_anchor": "guides/ensemble-folding#why",
    },
    "selectivity_metric_missing": {
        "severity": "warning",
        "stage": "results",
        "title": "A required selectivity metric was never computed",
        "remediation": "One or more of the mutual ipSAE/ipTM gate metrics is missing for this fold, so it is excluded from the selectivity call rather than treated as non-binding.",
        "doc_anchor": "guides/ensemble-folding#why",
    },
    "selectivity_frame_mismatch": {
        "severity": "warning",
        "stage": "results",
        "title": "Panel member folded in a different construct frame than the on-target",
        "remediation": "iPSAE is normalised by chain length, so a with_linker fold and a binder_only fold are not comparable. Re-fold the on-target and this panel member in the same construct frame before comparing them.",
        "doc_anchor": "guides/conjugate-design#frames",
    },
    "selectivity_metric_not_length_comparable": {
        "severity": "warning",
        "stage": "results",
        "title": "Cross-receptor selectivity delta is not length-comparable",
        "remediation": "A selectivity panel folds the same binder against different-length receptors. ipSAE (ipsae_d0res) is normalised by chain length, so an absolute-threshold call and a genuine cross-receptor delta are different claims; the length-comparable value (ipsae_d0ref) is not yet available for this fold. Treat any comparison across panel members on the raw ipSAE value with caution until it is.",
        "doc_anchor": "guides/conjugate-design#frames",
    },

    # ⛔ MIRRORED FROM shared/warnings.json, NOT INVENTED. The SDK copies the
    # platform taxonomy rather than importing it, and
    # test_registry_is_byte_identical_to_shared_warnings_json is the only thing
    # keeping the two in sync. These nine campaign codes were registered on the
    # platform and left unmirrored here, which turned that guard red on main.
    # [bd-dre-3jqok, mirroring bd-dre-7nn5h]
    "campaign_criteria_no_ceiling": {
        "severity": "error",
        "stage": "request",
        "title": "This campaign has no stopping ceiling",
        "remediation": "A run-until-criteria campaign must carry at least one ceiling (maxPoses, maxRounds, maxCredits, maxComputeUsd or maxGpuHours). Without one, a criterion that turns out to be unreachable on this target becomes an open-ended order to keep spending. Set the ceiling you are actually willing to spend and the campaign will stop there and report how far it got.",
        "doc_anchor": "guides/run-until-criteria#ceilings",
    },
    "campaign_criteria_unit_count_dependent": {
        "severity": "warning",
        "stage": "results",
        "title": "This criterion gets harder the more you fold",
        "remediation": "You asked that EVERY independent seed-unit clear the bar. Each additional seed is another chance to fail that conjunction, so a design can be demoted by evidence gathered to confirm it - measured on this platform at 42% pass with 2 seed-units and 0% with 9. Use a fraction rule (e.g. at least 3 of 4 units) if you meant 'holds up across seeds' rather than 'never once missed'.",
        "doc_anchor": "guides/run-until-criteria#unit-count-dependence",
    },
    "campaign_criteria_monotone_in_effort": {
        "severity": "warning",
        "stage": "results",
        "title": "This criterion rewards folding a design more",
        "remediation": "Gating on a design's BEST seed-unit means the score can only rise as more seeds are drawn, so designs that were folded more will out-rank equally good designs that were folded less. Use the median statistic, or a seed_replication leg, for a comparison that does not depend on how much effort each design received.",
        "doc_anchor": "guides/run-until-criteria#statistic",
    },
    "campaign_panel_uniform_draw": {
        "severity": "warning",
        "stage": "fold",
        "title": "Comparator panels are being filled in a way that will not complete them",
        "remediation": "When comparator legs are drawn evenly across every design, a design needing N comparators only completes when all N of its cells happen to be filled, so completions accrue as roughly the Nth power of the fraction folded - measured here as 4 complete panels out of 240 designs at 29% of the grid, where a linear reading predicted 70. Complete every leg of a subset of designs instead; the same spend then yields complete panels in proportion to it.",
        "doc_anchor": "guides/run-until-criteria#panel-chunking",
    },
    "campaign_projection_small_cohort": {
        "severity": "warning",
        "stage": "results",
        "title": "Cost projection rests on a small, selected cohort",
        "remediation": "The replication rates behind this projection were fitted on a cohort already selected for high scores, and every such rate measured on this campaign came in optimistic. The projection is reported as an interval whose expensive end has been widened for that reason, and it carries no single point estimate on purpose. Budget against the upper bound.",
        "doc_anchor": "guides/run-until-criteria#projection",
    },
    "campaign_projection_unbounded": {
        "severity": "error",
        "stage": "results",
        "title": "Cost to complete cannot be bounded from what has been measured",
        "remediation": "No design in the campaign has yet produced a scored seed-unit in a band with a measured replication rate, so there is nothing to project from and any number would be invented. Fold a first round and re-project; the interval narrows as soon as real units exist.",
        "doc_anchor": "guides/run-until-criteria#projection",
    },
    "campaign_band_not_converting": {
        "severity": "info",
        "stage": "results",
        "title": "A score band is not converting and is no longer being funded",
        "remediation": "Designs whose single seed-unit sits in this band replicated at a rate whose lower confidence bound is below the campaign's funding floor, so further seeds are being spent on higher bands instead. This is a budget decision, not a verdict on those designs - they remain candidates and can be funded explicitly if you want them measured anyway.",
        "doc_anchor": "guides/run-until-criteria#band-allocation",
    },
    "campaign_not_converging": {
        "severity": "warning",
        "stage": "results",
        "title": "A full round produced almost no newly qualified designs",
        "remediation": "The campaign is still inside its budget but the last round added fewer newly qualified designs than the convergence floor you set. This is the stop a budget ceiling never catches. Review whether the criterion is reachable on this target before funding more rounds; the designs measured so far are not discarded by this stop.",
        "doc_anchor": "guides/run-until-criteria#convergence",
    },
    "campaign_candidates_exhausted": {
        "severity": "warning",
        "stage": "generate",
        "title": "No undrawn candidate designs remain to fold",
        "remediation": "Every design the generator supplied has been folded, and the criterion is still unmet. Generate a further candidate set, or relax the criterion. The campaign has stopped rather than re-folding designs it has already judged, which would add cost without adding evidence.",
        "doc_anchor": "guides/run-until-criteria#candidates",
    },
}


def known_codes() -> list[str]:
    """Every code this SDK build knows about.

    Unknown codes are still legal on the wire and are never dropped — this list
    is for introspection and docs, **not** for filtering.
    """
    return sorted(DIAGNOSTIC_CODES.keys())


def spec_for(code: str) -> dict[str, str]:
    """Registry entry for ``code``, or an empty dict for an unregistered code."""
    return dict(DIAGNOSTIC_CODES.get(code) or {})


def severity_rank(severity: str | None) -> int:
    """Order a severity for threshold comparisons.

    An unrecognized severity ranks as ``warning`` rather than ``info`` — a
    future severity this build has never seen must not be silently demoted
    below the warn threshold.
    """
    if not severity:
        return _SEVERITY_ORDER["warning"]
    return _SEVERITY_ORDER.get(str(severity).strip().lower(), _SEVERITY_ORDER["warning"])


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass
class Diagnostic:
    """One user-facing diagnostic. Serializes to the canonical wire envelope.

    Mirrors ``fastapi_backend/core/warnings.py``'s ``Diagnostic`` field-for-field,
    plus :attr:`raw` — the untouched payload this object was parsed from, so an
    envelope key a newer server added is still reachable from an older SDK.

    Attributes
    ----------
    code : str
        Stable machine identifier; the join key across every layer.
    message : str
        Human-readable, interpolated, specific to this occurrence.
    severity : str
        ``"info"`` | ``"warning"`` | ``"error"``. A value outside that set is
        preserved verbatim and treated as at-least-``warning``.
    stage : str
        ``"request"`` | ``"generate"`` | ``"fold"`` | ``"results"`` | ``"client"``.
    title : str
        Short (<60 char) headline from the registry.
    remediation : str
        What the user should actually do about it. May be empty.
    details : dict
        Code-specific machine payload; stable keys per code, additive-only.
    doc_anchor : str
        Docs anchor, no leading slash. May be empty.
    occurred_at : str
        ISO-8601 UTC timestamp.
    subject_id : str | None
        Optional identifier for the thing the diagnostic is about (a design id,
        a gene, a job id).
    raw : dict
        The original wire payload when this was parsed from one; ``{}`` when
        constructed locally.
    """

    code: str
    message: str
    severity: str = "warning"
    stage: str = "results"
    title: str = ""
    remediation: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    doc_anchor: str = ""
    occurred_at: str = ""
    subject_id: str | None = None
    schema_version: int = DIAGNOSTIC_SCHEMA_VERSION
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        """Canonical wire envelope. Unknown keys from :attr:`raw` are re-emitted
        so a round-trip through this SDK is lossless."""
        out: dict[str, Any] = {
            "schema_version": self.schema_version,
            "code": self.code,
            "severity": self.severity,
            "stage": self.stage,
            "title": self.title,
            "message": self.message,
            "remediation": self.remediation,
            "details": self.details,
            "doc_anchor": self.doc_anchor,
            "occurred_at": self.occurred_at,
        }
        if self.subject_id:
            out["subject_id"] = self.subject_id
        for key, value in self.raw.items():
            if key not in out and key not in _ENVELOPE_ALIASES:
                out[key] = value
        return out

    @property
    def is_known_code(self) -> bool:
        """Whether this build's registry recognizes :attr:`code`.

        Informational only — an unknown code is still surfaced in full.
        """
        return self.code in DIAGNOSTIC_CODES

    def at_least(self, severity: str) -> bool:
        """Whether this diagnostic is at or above ``severity``."""
        return severity_rank(self.severity) >= severity_rank(severity)

    def format(self) -> str:
        """One human-readable block: title, message, remediation, details."""
        head = self.title or self.code
        parts = [f"[{self.severity}] {head} ({self.code})"]
        if self.message:
            parts.append(self.message)
        if self.remediation:
            parts.append(self.remediation)
        if self.details:
            try:
                parts.append(f"details: {json.dumps(self.details, sort_keys=True, default=str)}")
            except Exception:  # pragma: no cover - defensive
                parts.append(f"details: {self.details}")
        return "\n".join(parts)

    def __str__(self) -> str:
        return self.format()


# camelCase spellings tolerated on input. The contract is snake_case; these
# exist because a diagnostic that arrives in the wrong case must still surface.
_ENVELOPE_ALIASES: dict[str, str] = {
    "docAnchor": "doc_anchor",
    "occurredAt": "occurred_at",
    "subjectId": "subject_id",
    "schemaVersion": "schema_version",
}

_ENVELOPE_KEYS = frozenset(
    {
        "schema_version",
        "code",
        "severity",
        "stage",
        "title",
        "message",
        "remediation",
        "details",
        "doc_anchor",
        "occurred_at",
        "subject_id",
        *_ENVELOPE_ALIASES.keys(),
    }
)


def make_diagnostic(
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    *,
    severity: str | None = None,
    stage: str | None = None,
    title: str | None = None,
    remediation: str | None = None,
    doc_anchor: str | None = None,
    subject_id: str | None = None,
    occurred_at: str | None = None,
) -> Diagnostic:
    """Build a canonical diagnostic, enriched from the registry when known.

    Mirrors ``fastapi_backend.core.warnings.make_diagnostic``. This is the
    **emitter** path (client-side diagnostics this SDK originates), so an
    invalid severity/stage is coerced to a safe default. Diagnostics *received*
    from the wire go through :func:`diagnostic_from_payload`, which preserves
    unrecognized values instead of coercing them.
    """
    spec = DIAGNOSTIC_CODES.get(code) or {}
    sev = severity or spec.get("severity") or "warning"
    if sev not in SEVERITIES:
        sev = "warning"
    stg = stage or spec.get("stage") or "results"
    if stg not in STAGES:
        stg = "results"
    return Diagnostic(
        code=code,
        message=message,
        severity=sev,
        stage=stg,
        title=title if title is not None else (spec.get("title") or code),
        remediation=remediation if remediation is not None else (spec.get("remediation") or ""),
        details=dict(details or {}),
        doc_anchor=doc_anchor if doc_anchor is not None else (spec.get("doc_anchor") or ""),
        occurred_at=occurred_at or _utcnow_iso(),
        subject_id=subject_id,
    )


def is_diagnostic_envelope(payload: Any) -> bool:
    """Heuristic: does ``payload`` look like a canonical diagnostic envelope?

    Used to recognize a diagnostic that arrives without the canonical
    ``event: diagnostic`` SSE name (e.g. embedded in another event's body).
    Deliberately conservative: a ``code`` plus at least one other envelope-only
    key.
    """
    if not isinstance(payload, dict):
        return False
    code = payload.get("code")
    if not isinstance(code, str) or not code.strip():
        return False
    markers = ("schema_version", "schemaVersion", "severity", "remediation", "doc_anchor")
    return any(k in payload for k in markers)


def diagnostic_from_payload(
    payload: Any,
    *,
    default_code: str = "unspecified",
    default_stage: str = "results",
) -> Diagnostic | None:
    """Parse a wire envelope into a :class:`Diagnostic`. **Never filters.**

    This is the **receiver** path. Unknown codes, unknown severities and unknown
    envelope keys are preserved verbatim (the latter in :attr:`Diagnostic.raw`)
    — an SDK that only understood a fixed code list would reintroduce exactly
    the silence this contract exists to remove.

    Missing registry-supplied fields (``title``, ``remediation``, ``severity``,
    ``stage``, ``doc_anchor``) are backfilled from the local registry copy when
    the code is known, so an older server that emits a bare
    ``{"code": ..., "message": ...}`` still yields a fully-formed diagnostic.

    Returns ``None`` only when ``payload`` is not a mapping.
    """
    if not isinstance(payload, dict):
        return None

    def _pick(key: str) -> Any:
        if key in payload:
            return payload[key]
        for camel, snake in _ENVELOPE_ALIASES.items():
            if snake == key and camel in payload:
                return payload[camel]
        return None

    raw_code = _pick("code")
    code = raw_code.strip() if isinstance(raw_code, str) and raw_code.strip() else default_code
    spec = DIAGNOSTIC_CODES.get(code) or {}

    severity = _pick("severity")
    severity_str = (
        (str(severity).strip() if isinstance(severity, str) and severity.strip() else None)
        or spec.get("severity")
        or "warning"
    )

    stage = _pick("stage")
    stage_str = (
        (str(stage).strip() if isinstance(stage, str) and stage.strip() else None)
        or spec.get("stage")
        or default_stage
    )

    message = _pick("message")
    if not isinstance(message, str) or not message.strip():
        message = spec.get("title") or code

    title = _pick("title")
    if not isinstance(title, str) or not title.strip():
        title = spec.get("title") or code

    remediation = _pick("remediation")
    if not isinstance(remediation, str):
        remediation = spec.get("remediation") or ""

    doc_anchor = _pick("doc_anchor")
    if not isinstance(doc_anchor, str):
        doc_anchor = spec.get("doc_anchor") or ""

    details = _pick("details")
    if not isinstance(details, dict):
        # Keep a non-dict `details` rather than discarding it.
        details = {} if details is None else {"value": details}

    occurred_at = _pick("occurred_at")
    if not isinstance(occurred_at, str) or not occurred_at.strip():
        occurred_at = _utcnow_iso()

    subject_id = _pick("subject_id")
    if not isinstance(subject_id, str) or not subject_id.strip():
        subject_id = None

    schema_version = _pick("schema_version")
    try:
        schema_version_int = (
            int(schema_version) if schema_version is not None else DIAGNOSTIC_SCHEMA_VERSION
        )
    except (TypeError, ValueError):
        schema_version_int = DIAGNOSTIC_SCHEMA_VERSION

    extras = {k: v for k, v in payload.items() if k not in _ENVELOPE_KEYS}

    return Diagnostic(
        code=code,
        message=message,
        severity=severity_str,
        stage=stage_str,
        title=title,
        remediation=remediation,
        details=details,
        doc_anchor=doc_anchor,
        occurred_at=occurred_at,
        subject_id=subject_id,
        schema_version=schema_version_int,
        raw=extras,
    )


def diagnostics_from_container(payload: Any, *, _depth: int = 0) -> list[Diagnostic]:
    """Pull a ``diagnostics: [...]`` array out of a REST job/results payload.

    Looks at the top level and inside the handful of result containers the
    platform's status endpoints use (``result``, ``results``, ``data``,
    ``output``, ``job``), because the same logical field appears at different
    depths depending on which endpoint answered. Returns ``[]`` when there is
    nothing to find — never ``None``, never raises.
    """
    if not isinstance(payload, dict) or _depth > 3:
        return []
    out: list[Diagnostic] = []
    raw = payload.get("diagnostics")
    if isinstance(raw, list):
        for entry in raw:
            diag = diagnostic_from_payload(entry)
            if diag is not None:
                out.append(diag)
    for key in ("result", "results", "data", "output", "job"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            out.extend(diagnostics_from_container(nested, _depth=_depth + 1))
    return out


#: Code families whose diagnostics belong to the residue-targeting story, and
#: therefore keep raising :class:`~ligandai.errors.LigandAITargetingWarning` —
#: the category callers have been catching since bd-LIGANDAI_ALPHA_V2-x4vt8.
#: Everything else raises the broader
#: :class:`~ligandai.errors.LigandAIDiagnosticWarning` base. Both are catchable
#: as the base, so a filter on it sees every platform diagnostic.
_TARGETING_CODE_PREFIXES = (
    "avoid_",
    "target_",
    "targeting_",
    "degenerate_",
)


def is_targeting_code(code: str) -> bool:
    """Whether ``code`` belongs to the residue-targeting family.

    Prefix-based on purpose: a targeting code invented next year is a targeting
    code today, and must not silently fall out of the category a caller is
    already filtering on.
    """
    return any(code.startswith(prefix) for prefix in _TARGETING_CODE_PREFIXES)


def diagnostic_key(d: Diagnostic) -> str:
    """Stable identity for de-duplication across a stream + REST poll.

    ``code`` + ``subject_id`` + ``details`` — matching the Node SDK, so both
    SDKs collapse the same repeats. ``message`` is deliberately NOT part of the
    key: it is rendered from ``details``, so two envelopes agreeing on all three
    components describe the same occurrence.
    """
    try:
        payload = json.dumps(d.details, sort_keys=True, default=str)
    except Exception:  # pragma: no cover - defensive
        payload = str(d.details)
    return f"{d.code}|{d.subject_id or ''}|{payload}"


def merge_diagnostics(
    existing: list[Diagnostic], incoming: list[Diagnostic]
) -> tuple[list[Diagnostic], list[Diagnostic]]:
    """Append ``incoming`` to ``existing``, dropping exact repeats.

    Returns ``(merged, newly_added)``. ``newly_added`` is what a caller should
    surface on the warning channel — a job that polls its status endpoint every
    few seconds must not re-warn the same diagnostic on every tick.
    """
    merged = list(existing)
    seen = {diagnostic_key(d) for d in merged}
    added: list[Diagnostic] = []
    for d in incoming:
        key = diagnostic_key(d)
        if key in seen:
            continue
        seen.add(key)
        merged.append(d)
        added.append(d)
    return merged, added


def worst_severity(diags: list[Diagnostic]) -> str | None:
    """Highest severity present, or ``None`` for an empty list."""
    worst: str | None = None
    for d in diags:
        if worst is None or severity_rank(d.severity) > severity_rank(worst):
            worst = d.severity
    return worst
