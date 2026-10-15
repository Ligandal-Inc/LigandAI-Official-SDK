# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Peptide generation, folding, and scoring.

Public methods that submit GPU work return :class:`Job` (or :class:`AsyncJob`)
instances. Use ``.wait()`` to block until completion.

Endpoint mapping (server source-of-truth):

- :meth:`Peptides.generate`               → ``POST /api/ptf/parallel/generate``
- :meth:`Peptides.fold`                   → ``POST /api/folding/predict``
- :meth:`Peptides.fold_batch`             → ``POST /api/v1/folding/predict-batch``
- :meth:`Peptides.fold_custom_mutation`   → ``POST /api/ptf/fold-custom-mutation`` (or boltz2/modified-fold)
- :meth:`Peptides.continue_folding`       → ``POST /api/ptf/parallel/{sid}/continue``
- :meth:`Peptides.score_complex`          → ``POST /api/binder-scoring/fold-and-score``
- :meth:`Peptides.score_pdb`              → ``POST /api/v1/deltaforge/score-pdb``
- :meth:`Peptides.score_with_ligandiq`    → ``POST /api/ptf/parallel/{sid}/ligandiq-score``
- :meth:`Peptides.analyze_solubility`     → ``POST /api/peptide-features/solubility``
- :meth:`Peptides.search`                 → ``GET  /api/ptf/genes/summary`` + filter
- :meth:`Peptides.search_by_pocket`       → ``GET  /api/ptf/peptides/by-pocket``
- :meth:`Peptides.get_elite`              → ``GET  /api/ptf/parallel/{sid}/elite``
- :meth:`Peptides.by_gene`                → ``GET  /api/v1/peptides/by-gene``  (paid-only, v0.2.0+)
- :meth:`Peptides.list`                   → ``GET  /api/ptf/generated-peptides/by-gene/:gene``  (v0.2.0+)
- :meth:`Peptides.get`                    → ``GET  /api/v1/peptides/:id``       (paid-only, v0.2.0+)
"""

from __future__ import annotations

import math
import warnings
from datetime import datetime
from pathlib import Path
from collections.abc import AsyncIterator, Iterable, Iterator, Mapping
# ⛔ Aliased: the bare name `Sequence` in this module is
# ligandai.types.Sequence (a peptide-sequence business object, imported
# below) — NOT collections.abc.Sequence. avoid_paralogs=[...] takes a
# sequence OF GENE NAMES, so it needs the real generic container; using the
# ambient `Sequence` name there would accept the wrong type silently.
from collections.abc import Sequence as _StrSequence
from typing import Any, Callable, Literal, Union

from ligandai._targeting import validate_and_normalize_targeting
from ligandai.contrastive import (
    AvoidancePolicy,
    CfgReferenceProvenance,
    ContrastiveGuidance,
    avoid_paralogs_to_wire,
    normalize_avoid_paralogs,
    require_avoid_paralogs_model,
)
from ligandai.errors import (
    LigandAICreditError,
    LigandAIDiagnosticWarning,
    LigandAIError,
    LigandAIInvalidConfig,
    LigandAILegalAcceptanceRequired,
    LigandAIReusedSessionError,
    LigandAIValidationError,
    LigandAIWaitTimeout,
)
from ligandai.jobs import AsyncJob, Job
from ligandai.resources._base import AsyncResource, Resource, unwrap_rows
from ligandai.types import (
    CostEstimate,
    DeltaForgeScore,
    DevelopabilityResult,
    EcTrimmingConfig,
    FoldResult,
    GenerationResult,
    GeneSummary,
    LigandIQScore,
    PdcConfig,
    Peptide,
    PeptideDetail,
    PeptideInput,
    PeptideSegment,
    ResidueRange,
    SegmentConfig,
    Sequence,
    SolubilityResult,
)

# Allowed values for ``Peptides.get(include=[...])``. The server validates this
# against an allowlist and returns 400 for unknown entries — keep this in sync.
_IncludeField = Literal["pocket_features", "interface", "pdb"]
_ALLOWED_INCLUDE: frozenset[str] = frozenset({"pocket_features", "interface", "pdb"})
# ``esm_augmented`` is a selectively-unlocked, entitlement-gated scorer (per-account
# allowlist or superadmin). Passing it without the grant returns 403
# (ESM_SCORER_ENTITLEMENT_REQUIRED); the default ``v10_2`` scores on your tier.
_DeltaForgeScorer = Literal["auto", "current", "v10", "v10_2", "unified", "esm_augmented"]
_DeltaForgeAggregateMethod = Literal["boltzmann_parallel", "best_pair", "mean_pair"]

# Developability grade modules (bd-dre-h0cuo.2.3). Half-life rides inside the
# stability grade server-side but is requestable/suppressible independently.
_DEV_MODULES: frozenset[str] = frozenset({"stability", "immuno", "halflife"})


def _build_developability_body(
    *,
    sequences: list[str] | None,
    peptide_ids: list[int] | None,
    fold_ids: list[str | int] | None,
    modules: list[str] | None,
    stability: bool | None,
    immuno: bool | None,
    halflife: bool | None,
) -> dict[str, Any]:
    """Build the camelCase request body for /api/v1/peptides/score-developability.

    Validates that at least one input source is supplied and that any explicit
    ``modules`` whitelist uses known module names. The academia+ default (all
    three modules ON) is applied server-side; explicit ``False`` opt-outs and an
    explicit ``modules`` whitelist are forwarded verbatim.
    """
    seqs = [str(s).strip().upper() for s in (sequences or []) if str(s).strip()]
    pids = [int(p) for p in (peptide_ids or [])]
    fids = [str(f) for f in (fold_ids or [])]
    if not seqs and not pids and not fids:
        raise ValueError(
            "score_developability requires at least one of: sequences, peptide_ids, fold_ids"
        )
    if modules is not None:
        unknown = {str(m).lower() for m in modules} - _DEV_MODULES
        if unknown:
            raise ValueError(
                f"Unknown developability modules: {sorted(unknown)}. "
                f"Valid: {sorted(_DEV_MODULES)}"
            )
    body: dict[str, Any] = {}
    if seqs:
        body["sequences"] = seqs
    if pids:
        body["peptideIds"] = pids
    if fids:
        body["foldIds"] = fids
    if modules is not None:
        body["modules"] = [str(m).lower() for m in modules]
    # Explicit per-module opt-out flags (only forward when the caller set them).
    if stability is not None:
        body["stability"] = bool(stability)
    if immuno is not None:
        body["immuno"] = bool(immuno)
    if halflife is not None:
        body["halflife"] = bool(halflife)
    return body


# Cysteine-control keys that used to be passed via ``extra={...}`` and are now
# first-class typed kwargs on :meth:`Peptides.generate`. We continue to accept
# them via ``extra`` for backward compatibility but emit a DeprecationWarning;
# they will be hard-rejected in v0.3.0.
_DEPRECATED_EXTRA_CYS_KEYS: frozenset[str] = frozenset({
    "cys_mode",
    "cysteine_mode",
    "cysteineMode",
    "cys_gate",
    "cysteine_gate",
    "cyclic_mode",
    "cyclicMode",
    "cyclic_strength",
    "cyclicStrength",
    "strict_recombinant",
    "strictRecombinant",
    "dual_fold_viz",
    "dualFoldViz",
    "disulfide_constraints",
    "disulfideConstraints",
})

_LOGITS_EXTRA_KEYS: frozenset[str] = frozenset({
    "return_logits",
    "returnLogits",
    "output_logits",
    "outputLogits",
    "include_logits",
    "includeLogits",
    "logits",
})

# Raw wire-format guidance keys a caller could set directly through
# ``extra={...}`` / ``**extra``, bypassing the typed kwargs (``immunogenicity``,
# ``serum_stability``, ``residue_composition``, ``cellfree``,
# ``cysteine_enabled``, ...). Every guidance head has a typed kwarg as of
# bd-LIGANDAI_ALPHA_V2-b4f34, but ``extra`` remains a supported escape hatch and
# the model-compatibility check must not miss a guidance flag set that way just
# because it skipped the typed field.
#
# Both the canonical (``residueComposition*``) and the legacy (``halflife*``)
# spellings are listed. A key that stops being recognised here does not stop
# reaching the server — it just stops being COUNTED, which is how a guidance
# request goes silently missing from a compatibility check.
_GUIDANCE_EXTRA_KEYS: frozenset[str] = frozenset({
    "cellfreeEnabled",
    "cellfree_enabled",
    "cellfreeStrength",
    "cellfree_strength",
    "immunoEnabled",
    "immuno_enabled",
    "stabilityEnabled",
    "stability_enabled",
    "proteolyticEnabled",
    "proteolytic_enabled",
    "residueCompositionEnabled",
    "residue_composition_enabled",
    "halflifeEnabled",
    "halflife_enabled",
    "cysteineEnabled",
    "cysteine_enabled",
})

# Generator models known to this SDK build that do NOT implement the guidance
# heads — i.e. the ids for which the platform's model registry carries
# ``supports_guidance_heads = False``. The gate is that CAPABILITY, not the
# identity of any one model; add an id here only because its capability flag is
# false, never because of its version number.
#
# ⛔ NO LONGER EMPTY (2026-08-20): v6.5.22 carries
# ``supports_guidance_heads = False`` / ``guidance_transport = "unsupported"``
# in the platform registry, verified by reading GENERATOR_MODELS directly rather
# than from prose. Asking for guidance on v6.5.22 now fails fast and named here
# instead of as an opaque server rejection -- which is exactly what the paragraph
# below asked for.
#
# Previously (bd-LIGANDAI_ALPHA_V2-sxc8j, 2026-08-11): v6.5, v6.5.21, AND
# v6.9.21 all implement the four guidance heads
# (immuno/proteolytic/residue_composition/cellfree) — the v6.5 line via the
# streaming guided-pool transport, v6.9.21 via the batch-spawn "default_function"
# transport (guidance kwargs ride the normal generate call; no streaming
# surface). v6.9.21 used to be in this set; it was removed once the worker
# was redeployed and verified to serve the four heads.
#
# This set — and the conflict check in ``_resolve_generator_model`` below —
# stays in place for a FUTURE model that ships without guidance support: add
# its id here the day its registry entry carries
# ``supports_guidance_heads = False``, so that combination fails with a fast,
# named, client-side error instead of an opaque server rejection.
#
# ⛔ ABSENCE FROM THIS SET IS NOT A CAPABILITY CLAIM. A published SDK cannot know
# the capabilities of a model that did not exist when it was built, so an id this
# build has not heard of is forwarded and the SERVER's request validation — which
# reads the capability flag straight off the registry — is the authority. This
# set exists only to turn the combinations we DO know are impossible into a fast,
# named, pre-flight error instead of an opaque server rejection. Keeping a full
# copy of the registry here would go stale exactly as fast as a hardcoded id list.
_MODELS_WITHOUT_GUIDANCE_HEADS: frozenset[str] = frozenset({"v6.5.22"})


def _merge_avoidance(
    avoidance: Any,
    avoid_residues: Any,
    avoid_shell_angstrom: float | None,
) -> tuple[Any, float | None]:
    """Fold an :class:`~ligandai.contrastive.AvoidancePolicy` into the two kwargs.

    Explicit kwargs win over the policy. The policy's ``hard_negatives`` are NOT
    sent: the generator has no sequence-repulsion term, and there is no request
    field for one, so a hard-negative list on the wire would be dropped in
    silence. They are applied as a client-side filter by
    ``client.campaigns`` instead, which is the only place they actually bite.
    """
    if avoidance is None:
        return avoid_residues, avoid_shell_angstrom
    policy_kwargs = avoidance.generation_kwargs()
    if avoid_residues is None:
        avoid_residues = policy_kwargs.get("avoid_residues")
    if avoid_shell_angstrom is None:
        avoid_shell_angstrom = policy_kwargs.get("avoid_shell_angstrom")
    return avoid_residues, avoid_shell_angstrom


def _warn_deprecated_cys_extra(extra: dict[str, Any] | None) -> None:
    """Emit DeprecationWarning when cys-related keys arrive via ``extra=``.

    The typed kwargs (``cysteine_mode``, ``cyclic_mode``, etc.) are the
    blessed surface as of v0.2.0. The ``extra`` path still works for
    backward compatibility, but will be hard-rejected in v0.3.0.
    """
    if not extra:
        return
    leaked = sorted(k for k in extra if k in _DEPRECATED_EXTRA_CYS_KEYS)
    if not leaked:
        return
    keys = ", ".join(leaked)
    warnings.warn(
        (
            f"Passing cysteine/cyclic controls via extra={{}} is deprecated as of "
            f"ligandai v0.2.0 (got: {keys}). Pass them as typed kwargs on "
            f"Peptides.generate() — e.g. cysteine_mode=, cyclic_mode=, "
            f"strict_recombinant=, ... — instead. The extra-dict path will be "
            f"removed in v0.3.0."
        ),
        DeprecationWarning,
        stacklevel=3,
    )


# ---------------------------------------------------------------------------
# Guidance knob defaults and bounds — bd-LIGANDAI_ALPHA_V2-b4f34
#
# ⛔ THE CEILINGS BELOW ARE ENFORCED IN THE GENERATION WORKER, not here. The SDK
# does not clamp: silently rewriting a caller's number is how "the run did not do
# what I asked" bugs are born. It WARNS, sends the value you gave it, and lets the
# worker's clamp be the single enforcement point (so the website, both SDKs, a raw
# API call, and the local A2000 fallback generator all converge on the same
# number and it logs when it intervenes).
#
# ``warn_above`` is NOT the ceiling. It is the strength above which the measured
# evidence stops supporting the setting — the band between ``warn_above`` and
# ``ceiling`` is "supported, but you are past the calibrated region".
#
# Every value here traces to a measurement or is explicitly flagged as a
# judgement call. Do not add a row without one.
# ---------------------------------------------------------------------------
_GUIDANCE_BOUNDS: dict[str, dict[str, float]] = {
    # MEASURED. The binding guard is the L1-distance-to-natural-composition
    # bound (MAX_L1 = 45): clean on all 5 seeds at 1.5, trips at 1.75.
    "immuno_strength": {"default": 2.0, "min": 0.0, "ceiling": 1.5, "warn_above": 1.5},
    # MEASURED (bd-qmhck). L1 40 @2.0 -> 70 @4.0 -> 95 @8.0 while YIELD RISES to
    # 100%, so a yield-based guard never sees the damage.
    "stability_strength": {"default": 2.0, "min": 0.0, "ceiling": 2.0, "warn_above": 2.0},
    # MEASURED (bd-sxc8j.12). 4.0 is the last strength whose dose effect on
    # between-receptor JSD is not distinguishable from zero (-11.5%, 95% CI
    # [-27.2%, +4.7%]); it becomes significant at 8.0 (-22.3%, CI [-38.3%, -6.4%]).
    "residue_composition_strength": {
        "default": 2.0, "min": 0.0, "ceiling": 4.0, "warn_above": 2.0,
    },
    # ⚠ JUDGEMENT CALL, not a measurement. Basis: bd-b8k08 measured kernel
    # SATURATION above s=4 at round 0 — i.e. higher values buy nothing, not that
    # they were shown to cause harm. Stated as a judgement so nobody cites it as
    # a measured ceiling later.
    # ⛔ ``default`` here is the CLIENT default (1.5), which is what this SDK, the
    # Node SDK, the website and FastAPI all send. The generation worker's own
    # signature defaults to 2.0. That disagreement is real and unresolved; it is
    # documented at the ``cellfree_strength`` kwarg rather than papered over.
    "cellfree_strength": {"default": 1.5, "min": 0.0, "ceiling": 4.0, "warn_above": 4.0},
}


def _warn_strength_out_of_band(
    name: str, value: float | None, *, head_enabled: bool
) -> None:
    """Warn when an ACTIVE guidance strength leaves the calibrated band.

    Two distinct warnings, because they mean different things:

    * ``> ceiling`` — the generation worker WILL clamp this to the ceiling. The
      run will not use the number you passed. This is the loud one.
    * ``> warn_above`` (but <= ceiling) — accepted and applied verbatim, but past
      the strength the measurements support.

    Below ``min`` raises rather than warns: a negative strength inverts the sign
    of every penalty in the kernel, which is not a supported configuration. That
    check runs even for a disabled head — a negative number is a mistake whether
    or not it would have been applied.

    ⛔ ``head_enabled`` is load-bearing, not defensive. ``immuno_strength``
    DEFAULTS to 2.0 while its ceiling is 1.5, so an ungated check would warn on
    every single ``generate()`` call — including calls that never touch immuno
    guidance. A warning that fires when nothing is wrong is a warning users
    learn to ignore, which costs us the one case that mattered.
    """
    if value is None:
        return
    bounds = _GUIDANCE_BOUNDS.get(name)
    if bounds is None:
        return
    if value < bounds["min"]:
        raise LigandAIInvalidConfig(
            f"{name}={value} is below the minimum {bounds['min']}. A negative "
            "guidance strength inverts every penalty in the kernel and is not a "
            "supported configuration.",
            field=name,
            value=value,
        )
    if not head_enabled:
        # The value is inert on this request — nothing to warn about.
        return
    if value == bounds["default"]:
        # The caller did not choose this number; it is the signature default.
        # ``immuno_strength`` defaults to 2.0 against a 1.5 ceiling, so warning
        # here would fire for every caller who simply enabled the head — and the
        # clamp is documented on the parameter itself. Warn about values the
        # caller actually picked; document the ones they inherited.
        return
    if value > bounds["ceiling"]:
        warnings.warn(
            f"{name}={value} exceeds the server-side ceiling of "
            f"{bounds['ceiling']}. The generation worker clamps it to "
            f"{bounds['ceiling']} and logs that it did, so the run will NOT use "
            f"{value}. Pass {bounds['ceiling']} or lower to make the intent "
            "visible in your call.",
            UserWarning,
            stacklevel=3,
        )
    elif value > bounds["warn_above"]:
        warnings.warn(
            f"{name}={value} is above {bounds['warn_above']}, the highest "
            "strength the deployed measurements support. It is accepted and "
            f"applied verbatim (the ceiling is {bounds['ceiling']}), but you are "
            "past the calibrated region — check composition per design.",
            UserWarning,
            stacklevel=3,
        )


def _resolve_residue_composition(
    *,
    residue_composition: _ResidueCompositionTarget | None,
    residue_composition_strength: float | None,
    halflife: _ResidueCompositionTarget | None,
    halflife_strength: float | None,
) -> tuple[_ResidueCompositionTarget | None, float, str | None]:
    """Resolve the residue-composition knob from its new and legacy names.

    The knob was called ``halflife`` / ``halflife_strength`` through v0.11.x and
    was renamed to ``residue_composition`` / ``residue_composition_strength`` in
    bd-LIGANDAI_ALPHA_V2-b4f34 because it never measured a half-life.

    Contract (identical on every layer that accepts this knob — both SDKs, the
    server, the worker):

    * BOTH names are accepted. Existing callers do not break.
    * The legacy name emits a :class:`DeprecationWarning`.
    * If both are supplied and they DISAGREE, the NEW name wins and the warning
      says so.
    * If both are supplied and they AGREE, one warning, no conflict.

    Returns ``(target, strength, alias_used)`` where ``alias_used`` is
    ``"halflife"`` when the caller reached this knob through the legacy name and
    ``None`` otherwise. ``alias_used`` is recorded in request provenance so a run
    can be traced back to the name the caller actually used.
    """
    default_strength = _GUIDANCE_BOUNDS["residue_composition_strength"]["default"]
    legacy_used = halflife is not None or halflife_strength is not None
    new_used = residue_composition is not None or residue_composition_strength is not None

    if not legacy_used:
        strength = (
            residue_composition_strength
            if residue_composition_strength is not None
            else default_strength
        )
        _warn_strength_out_of_band(
            "residue_composition_strength",
            strength,
            head_enabled=residue_composition is not None,
        )
        return residue_composition, strength, None

    conflicts: list[str] = []
    if (
        residue_composition is not None
        and halflife is not None
        and residue_composition != halflife
    ):
        conflicts.append(
            f"residue_composition={residue_composition!r} vs halflife={halflife!r}"
        )
    if (
        residue_composition_strength is not None
        and halflife_strength is not None
        and residue_composition_strength != halflife_strength
    ):
        conflicts.append(
            f"residue_composition_strength={residue_composition_strength} vs "
            f"halflife_strength={halflife_strength}"
        )

    message = (
        "halflife= / halflife_strength= are deprecated names for the "
        "residue-composition guidance knob and will be removed in a future "
        "major release. Use residue_composition= / "
        "residue_composition_strength= instead. The knob was renamed because it "
        "does NOT measure, predict or optimise a half-life: it shapes "
        "sequence-level composition only (tryptophan load, basic fraction, "
        "tyrosine content, terminal residues). It implements no N-end rule and "
        "no PEST term."
    )
    if conflicts:
        message += (
            " Both names were supplied and they disagree ("
            + "; ".join(conflicts)
            + "). The NEW name wins."
        )
    warnings.warn(message, DeprecationWarning, stacklevel=3)

    target = residue_composition if residue_composition is not None else halflife
    if residue_composition_strength is not None:
        strength = residue_composition_strength
    elif halflife_strength is not None:
        strength = halflife_strength
    else:
        strength = default_strength
    _warn_strength_out_of_band(
        "residue_composition_strength", strength, head_enabled=target is not None
    )
    # alias_used is reported only when the legacy name is what actually carried
    # the request — if the caller passed both and the new name won, the canonical
    # name is what was used.
    alias_used = "halflife" if not new_used else None
    return target, strength, alias_used


def _requests_advanced_guidance(
    *,
    immunogenicity: bool,
    immuno_modules: dict[str, bool] | None,
    serum_stability: bool,
    stability_modules: dict[str, bool] | None,
    residue_composition: _ResidueCompositionTarget | None,
    cellfree: bool,
    cyclic_mode: _CyclicMode | None,
    extra: dict[str, Any] | None,
) -> bool:
    """Return True for guidance outputs gated to academia/pro/enterprise.

    ``cysteine_enabled`` / ``cysteine_mode`` are deliberately NOT in this set.
    Cysteine policy has always been available on every tier, and making an
    explicit ``cysteine_enabled=`` request newly tier-gated would be a silent
    downgrade for existing free/basic callers.
    """
    if (
        immunogenicity
        or immuno_modules is not None
        or serum_stability
        or stability_modules is not None
        or residue_composition is not None
        or cellfree
        or (cyclic_mode is not None and cyclic_mode != "none")
    ):
        return True
    return bool(extra and any(bool(extra.get(key)) for key in _LOGITS_EXTRA_KEYS))


def _guidance_arguments_in_use(
    *,
    immunogenicity: bool,
    immuno_modules: dict[str, bool] | None,
    serum_stability: bool,
    stability_modules: dict[str, bool] | None,
    residue_composition: _ResidueCompositionTarget | None,
    cellfree: bool,
    cysteine_enabled: bool | None = None,
    extra: dict[str, Any] | None,
) -> list[str]:
    """Names of the guidance-head arguments the caller actually turned on.

    Scope is the v6.5-line runtime penalty functions — proteolytic/serum
    stability, immunogenicity, residue & terminal composition, cell-free
    expressability, and (as of bd-LIGANDAI_ALPHA_V2-b4f34) an explicit
    ``cysteine_enabled=True`` request. Returned sorted so the error message is
    deterministic. Consulted only when the resolved model is in
    ``_MODELS_WITHOUT_GUIDANCE_HEADS`` — empty today, kept for a future model
    that ships without guidance support. ``quality_guided`` is NOT in this set:
    it is a different mechanism, defaults to ``True``, and is not a penalty head.
    """
    in_use: list[str] = []
    if immunogenicity:
        in_use.append("immunogenicity")
    if immuno_modules is not None:
        in_use.append("immuno_modules")
    if serum_stability:
        in_use.append("serum_stability")
    if stability_modules is not None:
        in_use.append("stability_modules")
    if residue_composition is not None:
        in_use.append("residue_composition")
    if cellfree:
        in_use.append("cellfree")
    if cysteine_enabled:
        in_use.append("cysteine_enabled")
    if extra:
        in_use.extend(
            f"extra[{key!r}]" for key in _GUIDANCE_EXTRA_KEYS if bool(extra.get(key))
        )
    return sorted(in_use)


def _resolve_generator_model(
    model: _GeneratorModel | str | None,
    *,
    immunogenicity: bool,
    immuno_modules: dict[str, bool] | None,
    serum_stability: bool,
    stability_modules: dict[str, bool] | None,
    residue_composition: _ResidueCompositionTarget | None,
    cellfree: bool,
    cysteine_enabled: bool | None = None,
    extra: dict[str, Any] | None,
) -> str | None:
    """Normalize ``model`` and reject request shapes the selected model cannot run.

    Returns the selector id to put on the wire as ``generatorModelId``, or
    ``None`` when the caller supplied nothing. ``None`` means **send no key** so
    the server picks the default — the SDK deliberately does not carry a
    client-side copy of the default, because a client-side copy stops tracking
    the server the moment the platform default moves.

    The SDK does not gate the id list. An id this build has not heard of is
    forwarded verbatim; the server's model registry is the authority and answers
    with an error naming the ids it knows. Hard-coding an allowlist here would
    make every future model unusable until users upgraded the SDK.

    Raises:
        LigandAIInvalidConfig: ``model`` is not a non-empty string, or the model
            does not implement the guidance heads the request asks for.
    """
    if model is None:
        return None
    if not isinstance(model, str) or not model.strip():
        raise LigandAIInvalidConfig(
            "model must be a non-empty generator model id string "
            "(e.g. 'v6.5.21', 'v6.9.21', 'v6.5').",
            field="model",
            value=model,
        )
    model_id = model.strip()
    if model_id in _MODELS_WITHOUT_GUIDANCE_HEADS:
        conflicts = _guidance_arguments_in_use(
            immunogenicity=immunogenicity,
            immuno_modules=immuno_modules,
            serum_stability=serum_stability,
            stability_modules=stability_modules,
            residue_composition=residue_composition,
            cellfree=cellfree,
            cysteine_enabled=cysteine_enabled,
            extra=extra,
        )
        if conflicts:
            raise LigandAIInvalidConfig(
                f"model={model_id!r} does not support the guidance options "
                f"{conflicts}. The stability, immunogenicity, residue-composition, "
                "cell-free and cysteine guidance heads run inside the v6.5-line "
                "sampling loop, which this model does not use. Drop those arguments to "
                f"use {model_id}, or use a model that supports them "
                "(e.g. 'v6.5.21'). They are not dropped silently, because a "
                "design generated without the guidance you asked for is not "
                "the design you asked for.",
                field="model",
                value=model_id,
                allowed=f"remove {conflicts} or select a guidance-capable model",
            )
    return model_id


def _parse_deltaforge_score(data: dict[str, Any]) -> DeltaForgeScore:
    scoring = data.get("scoring") or data.get("deltaforge") or data

    def pick(*keys: str) -> Any:
        for key in keys:
            if key in scoring and scoring[key] is not None:
                return scoring[key]
        return None

    return DeltaForgeScore.model_validate(
        {
            "dg": pick("dg", "delta_g", "deltaG"),
            "kd": pick("kd", "kd_nm", "kdNm"),
            "kd_nm": pick("kd_nm", "kdNm"),
            "contacts": pick("contacts", "contact_count", "num_contacts"),
            "interfaceResidues": pick("interface_residues", "interfaceResidues"),
            "scorer": pick("scorer"),
            "scorer_version": pick("scorer_version", "scorerVersion"),
            "model_sha256": pick("model_sha256", "modelSha256"),
            "feature_schema_version": pick("feature_schema_version", "featureSchemaVersion"),
            "aggregate_method": pick("aggregate_method", "aggregateMethod"),
            "version_family": pick("version_family", "versionFamily"),
            "affinity_scorer": pick("affinity_scorer", "affinityScorer"),
            "affinity_scorer_version": pick("affinity_scorer_version", "affinityScorerVersion"),
            "calibration_head": pick("calibration_head", "calibrationHead"),
            "structure_source_detected": pick("structure_source_detected", "structureSourceDetected"),
            "calibration_router": pick("calibration_router", "calibrationRouter"),
            "peptide_length": pick("peptide_length", "peptideLength"),
            "platform_length_scope": pick("platform_length_scope", "platformLengthScope"),
            "predicted_affinity_tier": pick("predicted_affinity_tier", "predictedAffinityTier"),
            "predicted_binder": pick("predicted_binder", "predictedBinder"),
            "predicted_binder_call": pick("predicted_binder_call", "predictedBinderCall"),
            "predicted_binder_label": pick("predicted_binder_label", "predictedBinderLabel"),
            "predicted_binder_probability": pick(
                "predicted_binder_probability", "predictedBinderProbability"
            ),
            "binder_call_method": pick("binder_call_method", "binderCallMethod"),
            "predicted_non_binder_reasons": pick(
                "predicted_non_binder_reasons", "predictedNonBinderReasons"
            ),
            "missing_binder_gate_inputs": pick(
                "missing_binder_gate_inputs", "missingBinderGateInputs"
            ),
            "readout_note": pick("readout_note", "readoutNote"),
            "affinity_plus_structure_readout": pick(
                "affinity_plus_structure_readout", "affinityPlusStructureReadout"
            ),
            "dual_readout": pick("dual_readout", "dualReadout"),
            "structural_energy_gates": pick("structural_energy_gates", "structuralEnergyGates"),
            "best_pair": pick("best_pair", "bestPair"),
            "pair_scores": pick("pair_scores", "pairScores"),
            "pair_errors": pick("pair_errors", "pairErrors"),
            "warnings": pick("warnings"),
            "metadata": scoring.get("metadata") or scoring,
            # fold confidence + PAE passthrough
            "iptm": pick("iptm", "iPTM"),
            "ptm": pick("ptm", "pTM"),
            "ipsae": pick("ipsae", "iPSAE"),
            # ⛔ peptide_ipsae is the BINDER metric (peptide-receptor interface iPSAE).
            # overall ipsae is complex-level confidence dominated by the receptor — NOT a binder proxy.
            # Use data.get() not pick() so explicit None is preserved (means 'not_computed').
            "peptide_ipsae": data.get("peptide_ipsae") if "peptide_ipsae" in data
                else scoring.get("peptide_ipsae"),
            "peptide_ipsae_status": pick("peptide_ipsae_status"),
            "plddt_mean": pick("plddt_mean", "plddtMean", "mean_plddt"),
            "foldJobId": pick("foldJobId", "fold_job_id"),
            "pae": pick("pae"),
            "paeStatus": pick("pae_status", "paeStatus"),
        }
    )

_TargetingStrategy = Literal["full_surface", "pocket_targeted"]

# Receptor topology FACE to restrict the design pocket to (bd-LIGANDAI_ALPHA_V2-q3z1b).
# The server resolves an explicit face to the matching membrane-topology residue
# ranges (NC=extracellular, TM=transmembrane, CY=intracellular) and sets them as
# the explicit-pocket target_residues. When unset (None) the backend featurizer
# applies its receptor default (extracellular + transmembrane, excluding the
# intracellular/cytoplasmic face) — so the override only fires when the caller
# names a face. ``full`` means "do not restrict — fold/target the whole surface".
_TargetFace = Literal[
    "extracellular",  # NC ranges only
    "ec_tm",          # NC + TM (the explicit form of the receptor default)
    "transmembrane",  # TM ranges only
    "intracellular",  # CY ranges only (cytoplasmic — explicit IC override)
    "full",           # no face restriction (whole surface)
]

# Cyclic peptide mode. Controls which cyclization constraint is applied during
# generation (recombinant-only scope — Adaptyv synthesis path).
#
# - ``"none"`` — linear peptide, no cyclic constraint (default).
# - ``"lactam"`` — head-to-tail amide closure; PREDICTION/VIZ layer only.
#   The synthesis order goes out as the disulfide (Cys-Cys) construct when
#   the user accepts a lactam-designed peptide via Adaptyv.
# - ``"disulfide"`` — terminal Cys-Cys bridge. PRIMARY recombinant-shippable
#   mode. When ``strict_recombinant=True`` (default), no internal Cys allowed.
# - ``"head_tail_contact"`` — soft B-matrix bias toward terminal-pair-favorable
#   compositions; no synthesis constraint added.
#
# Tier-gated: advanced immunogenicity/stability/logits guidance remains
# academia/pro/enterprise only. Quality-guided base generation is available to
# all authenticated tiers, including free, subject to credits and limits.
_CyclicMode = Literal["none", "lactam", "disulfide", "head_tail_contact"]

# Charge filtering mode applied by the filtered design worker.
# - ``"off"`` — no charge filter (default behavior when chargeMode='off').
# - ``"lt"`` — keep peptides with net charge < chargeValue.
# - ``"gt"`` — keep peptides with net charge > chargeValue.
# - ``"between"`` — keep peptides with chargeMin ≤ net charge ≤ chargeMax.
_ChargeMode = Literal["off", "lt", "gt", "between"]

# Cysteine placement policy applied during peptide generation.
#
# - ``"allow_all"`` / ``"allow"`` — no filtering; the model is free to place
#   cysteines anywhere. Use this when you want unconstrained generation, e.g.
#   when targeting a covalent binder against a target Cys.
# - ``"disulfide_only"`` / ``"stability_only"`` (default) — only keep peptides
#   with 0 cysteines OR pairs whose positions form a plausible disulfide
#   geometry (|i-j| in {3,4} or >=6).
# - ``"exclude_all"`` / ``"exclude"`` — reject any peptide containing cysteine.
# - ``"terminal_only"`` — cysteine permitted only at the two terminal positions.
# - ``"terminal_pair"`` — a cysteine at BOTH termini. This is the mode that
#   produces head-to-tail cyclisable designs: the kernel hard-masks the terminal
#   positions so the pair is placed rather than hoped for. Pair it with
#   ``cyclic_mode="disulfide"`` when you want the cyclisation scored as well.
#
# Server-side this is enforced via rejection sampling with backpressure refill,
# so requesting ``num_peptides=N`` returns exactly N peptides regardless of mode.
#
# ⛔ ``terminal_only`` and ``terminal_pair`` were MISSING from this type through
# v0.12.2 while the platform had accepted both the whole time — so a caller
# asking for cyclisable designs failed type-checking on a supported request and
# had no documented way to reach the mode.
#
# ⛔ AND THE VALUE IS NOT SERVER-VALIDATED. An unrecognised cysteine mode does
# not 400: the normaliser falls through to ``"disulfide_only"`` silently, so a
# typo becomes a different, plausible-looking run. That is why
# ``_validate_cysteine_mode`` below rejects unknown values CLIENT-SIDE — it is
# the only place the mistake is visible.
_CysteineMode = Literal[
    "allow_all",
    "allow",
    "disulfide_only",
    "stability_only",
    "exclude_all",
    "exclude",
    "terminal_only",
    "terminal_pair",
]

#: Every cysteine mode the platform recognises. A value outside this set is
#: silently rewritten to ``"disulfide_only"`` server-side, so it is rejected here.
_VALID_CYSTEINE_MODES: frozenset[str] = frozenset(
    {
        "allow_all",
        "allow",
        "disulfide_only",
        "stability_only",
        "exclude_all",
        "exclude",
        "terminal_only",
        "terminal_pair",
    }
)


def _validate_cysteine_mode(mode: Any) -> str:
    """Reject a cysteine mode the platform would silently rewrite.

    The server normalises an unknown mode to ``"disulfide_only"`` without an
    error, so ``cysteine_mode="terminal-pair"`` (hyphen) runs a DIFFERENT
    experiment and reports success. Caught here instead.
    """
    value = str(mode).strip()
    if value not in _VALID_CYSTEINE_MODES:
        raise LigandAIInvalidConfig(
            f"cysteine_mode={mode!r} is not a recognised mode. The platform does "
            f"not reject an unknown mode — it silently substitutes "
            f"'disulfide_only', so this would run a different experiment and "
            f"report success.",
            field="cysteine_mode",
            value=mode,
            allowed=sorted(_VALID_CYSTEINE_MODES),
        )
    return value

# Residue & terminal composition guidance target (formerly "half-life").
#
# ⛔ NAME CHANGE (bd-LIGANDAI_ALPHA_V2-b4f34): this knob was called ``halflife``
# through v0.11.x. It never measured, predicted or optimised a half-life. The
# deployed kernel is purely SEQUENCE-LEVEL COMPOSITION shaping:
#   * an escalating tryptophan penalty (1.0x / 2.5x / 4.0x as Trp count hits 0/1/2),
#   * an R/K/H penalty gated on the running basic fraction (0.3x / 0.6x / 1.0x at
#     basic fraction <=0.15 / >0.15 / >0.25),
#   * a tyrosine reward when Tyr is under-represented,
#   * terminal-residue rewards (P/Q at the N-terminus, P/D at the C-terminus),
#   * a proline reward cap and an anti-escape cysteine baseline.
# There is NO N-end-rule term and NO PEST term anywhere in it, and it consults no
# measured half-life data. See ``residue_composition`` on
# :meth:`Peptides.generate` for the full statement.
#
# ``"extended"`` applies the kernel at full strength; ``"moderate"`` applies it at
# 0.4x strength. ``"rapid"`` is accepted for backward compatibility but is a
# NO-OP in the dynamic kernel — the worker returns a zero penalty tensor for any
# target outside {"extended", "moderate"}.
_ResidueCompositionTarget = Literal["extended", "rapid", "moderate"]

#: Deprecated alias retained so downstream type annotations importing the old
#: private name keep resolving. Prefer :data:`_ResidueCompositionTarget`.
_HalflifeTarget = _ResidueCompositionTarget

# Proteolytic stability guidance mode. ``"resist"`` pushes the model away from
# protease-cleavable motifs; ``"target"`` does the inverse (deliberately
# cleavable, used for prodrugs / pro-peptides).
_StabilityMode = Literal["resist", "target"]

# Generator model to run the design with, sent on the wire as
# ``generatorModelId``. Omit it (``model=None``, the default) to let the server
# apply the platform default — the SDK does not carry a copy of that default,
# so a platform-side change reaches you without an SDK upgrade.
#
# - ``"v6.5.21"`` (default) — Receptor-conditioned. Designs respond to the
#   receptor you select. Replaces v6.5, which was trained with all-zero receptor
#   features and produced the same peptides regardless of target.
# - ``"v6.9.21"`` — A different, larger receptor feature space (107
#   per-residue channels vs 48), with classifier-free guidance applied at
#   inference. Validated for distinguishing sequence composition between
#   receptors ONLY — the paired 500-design fold screen (w0 1/284 vs w2 3/216)
#   was underpowered (resolves >=12x, observed 3.9x: not a null, not a
#   positive). No option on this model moves WHERE the peptide contacts the
#   structure — selection, not guidance, delivers placement. NOT strictly
#   better than v6.5: measured contact_logits 0.6281 vs v6.5.21's 0.6719.
#
#   The four guidance heads (``immunogenicity``, ``serum_stability`` /
#   proteolytic, ``residue_composition``, ``cellfree``) ARE supported on this
#   model — the same v6.5 penalty kernels and the same strength calibration,
#   routed through the normal batch-spawn path rather than the streaming guided
#   pool that v6.5/v6.5.21 use. ``immuno_strength`` and ``stability_strength``
#   SATURATE at their ceilings (1.5 and 2.0 respectively — passing higher has
#   no further effect); ``residue_composition_strength`` and
#   ``cellfree_strength`` are clamped at 4.0.
#
#   ⛔ TWO EFFECTS, NOT ONE. The measured -26.7% between-receptor JSD drop at
#   ``residue_composition_strength=4`` (vs an all-knobs-off control) decomposes
#   into a FIXED -17.7 percentage-point cysteine-kernel cost (95% CI [-32.5%,
#   -3.7%], significant) that fires whenever ANY guidance head is enabled at ANY
#   strength including 0, PLUS this knob's own dose share of -11.5%, 95% CI
#   [-27.2%, +4.7%] (NOT statistically significant). The dose share first
#   reaches significance at strength 8 (-22.3%, CI [-38.3%, -6.4%]). Quoting
#   ~28% as this knob's cost at strength 4 sums the two and bills them to the
#   wrong one — that figure is retired, do not reintroduce it.
#
#   ``serum_stability`` / proteolytic and ``residue_composition`` are ONE
#   coupled axis, not independent dials — each measurably moves the other's
#   metric. ``cellfree`` lowers LOCAL HYDROPHOBICITY (rolling 7-residue GRAVY
#   window), NOT K/R clusters — see its own parameter doc.
# - ``"v6.5"`` — Legacy, deprecated. Trained with all-zero receptor features, so
#   its output does not depend on the receptor you select. Retained only to
#   reproduce runs made before August 2026.
#
# This alias is a typing convenience for the ids this SDK build knows about, NOT
# a runtime allowlist: ``model`` is typed ``_GeneratorModel | str | None`` and an
# unrecognized id is forwarded to the server, whose model registry is the
# authority on what exists.
_GeneratorModel = Literal["v6.5.21", "v6.5.22", "v6.9.21", "v6.9.22", "v6.5"]

# Fold partner expansion mode. Controls which receptor chains are included in
# the peptide co-fold complex.
#
#   "target_only"     — fold peptide + ONLY the chain(s) listed in target_chains.
#                       Smallest, fastest fold; clearest single-target interface.
#   "native_complex"  — fold peptide + target chain(s) + their known native
#                       interaction partners from the input structure (e.g.,
#                       BMPR1A + RGMB). Lets the user compare the peptide's
#                       inhibitory effect against the native partner interface.
#   "all_conformations" — fold against every conformation/chain set the platform
#                       has on file for the gene (apo, bound, alternate states).
#                       Most expensive; useful for ensemble validation.
_FoldPartnerMode = Literal["target_only", "native_complex", "all_conformations"]


def _serialize_residue_ranges(
    target_residues: ResidueRange | list[ResidueRange] | dict | list | None,
) -> list[Any] | None:
    """Normalize ``target_residues`` into a JSON-ready list of dicts.

    Accepts a single :class:`ResidueRange`, a list of them, already-serialized
    dicts, or any mix. A bare ``ResidueRange`` is wrapped into a one-element
    list rather than iterated — iterating a pydantic model yields ``(field,
    value)`` tuples, which would silently produce a corrupt pocket spec
    (``[('chain', 'A'), ('start', 140), ...]``) that the server cannot read.
    """
    if target_residues is None:
        return None
    if isinstance(target_residues, ResidueRange):
        items: list[Any] = [target_residues]
    elif isinstance(target_residues, dict):
        # A single already-serialized range dict.
        items = [target_residues]
    elif isinstance(target_residues, (int, float)) and not isinstance(target_residues, bool):
        # A single bare residue number.
        items = [target_residues]
    else:
        items = list(target_residues)

    # ⛔ [bd-dre-i3h4y] A BARE RESIDUE NUMBER MUST GO ON THE WIRE AS A RANGE.
    #
    # `validate_region_list()` (which ran in `_generation_body` before this) has
    # accepted a flat list of PDB resnums since x4vt8 and normalizes it to
    # canonical {chain,start,end} — and then this builder deliberately keeps the
    # ORIGINAL shape so per-region `label`/`description` fields survive. For a
    # dict / ResidueRange that is right. For a bare int it meant the wire carried
    # `targets[0].targetResidues = [140, 141, ...]`, and the server's
    # shape-coercion step (`liftSdkResidueField`, residue-targeting-validation.ts)
    # dropped every non-object entry — so `targetRegions` was never populated,
    # `ptf_generation_runs.target_residues` persisted NULL, no `target_residues`
    # reached FastAPI, and generation ran on the whole-chain geometric-pocket
    # fallback. The call returned success the whole time. MEASURED on all 105
    # receipts of a live campaign (120-206 residues requested per run, NULL
    # persisted on every one).
    #
    # The server now accepts bare integers too (same five shapes as
    # `normalize_region` here and `normalize_regions()` in FastAPI), so this is
    # the second of two independent fixes rather than the only one: it is what
    # makes a flat list work against a server that has NOT taken that fix, and
    # it is what keeps the FastAPI surface (whose `target_residues` field is
    # typed `List[Dict]`) reachable for a caller who bypasses Express.
    #
    # Consecutive resnums are collapsed into one range — identical residue set
    # (the resolver enumerates start..end per region), readable label/diagnostic,
    # and the SAME variantKey as the equivalent hand-written range, which is the
    # correct identity because they request the same residues. The server's
    # collapse rule is the mirror of this one. Non-scalar entries keep their
    # original shape and order.
    passthrough: list[Any] = []
    resnums: list[int] = []
    for r in items:
        if isinstance(r, ResidueRange):
            passthrough.append(r.model_dump(by_alias=True))
        elif isinstance(r, (int, float)) and not isinstance(r, bool):
            resnums.append(int(r))
        else:
            passthrough.append(r)
    if not resnums:
        return passthrough

    ranges: list[dict[str, Any]] = []
    ordered = sorted(set(resnums))
    run_start = prev = ordered[0]
    for n in ordered[1:]:
        if n == prev + 1:
            prev = n
            continue
        ranges.append({"start": run_start, "end": prev})
        run_start = prev = n
    ranges.append({"start": run_start, "end": prev})
    return passthrough + ranges


def _jsonsafe_residue_ranges(value: Any) -> Any:
    """Recursively convert any stray :class:`ResidueRange` to its dict form.

    Final defensive pass over the request body so that residue ranges arriving
    through *any* path (e.g. raw objects passed via ``**extra`` or nested in a
    user-provided dict) are JSON-serializable. httpx serializes ``json=`` via
    ``json.dumps``, which raises ``"Object of type ResidueRange is not JSON
    serializable"`` if even one such object survives.
    """
    if isinstance(value, ResidueRange):
        return value.model_dump(by_alias=True)
    if isinstance(value, dict):
        return {k: _jsonsafe_residue_ranges(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonsafe_residue_ranges(v) for v in value]
    return value


def _generation_target(
    gene: str,
    target_residues: list[ResidueRange] | None = None,
    targeting_strategy: _TargetingStrategy = "full_surface",
    variant_id: int | None = None,
    user_pdb_id: int | None = None,
) -> dict[str, Any]:
    """Build a single PTF target spec for the parallel generate endpoint."""
    target: dict[str, Any] = {"gene": gene, "targetingStrategy": targeting_strategy}
    serialized = _serialize_residue_ranges(target_residues)
    if serialized is not None:
        target["targetResidues"] = serialized
    if variant_id is not None:
        target["variantId"] = variant_id
    if user_pdb_id is not None:
        target["userPdbId"] = user_pdb_id
        target["pdbId"] = f"userpdb_{user_pdb_id}"
        target["activeStructureSource"] = "user_pdb"
    return target


def _generation_body(
    *,
    gene: str,
    num_peptides: int | None,
    length_range: tuple[int, int],
    target_residues: list[ResidueRange] | None,
    avoid_residues: list[ResidueRange] | None,
    # PDB-numbered hotspot residues (flat list), and the Ca-Ca packing-shell
    # radius grown around avoid_residues. [bd-LIGANDAI_ALPHA_V2-x4vt8]
    hotspot_residues: list[int] | None,
    avoid_shell_angstrom: float | None,
    target_chains: list[str] | None,
    fold_partners: _FoldPartnerMode | list[str] | None,
    targeting_strategy: _TargetingStrategy | None,
    target_face: _TargetFace | None,
    pocket_expansion_radius_a: float | None,
    receptor_flexible_residues: Sequence[int] | None,
    auto_fold: bool,
    top_n_fold: int | None,
    ec_domain_trimming: bool,
    deimmunize_mode: bool,
    variant_id: int | None,
    user_pdb_id: int | None,
    gen_gpus: int,
    fold_gpus: int,
    program_id: int | str | None,
    conversation_id: str | None,
    cysteine_mode: _CysteineMode,
    # Tri-state cysteine-kernel switch (bd-LIGANDAI_ALPHA_V2-b4f34):
    #   True  -> the cysteine kernel fires with the given ``cysteine_mode``, and
    #            counts as a guidance request on its own, so cysteine policy can
    #            be requested WITHOUT enabling any other head.
    #   False -> the kernel does NOT fire, whatever ``cysteine_mode`` says.
    #   None  -> legacy behaviour: fires iff guidance is effectively active.
    cysteine_enabled: bool | None,
    quality_guided: bool,
    quality_guidance_scale: float,
    # Generator model selector. ``None`` = omit the key entirely and let the
    # server apply its own default (see ``_GeneratorModel``).
    model: _GeneratorModel | str | None = None,
    immunogenicity: bool,
    immuno_strength: float,
    immuno_modules: dict[str, bool] | None,
    serum_stability: bool,
    stability_strength: float,
    stability_mode: _StabilityMode,
    stability_modules: dict[str, bool] | None,
    # Already resolved from the new + legacy names by
    # ``_resolve_residue_composition`` in the caller — this builder never sees
    # ``halflife=`` and must not grow a second resolution site.
    residue_composition: _ResidueCompositionTarget | None,
    residue_composition_strength: float,
    residue_composition_alias_used: str | None,
    cellfree: bool,
    cellfree_strength: float,
    # Charge / solubility filtering (tier-gated; server activates filtered
    # design worker when any non-default constraint is present).
    charge_mode: _ChargeMode | None,
    charge_value: float | None,
    charge_min: float | None,
    charge_max: float | None,
    min_solubility: float | None,
    # Cyclization (tier-gated: academia/pro/enterprise only).
    cyclic_mode: _CyclicMode | None,
    cyclic_strength: float,
    strict_recombinant: bool,
    dual_fold_viz: bool,
    folding_mode: str | None,
    fold_strategy: str | None,
    folding_conformations: str | list[str] | None,
    max_folds_per_target: int | None,
    enable_expansion: bool | None,
    auto_conformation_expansion: bool | None,
    clash_resolution_enabled: bool | None,
    md_relaxation_enabled: bool | None,
    num_trajectories: int | None,
    sampling_steps: int | None,
    glycosylation_enabled: bool | None,
    segment_config: SegmentConfig | dict | None,
    pdc_config: PdcConfig | dict | None,
    ec_trimming_config: EcTrimmingConfig | dict | None,
    # Ensemble auto-fold (v0.6.7) — opt-in multi-engine Phase-2 auto-fold. When
    # absent / single-Boltz-2, the legacy single-engine auto-fold path runs
    # unchanged (no ensemble keys are emitted). Refs: bd-LIGANDAI_ALPHA_V2-7y1uh
    fold_engines: list[str] | None,
    fold_per_engine: dict[str, Any] | None,
    fold_shared_msa: str | dict[str, Any] | None,
    # Cascade auto-fold (v0.6.7) — Boltz-2 stage-1 gate -> ensemble of ipSAE/ipTM
    # winners. Emits foldCascade/cascadeGate* ONLY when fold_cascade is truthy;
    # absent otherwise (legacy bytes preserved). Refs: bd-LIGANDAI_ALPHA_V2-7y1uh
    fold_cascade: bool,
    cascade_gate_metric: str,
    cascade_gate_threshold: float | None,
    cascade_gate_top_n: int | None,
    # Contrastive (paralog-mean) guidance — steer AWAY from real off-target
    # receptor pockets at sampling time. See ligandai.contrastive.
    contrastive: "ContrastiveGuidance | None",
    allow_unforwarded_contrastive: bool,
    # Name the off-target genes yourself instead of building/fetching a
    # ContrastiveGuidance panel — the customer-reachable surface for the same
    # capability. See ligandai.contrastive.normalize_avoid_paralogs.
    avoid_paralogs: _StrSequence[str] | None = None,
    # SUPERADMIN-ONLY GPU routing. Emitted ONLY when true, so a body from a
    # caller who never heard of this flag is byte-identical to before.
    local: bool = False,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # Client-side targeting validation — BEFORE any HTTP call.
    # [bd-LIGANDAI_ALPHA_V2-x4vt8] target_residues/avoid_residues/hotspot_residues
    # are PDB residue numbers, never 0-based array indices; see
    # ligandai._targeting for the full rule set. target_residues/avoid_residues
    # are validated here but kept in their ORIGINAL shape below (ResidueRange or
    # dict) so per-region `label` fields survive serialization unchanged — only
    # hotspot_residues/avoid_shell_angstrom (which have no prior serialization
    # path) take the validator's normalized return value.
    _targeting_normalized = validate_and_normalize_targeting(
        target_residues=target_residues,
        avoid_residues=avoid_residues,
        hotspot_residues=hotspot_residues,
        avoid_shell_angstrom=avoid_shell_angstrom,
    )
    hotspot_residues = _targeting_normalized["hotspot_residues"]
    avoid_shell_angstrom = _targeting_normalized["avoid_shell_angstrom"]

    # Generator model selection — validated here, in the single body builder both
    # the sync and async generate() paths go through, so neither surface can
    # bypass the model/guidance compatibility check.
    generator_model_id = _resolve_generator_model(
        model,
        immunogenicity=immunogenicity,
        immuno_modules=immuno_modules,
        serum_stability=serum_stability,
        stability_modules=stability_modules,
        residue_composition=residue_composition,
        cellfree=cellfree,
        cysteine_enabled=cysteine_enabled,
        extra=extra,
    )

    # Auto-resolve targeting_strategy when not explicitly set:
    #   * target_residues present → "pocket_targeted" (treat residues as a real pocket)
    #   * otherwise → "full_surface"
    # This prevents the silent "I gave you a hotspot but you used full_surface
    # and ignored it" footgun that pre-2026-05-07 jobs hit.
    effective_strategy: _TargetingStrategy = (
        targeting_strategy
        if targeting_strategy is not None
        else ("pocket_targeted" if target_residues else "full_surface")
    )

    body: dict[str, Any] = {
        "targets": [_generation_target(gene, target_residues, effective_strategy, variant_id, user_pdb_id)],
        "lengthRange": list(length_range),
        "autoFoldEnabled": auto_fold,
        "ecDomainTrimming": ec_domain_trimming,
        "deimmunizeMode": deimmunize_mode,
        "genParallelCount": gen_gpus,
        "foldingGpus": fold_gpus,
        "cysteineMode": _validate_cysteine_mode(cysteine_mode),
        # Guidance modules. Quality-guided generation is free+; immunogenicity,
        # serum stability, and logits-style outputs are academia+.
        "qualityGuidedEnabled": quality_guided,
        "qualityGuidanceScale": quality_guidance_scale,
        "immunoEnabled": immunogenicity,
        "immunoStrength": immuno_strength,
        "stabilityEnabled": serum_stability,
        "stabilityStrength": stability_strength,
        "stabilityMode": stability_mode,
        # ── Residue & terminal composition (formerly "half-life") ────────────
        # Emitted under BOTH the canonical and the legacy wire key, on purpose.
        #
        # A renamed knob has exactly two failure modes and they pull in opposite
        # directions: send only the NEW key and a server that has not taken the
        # rename yet DROPS the knob — a silent no-op, which is the precise defect
        # this workstream exists to remove. Send only the LEGACY key and the SDK
        # is the last thing keeping a dead name alive. Sending both is the only
        # option that cannot silently no-op against either server generation.
        # The values are always identical, so the server's alias resolver takes
        # the "both supplied and they agree" branch — no conflict, one
        # deprecation note in provenance.
        #
        # The legacy keys come out when the renamed server is the only server in
        # every environment. That is a deliberate, separately-verified step, not
        # something to tidy up in passing.
        "residueCompositionEnabled": residue_composition is not None,
        "halflifeEnabled": residue_composition is not None,
        "cellfreeEnabled": cellfree,
        "cellfreeStrength": cellfree_strength,
    }
    # Generator model. Emitted ONLY when the caller named one — an absent key
    # means "server default", and the server default is the one place the
    # default is allowed to live. Writing the current default in here would
    # pin every SDK build to whatever the default was on its release day.
    if generator_model_id is not None:
        body["generatorModelId"] = generator_model_id
    if residue_composition is not None:
        body["residueCompositionTarget"] = residue_composition
        body["residueCompositionStrength"] = residue_composition_strength
        body["halflifeTarget"] = residue_composition
        body["halflifeStrength"] = residue_composition_strength
    # Provenance: record WHICH name the caller reached the knob through, so a run
    # that used the deprecated spelling is traceable after the fact rather than
    # only at call time. Absent when the canonical name was used.
    if residue_composition_alias_used is not None:
        body["residueCompositionAliasUsed"] = residue_composition_alias_used
    # Tri-state cysteine switch. Emitted ONLY when the caller took a position:
    # an absent key preserves the legacy "fires iff guidance is effectively
    # active" resolution, which is what every existing caller already gets.
    if cysteine_enabled is not None:
        body["cysteineEnabled"] = cysteine_enabled
    if num_peptides is not None:
        body["peptidesPerTarget"] = num_peptides
    if top_n_fold is not None:
        body["maxFoldsPerTarget"] = top_n_fold
    if max_folds_per_target is not None:
        body["maxFoldsPerTarget"] = max_folds_per_target
    if folding_mode is not None:
        body["foldingMode"] = folding_mode
    if fold_strategy is not None:
        body["foldStrategy"] = fold_strategy
    if folding_conformations is not None:
        body["foldingConformations"] = folding_conformations
    if enable_expansion is not None:
        body["enableExpansion"] = enable_expansion
    if auto_conformation_expansion is not None:
        body["autoConformationExpansion"] = auto_conformation_expansion
    if clash_resolution_enabled is not None:
        body["clashResolutionEnabled"] = clash_resolution_enabled
    if md_relaxation_enabled is not None:
        body["mdRelaxationEnabled"] = md_relaxation_enabled
    if num_trajectories is not None:
        body["numTrajectories"] = num_trajectories
        body["diffusionSamples"] = num_trajectories
    if sampling_steps is not None:
        body["samplingSteps"] = sampling_steps
    if glycosylation_enabled is not None:
        body["glycosylationEnabled"] = glycosylation_enabled
    # Program linkage (v0.7.0): accept both numeric DB id and public string id,
    # plus conversation_id for the convo-dedup path. Server resolution ladder:
    # programDbId → programId → convo-dedup → gene-hash → recent-by-genes → create-new.
    if program_id is not None:
        if isinstance(program_id, int):
            body["programDbId"] = program_id
            body["program_db_id"] = program_id
        else:
            body["programId"] = str(program_id)
            body["program_id"] = str(program_id)
    if conversation_id is not None:
        body["conversationId"] = conversation_id
        body["conversation_id"] = conversation_id
    # Optional immuno / stability sub-modules (dict of booleans per protease/epitope)
    if immuno_modules is not None:
        body["immunoModules"] = immuno_modules
    if stability_modules is not None:
        body["stabilityModules"] = stability_modules
    # Charge / solubility filtering — only send non-None values; server uses
    # its own defaults when these keys are absent.
    if charge_mode is not None:
        body["chargeMode"] = charge_mode
    if charge_value is not None:
        body["chargeValue"] = charge_value
    if charge_min is not None:
        body["chargeMin"] = charge_min
    if charge_max is not None:
        body["chargeMax"] = charge_max
    if min_solubility is not None:
        body["minSolubility"] = min_solubility
    # Cyclization — only send when explicitly requested (non-None + non-"none").
    # The server reads this via req.body.cyclicMode; tier gate is enforced
    # server-side (HTTP 403 for insufficient tiers), but we also document it here.
    if cyclic_mode is not None and cyclic_mode != "none":
        body["cyclicMode"] = cyclic_mode
        body["cyclicStrength"] = cyclic_strength
        body["strictRecombinant"] = strict_recombinant
        if dual_fold_viz:
            body["dualFoldViz"] = dual_fold_viz
    # Multi-segment scaffold config (binding/linker/stability/premade segments).
    if segment_config is not None:
        from ligandai.types import SegmentConfig
        body["segmentConfig"] = (
            segment_config.model_dump(by_alias=True)
            if isinstance(segment_config, SegmentConfig)
            else segment_config
        )
    # Peptide-Drug Conjugate configuration (Pro+ tier).
    if pdc_config is not None:
        from ligandai.types import PdcConfig
        body["pdcConfig"] = (
            pdc_config.model_dump(by_alias=True)
            if isinstance(pdc_config, PdcConfig)
            else pdc_config
        )
        body["pdcEnabled"] = True
    # Fine-grained EC trimming / structure preparation.
    if ec_trimming_config is not None:
        from ligandai.types import EcTrimmingConfig
        cfg = (
            ec_trimming_config.model_dump(by_alias=True)
            if isinstance(ec_trimming_config, EcTrimmingConfig)
            else ec_trimming_config
        )
        body["ecTrimming"] = cfg
    # Hotspot → pocket expansion. When the user passes a single residue
    # (or a small set), the server includes every residue within
    # ``pocket_expansion_radius_a`` Å of any hotspot atom in the design
    # pocket. Default 6.0 Å mirrors the platform's "shell of contacts"
    # pocket convention. ``0.0`` is a valid and meaningful value — it
    # disables expansion so only the literal listed residues are used
    # (the server reads pocketRadiusA=0 as "hotspots-only, no expansion").
    # MUST check ``is not None`` (not truthiness): 0.0 is falsy but real.
    if target_residues and pocket_expansion_radius_a is not None and pocket_expansion_radius_a >= 0:
        body["pocketRadiusA"] = float(pocket_expansion_radius_a)
        body["pocketExpansionRadiusA"] = float(pocket_expansion_radius_a)
        body["expandHotspotPocket"] = pocket_expansion_radius_a > 0

    # ⛔ [bd-dre-i3h4y] TARGET REGIONS GO ON THE WIRE AT THE TOP LEVEL TOO, the
    # same way avoid_residues already does immediately below.
    #
    # `avoid_residues` has always been emitted as the INTERNAL key the Express
    # handler destructures (`avoidRegions`), while `target_residues` was emitted
    # ONLY as `targets[0].targetResidues` and relied on one server-side lift
    # (`liftSdkResidueFieldsOnBody`) to reach `targetRegions`. That made the
    # entire pocket-targeting transport depend on a single coercion step, and
    # when that step dropped bare integers the field vanished with a 200 OK:
    # measured NULL `ptf_generation_runs.target_residues` on 105/105 runs of a
    # live campaign. Emitting the canonical key here removes the single point of
    # failure and makes the call work against a server that has not taken the
    # lift fix. Per-region `label`/`description` are carried through, and
    # `targets[0].targetResidues` still carries the caller's original shape, so
    # nothing that reads either key loses information. Express precedence: an
    # explicitly-provided `targetRegions` wins and the lift is a no-op.
    if target_residues is not None:
        _serialized_target = _serialize_residue_ranges(target_residues)
        if _serialized_target:
            body["targetRegions"] = [
                {
                    # null when unscoped — never guessed (x4vt8).
                    "chain": r.get("chain"),
                    "start": r["start"] if "start" in r else r["startResidue"],
                    "end": r["end"] if "end" in r else r["endResidue"],
                    "startResidue": r["start"] if "start" in r else r["startResidue"],
                    "endResidue": r["end"] if "end" in r else r["endResidue"],
                    **({"description": r["description"]} if r.get("description") else {}),
                    **({"label": r["label"]} if r.get("label") else {}),
                }
                for r in _serialized_target
                if isinstance(r, dict)
            ]

    # Avoid residues — receptor regions the caller wants kept clear. Serialized
    # as ``avoidRegions`` (top-level body key), which the Express layer reads
    # alongside ``targetRegions`` and forwards to FastAPI as ``avoid_residues``.
    #
    # start/end are PDB RESIDUE NUMBERS as numbered in the receptor structure
    # (not indices into any internal array); ``chain`` is honoured. The server
    # removes those residues, plus a CA-CA packing shell around them
    # (``avoid_shell_angstrom``, default 8.0 A), from the residue set the
    # generator is featurized on. Applied in EVERY targeting mode — no
    # target_residues required.
    #
    # ⚠ It does NOT keep designs off that surface and does NOT change what is
    # generated: measured p = 1.00 / JSD 2.4e-5 between arms, with gate-passing
    # designs still contacting the excluded patch. Avoidance is established on
    # the folded complex, never from this field.
    # Uses the same ResidueRange shape as target_residues.
    if avoid_residues is not None:
        serialized_avoid = _serialize_residue_ranges(avoid_residues)
        if serialized_avoid is not None:
            # Emit BOTH the Express-legacy {chain, startResidue, endResidue}
            # shape AND the canonical {chain, start, end} shape (contract
            # §"SDK rule": SDKs emit the canonical form; extra keys are
            # harmless/ignored by any reader that only looks at one shape).
            # [bd-LIGANDAI_ALPHA_V2-x4vt8] Values are validated (start/end
            # are ints >= 1) by validate_and_normalize_targeting() above, so
            # NO fallback default is used here — a missing start/end at this
            # point is a bug in the validator, not something to paper over
            # with a silent 0 (0 is exactly the array-index mistake this
            # bead exists to kill).
            body["avoidRegions"] = [
                {
                    # ⛔ [bd-dre-i3h4y] `r.get("chain", "A")` GUESSED CHAIN A for
                    # any region the caller did not scope — the exact thing the
                    # x4vt8 rule three lines above forbids, and that every other
                    # implementation of this rule (normalize_region here,
                    # liftSdkResidueField / canonicalizeRegionsForVariantKey in
                    # Express, normalize_regions in FastAPI) already gets right by
                    # preserving null. A fabricated chain short-circuits FastAPI's
                    # own fallback order (region chain -> active_chains ->
                    # chain-agnostic resnum match), so on a differently-labelled
                    # chain that reuses the same resnum the exclusion silently
                    # lands on the WRONG residue. null means "no chain given".
                    "chain": r.get("chain"),
                    "start": r["start"] if "start" in r else r["startResidue"],
                    "end": r["end"] if "end" in r else r["endResidue"],
                    "startResidue": r["start"] if "start" in r else r["startResidue"],
                    "endResidue": r["end"] if "end" in r else r["endResidue"],
                }
                for r in serialized_avoid
                if isinstance(r, dict)
            ]

    # Ca-Ca packing-shell radius (Angstrom) grown around avoid_residues.
    # None = server default (8.0 A, residue_targeting.DEFAULT_SHELL_CA_ANGSTROM);
    # 0 disables the shell (named residues only). Validated by
    # validate_and_normalize_targeting() above (0-50 A range).
    # [bd-LIGANDAI_ALPHA_V2-x4vt8] Dual-emitted camelCase + snake_case, matching
    # this body builder's existing convention for program_db_id/conversation_id.
    if avoid_shell_angstrom is not None:
        body["avoidShellAngstrom"] = avoid_shell_angstrom
        body["avoid_shell_angstrom"] = avoid_shell_angstrom

    # Hotspot residues — flat list of PDB residue numbers (not indices; see
    # target_residues/avoid_residues above for the same rule). Distinct from
    # pocket_expansion_radius_a's hotspot-adjacent expansion: this is the
    # literal residue list forwarded to FastAPI's `hotspot_residues` field.
    # [bd-LIGANDAI_ALPHA_V2-x4vt8]
    if hotspot_residues:
        body["hotspotResidues"] = hotspot_residues
        body["hotspot_residues"] = hotspot_residues

    # Receptor flexible residues — 1-based receptor residue indices the backend
    # is allowed to relax during generation/docking (side-chain / limited
    # backbone flexibility at those positions). Emitted as ``flexibleResidues``
    # only when provided, so legacy bytes are preserved for the common case.
    if receptor_flexible_residues:
        flex = [int(r) for r in receptor_flexible_residues]
        if flex:
            body["flexibleResidues"] = flex

    # Restrict generation to specific receptor chains (multimer support).
    # Server reads ``config.targetChains`` and filters conformations / restricts
    # the binding surface to only the listed chain IDs.
    if target_chains is not None:
        normalized_chains = [str(c).upper() for c in target_chains if c]
        if normalized_chains:
            body["targetChains"] = normalized_chains

    # Fold partner expansion mode. Three explicit user intents map to the
    # underlying server flags:
    #
    #   "target_only"       -> foldingConformations="generation",
    #                          autoConformationExpansion=false
    #                          (peptide + selected target chain only)
    #   "native_complex"    -> foldingConformations="native",
    #                          autoConformationExpansion=false
    #                          (peptide + target + native interaction partners)
    #   "all_conformations" -> foldingConformations="all",
    #                          autoConformationExpansion=true
    #                          (full ensemble across receptor conformations)
    #   list[str]           -> foldingConformations=<list of conformation names>
    #                          (explicit conformation set)
    if fold_partners is not None:
        if isinstance(fold_partners, list):
            body["foldingConformations"] = fold_partners
        elif fold_partners == "target_only":
            body["foldingConformations"] = "generation"
            body["autoConformationExpansion"] = False
            body["enableExpansion"] = False
            body["foldPartnerMode"] = "target_only"
        elif fold_partners == "native_complex":
            body["foldingConformations"] = "native"
            body["autoConformationExpansion"] = False
            body["foldPartnerMode"] = "native_complex"
        elif fold_partners == "all_conformations":
            body["foldingConformations"] = "all"
            body["autoConformationExpansion"] = True
            body["enableExpansion"] = True
            body["foldPartnerMode"] = "all_conformations"

    # Ensemble auto-fold (v0.6.7). Reuse the exact cofold validation helpers so
    # there is one engine/per-engine/shared-MSA validation surface. The legacy
    # single-Boltz-2 auto-fold is preserved byte-for-byte: keys are emitted ONLY
    # when the request resolves to a REAL ensemble (>1 engine, or any engine that
    # is not boltz2). ``fold_engines=None`` or ``["boltz2"]`` emits NOTHING.
    # Bills PER ENGINE, free-pays-credits (any tier with a positive balance),
    # mirroring ``cofold()``. Refs: bd-LIGANDAI_ALPHA_V2-7y1uh
    if fold_engines:
        norm_fold_engines = _cofold_normalize_engines(fold_engines)
        is_real_ensemble = (
            len(norm_fold_engines) > 1
            or any(e != "boltz2" for e in norm_fold_engines)
        )
        if is_real_ensemble:
            body["foldEngines"] = norm_fold_engines
            clean_fold_per_engine = _cofold_sanitize_per_engine(fold_per_engine)
            if clean_fold_per_engine:
                body["foldPerEngine"] = clean_fold_per_engine
            # Single shared OUR-OWN MSA for the whole ensemble; never per-engine,
            # never external. Normalize to {"mode": "auto"|"none"} exactly as
            # ``cofold()`` does (default "auto").
            if fold_shared_msa is None:
                body["foldSharedMsa"] = {"mode": "auto"}
            elif isinstance(fold_shared_msa, str):
                _msa_mode = fold_shared_msa.strip().lower()
                if _msa_mode not in ("auto", "none"):
                    raise ValueError("fold_shared_msa must be 'auto' or 'none'")
                body["foldSharedMsa"] = {"mode": _msa_mode}
            elif isinstance(fold_shared_msa, dict):
                _msa_mode = str(fold_shared_msa.get("mode", "auto")).strip().lower()
                if _msa_mode not in ("auto", "none"):
                    raise ValueError("fold_shared_msa.mode must be 'auto' or 'none'")
                body["foldSharedMsa"] = {"mode": _msa_mode}
            else:
                raise TypeError(
                    "fold_shared_msa must be a str ('auto'|'none') or a dict"
                )

    # Cascade auto-fold (v0.6.7). Stage 1 folds the stratified top-N peptides on
    # Boltz-2 ONLY; the ipSAE/ipTM winners then run the OTHER selected engines
    # (the ensemble MINUS boltz2 — boltz2 is reused from stage 1, never re-folded).
    # Cascade REQUIRES boltz2 in fold_engines (it is the stage-1 gate). Keys are
    # emitted ONLY when fold_cascade is truthy; absent otherwise (legacy bytes
    # preserved). Refs: bd-LIGANDAI_ALPHA_V2-7y1uh
    if fold_cascade:
        _norm_cascade_engines = _cofold_normalize_engines(fold_engines or [])
        if "boltz2" not in _norm_cascade_engines:
            raise ValueError(
                "fold_cascade requires 'boltz2' in fold_engines — Boltz-2 is the "
                "stage-1 gate whose ipSAE/ipTM winners advance to the ensemble."
            )
        _gate_metric = str(cascade_gate_metric or "ipsae").strip().lower()
        if _gate_metric not in ("ipsae", "iptm"):
            raise ValueError(
                "cascade_gate_metric must be 'ipsae' or 'iptm'"
            )
        body["foldCascade"] = True
        body["cascadeGateMetric"] = _gate_metric
        if cascade_gate_threshold is not None:
            body["cascadeGateThreshold"] = float(cascade_gate_threshold)
        if cascade_gate_top_n is not None:
            _top_n = int(cascade_gate_top_n)
            if _top_n <= 0:
                raise ValueError("cascade_gate_top_n must be a positive integer")
            body["cascadeGateTopN"] = _top_n

    # Receptor topology target face (bd-LIGANDAI_ALPHA_V2-q3z1b). Sent ONLY when
    # the caller named a face — an unset face leaves the body field absent so the
    # backend featurizer applies its receptor default (extracellular + TM, IC
    # excluded). When present, the Express /api/ptf/parallel/generate handler
    # resolves it to the matching membrane-topology residue ranges and sets them
    # as the explicit-pocket target_residues (extracellular→NC, transmembrane→TM,
    # intracellular→CY, ec_tm→NC+TM, full→no restriction).
    if target_face is not None:
        body["targetFace"] = target_face

    # ── Contrastive (paralog-mean) guidance ─────────────────────────────────
    # Emitted ONLY when the caller supplied a panel. A v6.9-line run without one
    # is still contrastive — the platform derives its own real paralog
    # references and refuses the run if it cannot produce any — this is how you
    # CHOOSE the panel instead of accepting the derived one.
    #
    # ⛔ require_forwardable() is the pre-flight that stops this becoming a knob
    # that reads ON and executes OFF. It no longer refuses the forward itself —
    # the passthrough exists (Express -> FastAPI -> the v6.9.21/v6.9.22 runtimes
    # and the delta resident) — but it still refuses every panel that could only
    # corrupt the run silently: an empty panel, an all-zero reference, a width
    # that is not the model's declared width, a duplicate gene, and a v6.5-line
    # generator that has no CFG reference operator at all. See
    # ligandai.contrastive for what each one costs if it is not caught here.
    if contrastive is not None:
        contrastive.require_forwardable(
            model_id=generator_model_id,
            allow_unforwarded=bool(allow_unforwarded_contrastive),
        )
        body.update(contrastive.to_wire())

    # ── avoid_paralogs (bd-dre-siz9d) ───────────────────────────────────────
    # The customer-reachable way to NAME the off-targets: restricts the
    # server-side paralog reference producer to these genes instead of
    # building/fetching a ContrastiveGuidance panel from raw pocket features
    # (which needs an entitlement most callers do not have — see
    # Campaigns.pocket_features). Emitted ONLY when supplied, under both
    # spellings, mirroring ContrastiveGuidance.to_wire().
    #
    # ⛔ Two ways to name one panel. `contrastive=` already IS a chosen panel;
    # accepting both and silently preferring one is how a caller ends up
    # steered away from something they did not choose.
    if avoid_paralogs is not None and contrastive is not None:
        raise LigandAIValidationError(
            "pass either avoid_paralogs=[...] or contrastive=ContrastiveGuidance(...), "
            "not both — they are two ways of naming the same off-target panel, and "
            "silently preferring one would steer generation away from receptors "
            "the caller did not choose to name that way."
        )
    _normalized_avoid_paralogs = normalize_avoid_paralogs(avoid_paralogs)
    if _normalized_avoid_paralogs is not None:
        require_avoid_paralogs_model(generator_model_id)
        body.update(avoid_paralogs_to_wire(_normalized_avoid_paralogs))

    # ── SUPERADMIN-ONLY local (on-prem) GPU routing [bd-LIGANDAI_ALPHA_V2-d7b23]
    # ⛔ EMITTED ONLY WHEN TRUE, and never as `false`. The platform's gate is
    # "falsy is a complete no-op" (fastapi_backend/middleware/local_execution.py
    # and the Express gateLocalGpuFlag) — sending `local: false` on every request
    # would be harmless but would change the wire body for 100% of callers, and
    # the whole design of this flag is that the 99.9% who never set it pay no
    # auth round-trip and see a byte-identical request.
    # ⛔ A non-superadmin key that sets this gets HTTP 403 from the server, NOT a
    # silent downgrade to Modal. That is deliberate and the SDK does not soften
    # it: a caller who was quietly sent to the other machine would have a
    # different bill and a different confidentiality posture with nothing in the
    # response saying so.
    if local:
        body["local"] = True

    if extra:
        body.update(extra)
    # Final defensive pass: convert any stray ResidueRange (e.g. arriving via
    # **extra, or nested in a user-provided dict) into its JSON dict form so the
    # body is always json.dumps-able regardless of how residues entered it.
    return _jsonsafe_residue_ranges(body)


def _resolve_segment_config(
    segments: "list[PeptideSegment] | list[dict] | None",
    segment_config: "SegmentConfig | dict | None",
) -> "SegmentConfig | dict | None":
    """Reconcile the ``segments=`` convenience kwarg with ``segment_config=``.

    ``segments`` is a convenience for the common motif-grafting case: pass a
    plain ordered list of :class:`PeptideSegment` (premade + generated flanks)
    and the SDK wraps them into ``SegmentConfig(mode="custom", segments=...)``.
    Segment ``id``/``position`` auto-assign from list order (see
    :class:`SegmentConfig`), so callers never hand-number.

    Precedence: if ``segment_config`` is given explicitly it wins (and passing
    both raises, to avoid a silent ambiguity). Plain dicts in ``segments`` are
    coerced to :class:`PeptideSegment` so auto-numbering still applies.
    """
    from ligandai.types import PeptideSegment, SegmentConfig

    if segments is None:
        return segment_config
    if segment_config is not None:
        raise ValueError(
            "Pass either segments=[...] or segment_config=SegmentConfig(...), "
            "not both. Use segment_config for full control (mode/length_range); "
            "use segments for the common 'list of segments' case."
        )
    coerced: list[PeptideSegment] = [
        s if isinstance(s, PeptideSegment) else PeptideSegment(**s) for s in segments
    ]
    return SegmentConfig(mode="custom", segments=coerced)


_VALID_FOLD_APPROACHES = ("boltz2_affinity", "esmfold2", "esmfold2_fast")


def _legacy_queue_model(approach: str) -> str:
    """Map a ``fold_approach`` onto the legacy folding-queue ``model`` vocabulary.

    ``/api/folding/{submit,predict,batch}`` accept only ``boltz2`` or ``alphafold2``
    — that field selects the queue lane, not the engine. Every fold_approach the SDK
    supports runs on the boltz2 lane; the engine itself is carried separately in
    ``fold_approach``/``foldApproach``, which the backend reads.

    ``alphafold2`` is reachable only by passing ``fold_approach="alphafold2"``
    explicitly, and is preserved here so that path keeps working.
    """
    return "alphafold2" if approach == "alphafold2" else "boltz2"


def _resolve_fold_approach(fold_approach: str | None) -> str:
    """Normalize / validate ``fold_approach`` before HTTP submit.

    Default = ``"boltz2_affinity"`` — current production gold standard.
    Aliases: ``"boltz2"`` and ``"boltz"`` collapse to ``"boltz2_affinity"``.
    """
    if fold_approach is None or fold_approach == "":
        return "boltz2_affinity"
    fa = str(fold_approach).strip().lower().replace("-", "_")
    if fa in ("boltz2", "boltz", "boltz_2", "boltz_affinity"):
        return "boltz2_affinity"
    if fa == "esmfold":
        return "esmfold2"
    if fa not in _VALID_FOLD_APPROACHES:
        raise ValueError(
            f"fold_approach must be one of {_VALID_FOLD_APPROACHES}, got {fold_approach!r}"
        )
    return fa


def _fold_body(
    sequences: list[Sequence | str | dict[str, Any]],
    *,
    auto_score: bool = True,
    template_mode: bool = False,
    msa_enabled: bool | None = None,
    target_gene: str | None = None,
    glycosylation: bool | None = None,
    pegylation: bool | None = None,
    gpu_count: int = 1,
    diffusion_samples: int = 1,
    sampling_steps: int | None = None,
    recycling_steps: int | None = None,
    num_trajectories: int | None = None,
    step_scale: float | None = None,
    contribute_to_receptordb: bool | None = None,
    n_parallel_gpus: int | None = None,
    # / approach selection
    fold_approach: str | None = None,
    num_seeds: int | None = None,
    num_recycles: int | None = None,
    return_pdb: bool | None = None,
    use_potentials: bool | None = None,
) -> dict[str, Any]:
    """Build the body for ``POST /api/folding/predict``.

    Single sequence → ``{model, sequence}``. Multiple → ``{model, entities}``.

    ``fold_approach`` selects the upstream design worker:
      - ``"boltz2_affinity"`` (default) — gold-standard 2-chain Boltz-2 + affinity head
      - ``"esmfold2"`` — single-sequence ESMFold2 on B200+ (~3-5 s/peptide)
      - ``"esmfold2_fast"`` — LF-Pose v3 (50 ms warm-pool variant)

    ``use_potentials`` controls Boltz-2 physics-based steering potentials
    (boltz ``--use_potentials``). Platform default is now ON
    (bd-LIGANDAI_ALPHA_V2-j2kc5). When True/False the body carries both
    ``usePotentials`` and ``use_potentials`` (either casing reaches the platform) so
    the caller's choice is explicit and overrides the server default. When None the
    key is omitted and the server's default (ON) applies. Boltz-2 path only; ignored
    by ESMFold approaches.
    """
    approach = _resolve_fold_approach(fold_approach)
    normalized = [_norm_seq(s) for s in sequences]
    # Effective trajectory count: prefer num_seeds when explicitly set,
    # otherwise fall back to num_trajectories then diffusion_samples.
    effective_samples = (
        int(num_seeds) if num_seeds is not None
        else (num_trajectories if num_trajectories is not None else diffusion_samples)
    )
    body: dict[str, Any] = {
        # `model` and `fold_approach` are DIFFERENT server vocabularies and must not
        # be conflated:
        #   model         -> the legacy folding queue lane, strictly boltz2|alphafold2
        #                    (server/routes.ts /api/folding/{submit,predict,batch};
        #                     job_queue.ts types it as 'boltz2' | 'alphafold2')
        #   fold_approach -> the engine: boltz2_affinity | esmfold2 | esmfold2_fast |
        #                    protenix_v2 | openfold3 | promera | opendde
        # This sent the fold_approach value in `model`, so a default fold posted
        # model="boltz2_affinity" and the route replied
        # 400 "Invalid model. Choose boltz2 or alphafold2" — i.e. peptides.fold()
        # was broken against production for every caller who did not override it.
        # The engine still travels in foldApproach/fold_approach below, so engine
        # selection is unaffected; only the queue lane is now named correctly.
        "model": _legacy_queue_model(approach),
        "foldApproach": approach,
        "fold_approach": approach,
        "gpuCount": gpu_count,
        "diffusionSamples": effective_samples,
        "templateMode": template_mode,
        "autoScore": auto_score,
    }
    if num_seeds is not None:
        body["numSeeds"] = int(num_seeds)
        body["num_seeds"] = int(num_seeds)
    if num_recycles is not None:
        body["numRecycles"] = int(num_recycles)
        body["num_recycles"] = int(num_recycles)
    if return_pdb is not None:
        body["returnPdb"] = bool(return_pdb)
        body["return_pdb"] = bool(return_pdb)
    # Physics-based steering potentials. Platform default ON (bd-..-j2kc5). Emit both
    # casings so the Express proxy + FastAPI (AliasChoices) accept it. Explicit True OR
    # False is forwarded (lets a caller override the server default); only None omits.
    if use_potentials is not None:
        body["usePotentials"] = bool(use_potentials)
        body["use_potentials"] = bool(use_potentials)
    if sampling_steps is not None:
        body["samplingSteps"] = sampling_steps
    if recycling_steps is not None:
        body["recyclingSteps"] = recycling_steps
    if num_trajectories is not None:
        body["numTrajectories"] = num_trajectories
    if step_scale is not None:
        body["stepScale"] = step_scale
    if contribute_to_receptordb is not None:
        body["contributeToReceptordb"] = contribute_to_receptordb
        body["submitToCommunity"] = contribute_to_receptordb
    # explicit parallel-GPU cap (tier-validated server-side via tier GPU limits).
    # When None, the server picks a tier-appropriate default. When set above the
    # caller's tier cap the server returns 400 with the cap value in the error body.
    if n_parallel_gpus is not None:
        body["nParallelGpus"] = int(n_parallel_gpus)
        # Mirror snake_case for platform consumers
        body["n_parallel_gpus"] = int(n_parallel_gpus)
    if target_gene is not None:
        body["targetGeneName"] = target_gene
    if msa_enabled is not None:
        body["msaEnabled"] = msa_enabled
    if glycosylation:
        body["glycosylation"] = {"enabled": True}
    if pegylation:
        body["pegylation"] = {"enabled": True}

    if len(normalized) == 1 and not normalized[0].get("chainId"):
        body["sequence"] = normalized[0]["sequence"]
        if "name" in normalized[0]:
            body["name"] = normalized[0]["name"]
    else:
        body["entities"] = [
            _fold_entity(s, i) for i, s in enumerate(normalized)
        ]
    return body


#: Role strings the platform accepts as "this chain is a designed binder".
#: Mirrors ``fastapi_backend/services/msa_prefetch_guard.py::_is_flagged_peptide``.
PEPTIDE_ROLES: frozenset[str] = frozenset({"peptide", "binder", "ligand_peptide"})


def is_peptide_chain(entity: dict[str, Any]) -> bool:
    """True when a fold chain carries ANY de novo peptide / no-MSA signal.

    Accepts every spelling the platform honors — ``isPeptide``, ``is_peptide``,
    ``use_msa=False``, and ``role``/``entity_role``/``chain_role`` in
    :data:`PEPTIDE_ROLES`. One is sufficient. Mirrors the server-side guard
    ``msa_prefetch_guard._is_flagged_peptide`` so the SDK and the platform agree
    on what counts as a flag.
    """
    if entity.get("isPeptide") is True or entity.get("is_peptide") is True:
        return True
    if entity.get("use_msa") is False:
        return True
    for role_key in ("role", "entity_role", "chain_role"):
        role = entity.get(role_key)
        if isinstance(role, str) and role.strip().lower() in PEPTIDE_ROLES:
            return True
    return False


def _fold_entity(s: dict[str, Any], index: int) -> dict[str, Any]:
    """Build one wire entity for ``POST /api/folding/predict``.

    ⛔ ONE CALLER FLAG MUST BECOME EVERY WIRE SPELLING. [bd-dre-yt8kx]

    Four independent consumers decide "is this chain a designed binder" and they
    do NOT read the same keys:

      * ``msa_prefetch_guard._is_flagged_peptide`` — isPeptide, is_peptide,
        use_msa=False, role/entity_role/chain_role
      * ``modal_workers/boltz2.py`` (~2567)  — the same four
      * ``modal_workers/boltz2_batch.py`` (~403) — ``isPeptide`` and
        ``use_msa`` ONLY
      * ``modal_workers/boltz2_fda_v2.py`` (~205) — ``isPeptide`` and
        ``use_msa`` ONLY

    So a caller who sets only ``role="binder"`` (or only snake-case
    ``is_peptide``) clears the prefetch guard and is still folded as a RECEPTOR
    by the batch and FDA workers — which then demand an MSA the design cannot
    have. Normalizing any one signal to ``isPeptide`` + ``is_peptide`` +
    ``use_msa: False`` is what makes a single caller-side flag actually hold all
    the way to the GPU. Do not "simplify" this back to forwarding whatever the
    caller happened to type.
    """
    entity: dict[str, Any] = {
        "type": s.get("type", "protein"),
        "chainId": s.get("chainId") or chr(ord("A") + index),
        "sequence": s["sequence"],
    }
    if "name" in s:
        entity["name"] = s["name"]
    if "geneName" in s:
        entity["geneName"] = s["geneName"]
    if "role" in s:
        entity["role"] = s["role"]
    # A non-polypeptide (ligand / dna / rna) is never MSA'd and is never
    # labelled a peptide — it carries its own Boltz-2 entity type instead.
    if entity["type"] != "protein":
        return entity
    if is_peptide_chain(s):
        # De novo binders are orphan sequences: the local MSA service runs a full
        # mmseqs search and then rejects the result as "Degenerate MSA: only 1
        # sequence", and when it cannot answer at all the fold is REFUSED for an
        # alignment that cannot exist. bd-LIGANDAI_ALPHA_V2-5319n / bd-dre-yt8kx.
        entity["isPeptide"] = True
        entity["is_peptide"] = True
        entity["use_msa"] = False
    elif s.get("use_msa") is True:
        # Explicit receptor opt-IN travels too, so a caller can be unambiguous.
        entity["use_msa"] = True
    return entity


def _norm_seq(s: Sequence | str | dict[str, Any]) -> dict[str, Any]:
    """Normalize one fold chain into the flat dict ``_fold_body`` consumes.

    ⛔ The ``Sequence`` branch used to emit ONLY sequence/name/geneName/chainId,
    so every peptide/no-MSA flag a caller set on a typed ``Sequence`` was
    dropped before the body was built — and ``Sequence.msa`` was never read at
    all. A caller who wrote ``Sequence(sequence=design, msa=False)`` got a chain
    that the platform folded as a RECEPTOR and demanded an MSA for. Keep the
    flags travelling. [bd-dre-yt8kx]
    """
    if isinstance(s, str):
        return {"sequence": s}
    if isinstance(s, Sequence):
        out: dict[str, Any] = {"sequence": s.sequence}
        if s.name:
            out["name"] = s.name
        if s.target_gene:
            out["geneName"] = s.target_gene
        if s.target_chain:
            out["chainId"] = s.target_chain
        if s.type:
            out["type"] = s.type
        if s.role:
            out["role"] = s.role
        if s.is_peptide is not None:
            out["isPeptide"] = bool(s.is_peptide)
            out["is_peptide"] = bool(s.is_peptide)
        # `msa` is the deprecated alias for `use_msa`; an explicit `use_msa`
        # always wins. Either False also implies the peptide flag below.
        effective_use_msa = s.use_msa if s.use_msa is not None else s.msa
        if effective_use_msa is not None:
            out["use_msa"] = bool(effective_use_msa)
        # Any no-MSA spelling implies isPeptide on the wire, so a single flag is
        # enough for the caller and every downstream guard sees a signal it
        # recognizes regardless of which key it reads.
        if s.msa_disabled:
            out["isPeptide"] = True
            out["is_peptide"] = True
            out["use_msa"] = False
        return out
    return dict(s)


# Server-side bookkeeping keys that are not part of the public request contract.
# ``parameters`` echoes the server's resolved job config, so a key the server
# adds for its own accounting would otherwise surface in user-facing output and
# read like a knob a caller can set. Scrubbed here as defence in depth — the
# server is the authority and already omits these.
_NON_PUBLIC_PARAMETER_KEYS = frozenset({"internalPoolPeptidesPerTarget"})


def _public_parameters(params: Any) -> Any:
    """Drop server-internal bookkeeping keys from an echoed config object."""
    if not isinstance(params, dict):
        return params
    return {k: v for k, v in params.items() if k not in _NON_PUBLIC_PARAMETER_KEYS}


def _parse_cfg_reference_provenance(
    payload: dict[str, Any], parameters: Any
) -> CfgReferenceProvenance | None:
    """Read ``cfg_reference_provenance`` off a job payload, or None if absent.

    ⛔ None means "this server build did not report it" — NEVER "no
    conditioning happened". As of 2026-09-23 no production job record carries
    this field yet (Express's relay — bd-dre-lknrx — has not landed), so every
    run parsed today gets None here regardless of whether it was genuinely
    CFG-conditioned. See :class:`CfgReferenceProvenance` for why the two must
    never render the same way.

    ``caller_requested`` falls back to whether THIS request's echoed
    parameters named a panel (``cfg_reference_genes`` / ``cfgReferenceGenes``)
    when the provenance payload itself carries no explicit flag — the only
    other place that fact survives once the request has been sent.
    """
    node = payload.get("cfg_reference_provenance")
    if node is None:
        node = payload.get("cfgReferenceProvenance")
    named_panel = False
    if isinstance(parameters, dict):
        named_panel = bool(
            parameters.get("cfg_reference_genes") or parameters.get("cfgReferenceGenes")
        )
    return CfgReferenceProvenance.from_wire(node, caller_requested_hint=named_panel)


def _parse_generation(payload: dict[str, Any]) -> GenerationResult:
    """Coerce a server result payload into :class:`GenerationResult`."""
    _parameters = _public_parameters(payload.get("parameters") or payload.get("config"))
    out: dict[str, Any] = {
        "jobId": payload.get("jobId") or payload.get("id") or payload.get("session_id") or "",
        "sessionId": payload.get("sessionId") or payload.get("session_id"),
        "gene": payload.get("gene") or _first_target_gene(payload) or "",
        "peptides": _extract_peptides(payload),
        "totalGenerated": payload.get("totalGenerated") or payload.get("total"),
        "parameters": _parameters,
        # Extra field (GenerationResult / _LGModel is extra="allow") — a typed,
        # read-only CfgReferenceProvenance instance, or None when this server
        # build does not report it. See ligandai.contrastive.CfgReferenceProvenance.
        "cfg_reference_provenance": _parse_cfg_reference_provenance(payload, _parameters),
    }
    return GenerationResult.model_validate(out)


def _parse_fold(payload: dict[str, Any]) -> FoldResult:
    """Build a :class:`FoldResult` from a server status payload.

    the parser is now durable about which keys it
    inspects. The server may emit the PDB content at the top level, nested
    under ``result``, or under ``output_data`` (gpu_jobs row shape). We probe
    each one in priority order and surface :attr:`FoldResult.has_structure`
    set True only when actual content was found.
    """
    if not isinstance(payload, dict):
        payload = {}

    # Server emits the fold result in three possible places: top-level (raw
    # webhook), payload["result"] (folding_jobs endpoint), or
    # payload["output_data"] (gpu_jobs row). Probe in priority order.
    nested_candidates: list[dict[str, Any]] = []
    for key in ("result", "output_data", "outputData"):
        node = payload.get(key)
        if isinstance(node, dict):
            nested_candidates.append(node)

    def _first(*keys: str) -> Any:
        for src in (payload, *nested_candidates):
            if not isinstance(src, dict):
                continue
            for k in keys:
                v = src.get(k)
                if v is not None and v != "":
                    return v
        return None

    pdb_data = _first("pdbContent", "pdb_content", "pdbData", "pdb_data", "pdb")
    pdb_url = _first("pdbUrl", "pdb_url")
    cif_data = _first("cifContent", "cif_content", "cifData", "cif_data", "cif")
    iptm = _first("iptm", "ipTM")
    ipsae = _first("ipsae", "iPSAE")
    # ⛔ peptide_ipsae is the BINDER metric (peptide-receptor interface iPSAE).
    # Read it on its own — never coalesce into `ipsae`, which is the complex-level
    # score inflated by receptor-self confidence on multimeric targets. A missing
    # value means "not computed", not zero.
    peptide_ipsae = _first("peptideIpsae", "peptide_ipsae")
    # server emits both a scalar mean_plddt AND an
    # array plddt (per-residue). FoldResult.plddt is a scalar — prefer the
    # mean if available, fall back to mean(plddt-array), else None.
    plddt_raw = _first("mean_plddt", "meanPlddt", "plddt")
    if isinstance(plddt_raw, (int, float)):
        plddt = float(plddt_raw)
    elif isinstance(plddt_raw, list) and plddt_raw:
        try:
            plddt = sum(float(v) for v in plddt_raw) / len(plddt_raw)
        except (TypeError, ValueError):
            plddt = None
    else:
        plddt = None
    ptm = _first("ptm")
    ipae = _first("ipae")

    # ── PDE (predicted distance error), migration 170 ────────────────────────
    # ⛔ Each of these is probed on its OWN keys. interface_pde_min must NEVER
    # fall back to gpde or interface_gpde: those are complex-level statistics,
    # so substituting one would report a different quantity under a name that
    # promises the interface — the same class of error as coalescing
    # peptide_ipsae into ipsae. A missing value means "not computed".
    #
    # The tags are read in the same pass and validated onto the model together
    # with the numbers, because a PDE value without its engine/kind/scale
    # cannot be compared to any other PDE value.
    gpde = _first("gpde")
    interface_gpde = _first("interfaceGpde", "interface_gpde")
    interface_pde_min = _first("interfacePdeMin", "interface_pde_min")
    # A bare `pde` key is accepted LAST as a defensive alias for the
    # DB-generated cross-engine column, so the canonical names always win. A
    # non-numeric `pde` (e.g. a nested block) coerces to None below rather than
    # poisoning the field.
    pde_rankable = _first("pdeRankable", "pde_rankable", "pde")
    pde_engine = _first("pdeEngine", "pde_engine")
    pde_kind = _first("pdeKind", "pde_kind")
    pde_scale = _first("pdeScale", "pde_scale")
    pde_interpretation = _first("pdeInterpretation", "pde_interpretation")
    pde_units = _first("pdeUnits", "pde_units")

    chain_pair_iptm = _first("chainPairIptm", "chain_pair_iptm")
    per_chain = _first("perChain", "per_chain", "per_chain_metrics")
    pae_url = _first("paeUrl", "pae_url")
    confidence = _first("confidence", "confidence_metrics", "confidenceMetrics")
    metrics = _first("metrics") or {}
    scores = _first("scores", "deltaforge", "deltaforge_score") or {}
    # Acceptance-gate verdict [bd-LIGANDAI_ALPHA_V2-dl6pj]. Populated on the
    # peptides.search()/.list() path today (server/enterprise-api-routes.ts
    # emits camelCase `qualityTier`); probed defensively here too so this
    # parser stays correct if/when a fold-status payload starts carrying it.
    # Never re-derive the verdict client-side -- surface exactly what the
    # server decided, per QualityTier's docstring.
    quality_tier = _first("qualityTier", "quality_tier")

    def _to_float(v: Any) -> float | None:
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    iptm = _to_float(iptm)
    ipsae = _to_float(ipsae)
    peptide_ipsae = _to_float(peptide_ipsae)
    ptm = _to_float(ptm)
    ipae = _to_float(ipae) if not isinstance(ipae, dict) else None
    gpde = _to_float(gpde)
    interface_gpde = _to_float(interface_gpde)
    interface_pde_min = _to_float(interface_pde_min)
    pde_rankable = _to_float(pde_rankable)

    def _to_tag(v: Any) -> str | None:
        return v if isinstance(v, str) and v else None

    pde_engine = _to_tag(pde_engine)
    pde_kind = _to_tag(pde_kind)
    pde_scale = _to_tag(pde_scale)
    pde_interpretation = _to_tag(pde_interpretation)
    pde_units = _to_tag(pde_units)

    # Older payloads carry the raw components but not the DB-generated
    # cross-engine column. Recompute it from the tags rather than leaving the
    # caller to apply the native+angstrom rule by hand — that rule is exactly
    # what gets forgotten. Anything short of native+angstrom stays None, which
    # is a refusal to compare across engines, not a missing measurement.
    if pde_rankable is None and pde_kind == "native" and pde_scale == "angstrom":
        pde_rankable = interface_pde_min

    if not isinstance(metrics, dict):
        metrics = {}
    # Make headline metrics easy to subscript even when the server didn't pre-
    # build the metrics dict.
    metrics = dict(metrics)
    for k, v in (
        ("iptm", iptm), ("ipsae", ipsae), ("ptm", ptm),
        ("mean_plddt", plddt), ("ipae", ipae),
        ("peptide_ipsae", peptide_ipsae),
        # ⛔ `metrics` is a flat dict[str, float] — a value placed here arrives
        # WITHOUT its engine/kind/scale tags, so only a PDE number that is
        # self-qualifying may go in. `pde_rankable` is exactly that: it is
        # non-None if and only if the value is a native read on a verified
        # Angstrom scale, i.e. safe to compare against any other fold's
        # pde_rankable. The raw gpde / interface_gpde / interface_pde_min are
        # deliberately NOT back-filled here — untagged, they are numbers no
        # caller can interpret, and a `metrics["interface_pde_min"]` that
        # silently mixes Boltz-2's unverified scale with Protenix Angstroms is
        # precisely the cross-engine error this design exists to prevent. Read
        # them from `fold.pde`, where the tags cannot be detached.
        # LOWER IS BETTER for this key, unlike every other entry in the dict.
        ("pde_rankable", pde_rankable),
    ):
        if v is not None and k not in metrics:
            try:
                metrics[k] = float(v)
            except (TypeError, ValueError):
                pass

    has_struct_flag = _first("hasStructure", "has_structure")
    has_structure = bool(has_struct_flag) or bool(pdb_data) or bool(cif_data)

    return FoldResult.model_validate(
        {
            "jobId": payload.get("jobId") or payload.get("id") or _first("jobId", "id") or "",
            "pdbUrl": pdb_url,
            "pdbData": pdb_data,
            "cifData": cif_data,
            "hasStructure": has_structure,
            "iptm": iptm,
            "ipsae": ipsae,
            "peptideIpsae": peptide_ipsae,
            # Mirror the server's derivation so the field is meaningful even on
            # older payloads that predate peptideIpsaeStatus.
            "peptideIpsaeStatus": (
                _first("peptideIpsaeStatus", "peptide_ipsae_status")
                or ("ok" if peptide_ipsae is not None else "not_computed")
            ),
            "plddt": plddt,
            "ptm": ptm,
            "ipae": ipae,
            # PDE (migration 170). Values and tags are validated as ONE unit —
            # there is no path here that sets a PDE number without also setting
            # the engine/kind/scale that say whether it may be compared.
            "gpde": gpde,
            "interfaceGpde": interface_gpde,
            "interfacePdeMin": interface_pde_min,
            "pdeRankable": pde_rankable,
            "pdeEngine": pde_engine,
            "pdeKind": pde_kind,
            "pdeScale": pde_scale,
            # Direction is stated, never inferred from the field name. Defaults
            # to 'lower_is_better' whenever any PDE number is present, so a
            # payload predating the server-side tag still tells the caller which
            # way to sort — PDE runs opposite to ipSAE/iPTM/pLDDT.
            "pdeInterpretation": (
                pde_interpretation
                or (
                    "lower_is_better"
                    if any(
                        v is not None
                        for v in (gpde, interface_gpde, interface_pde_min, pde_rankable)
                    )
                    else None
                )
            ),
            "pdeUnits": (
                pde_units
                or ("angstrom" if pde_scale == "angstrom"
                    else ("unverified" if pde_scale == "unverified" else None))
            ),
            "chainPairIptm": chain_pair_iptm,
            "perChain": per_chain,
            "paeUrl": pae_url,
            "confidence": confidence if isinstance(confidence, dict) else None,
            "metrics": metrics or None,
            "scores": scores if isinstance(scores, dict) and scores else None,
            "qualityTier": quality_tier if isinstance(quality_tier, dict) else None,
        }
    )


# ─── Ensemble co-fold helpers (cofold) ──────────────────────────────────────
#
# The ensemble co-fold surface (POST /api/v1/cofold/start) folds the SAME chains
# on 1-6 engines (esmfold2, boltz2, protenix, openfold3, promera, opendde) in parallel. MSA is a
# single shared control for the whole ensemble (our-own MSA server) — there is
# NO per-engine MSA setting. Any MSA key in ``per_engine`` is rejected here,
# client-side, BEFORE the request reaches the wire (the server strips again).

#: Known ensemble engines (canonical lowercase). Mirrors the server.
COFOLD_ENGINES: tuple[str, ...] = ("esmfold2", "boltz2", "protenix", "openfold3", "promera", "opendde")

#: MSA keys never permitted in a per-engine override. Mirrors the server-side
#: ``FORBIDDEN_PER_ENGINE_MSA_KEYS`` — MSA is shared, never per-engine.
_COFOLD_FORBIDDEN_PER_ENGINE_MSA_KEYS = frozenset({
    "msa", "msa_csv", "use_msa_server", "msa_server_mode",
    "precomputed_msa_dir", "main_msa_file_paths", "pairedMsaPath",
    "unpairedMsaPath", "msa_mode", "msa_server",
})


def _cofold_normalize_engines(engines: list[str] | tuple[str, ...]) -> list[str]:
    """Validate + de-dupe (preserving order) the requested engine list."""
    if not engines:
        raise ValueError(
            "engines required (1-6 of: esmfold2, boltz2, protenix, openfold3, promera, opendde)"
        )
    seen: set[str] = set()
    out: list[str] = []
    for e in engines:
        el = str(e).strip().lower()
        if el not in COFOLD_ENGINES:
            raise ValueError(
                f"unknown engine '{e}'; known={list(COFOLD_ENGINES)}"
            )
        if el not in seen:
            seen.add(el)
            out.append(el)
    if len(out) > 6:
        # The server accepts SIX (fastapi_backend/routers/cofold.py MAX_ENGINES = 6)
        # and prices a 6-engine bundle at a 25% discount, but this guard said 5 —
        # so the SDK could not submit the largest ensemble the platform sells, and
        # the error string one line above already said "1-6". [bd-dre-3jqok]
        raise ValueError("at most 6 engines per ensemble")
    return out


def _cofold_chains(chains: list[Any]) -> list[dict[str, Any]]:
    """Coerce chain inputs into the server ChainSpec shape:
    ``{"id", "sequence", "kind": "receptor"|"peptide"}``.

    Accepts ``Sequence`` objects, bare AA strings, or dicts. A bare string with
    no ``kind`` defaults to ``receptor`` unless it is the LAST chain in a
    multi-chain payload, in which case it defaults to ``peptide`` (the canonical
    receptor-then-peptide ordering). Per-chain MSA is NOT accepted — MSA is the
    shared control.
    """
    if not isinstance(chains, list) or not chains:
        raise ValueError("chains must be a non-empty list")
    n = len(chains)
    normalized_chains = [_norm_seq(c) for c in chains]
    # When ANY chain is explicitly flagged, the caller has told us which chain is
    # the design and the positional "last chain is the peptide" fallback must NOT
    # also fire — otherwise a flagged binder listed first leaves the RECEPTOR
    # labelled a peptide and co-folded with no MSA. [bd-dre-yt8kx]
    any_explicit_flag = any(
        is_peptide_chain(d) or str(d.get("kind") or "").strip().lower() in ("receptor", "peptide")
        for d in normalized_chains
    )
    out: list[dict[str, Any]] = []
    for i, d in enumerate(normalized_chains):
        seq = str(d.get("sequence") or "").strip()
        if not seq:
            raise ValueError(f"chains[{i}] has no sequence")
        cid = d.get("chainId") or d.get("id") or chr(ord("A") + i)
        kind = str(d.get("kind") or "").strip().lower()
        if kind not in ("receptor", "peptide"):
            # An explicit peptide/binder flag beats position. A caller who
            # flagged the design but listed it first would otherwise have it
            # co-folded as the receptor. [bd-dre-yt8kx]
            if is_peptide_chain(d):
                kind = "peptide"
            elif any_explicit_flag:
                # Some chain was named explicitly; an unnamed one is the receptor.
                kind = "receptor"
            else:
                # Default: last chain in a multi-chain complex is the peptide.
                kind = "peptide" if (n >= 2 and i == n - 1) else "receptor"
        # Drop any MSA key a caller put on the chain dict — MSA is shared.
        out.append({"id": str(cid), "sequence": seq, "kind": kind})
    return out


def _cofold_cofactors(cofactors: list[Any] | None) -> list[dict[str, Any]]:
    """Coerce cofactors into the server ``CofoldStartRequest.cofactors`` shape:
    ``{"ccd" | "smiles", "copies"?, "id"?}``.

    Accepts a CCD string (``"ZN"``), a dict, or anything with ``as_dict()`` (the
    ensemble's ``CofactorSpec``). EXACTLY ONE of ``ccd`` / ``smiles`` — an empty
    ligand spec is what silently killed cofactor folds for months, so it is
    refused here rather than on the wire.
    """
    out: list[dict[str, Any]] = []
    for i, c in enumerate(cofactors or []):
        if isinstance(c, str):
            d: dict[str, Any] = {"ccd": c.strip().upper()}
        elif isinstance(c, dict):
            d = dict(c)
        elif hasattr(c, "as_dict"):
            d = dict(c.as_dict())
        else:
            raise TypeError(
                f"cofactors[{i}] must be a CCD string, dict, or CofactorSpec; "
                f"got {type(c).__name__}"
            )
        if "ccdCode" in d and "ccd" not in d:
            d["ccd"] = d.pop("ccdCode")
        ccd, smiles = d.get("ccd"), d.get("smiles")
        if bool(ccd) == bool(smiles):
            raise ValueError(
                f"cofactors[{i}] needs exactly one of ccd= or smiles= "
                f"(got ccd={ccd!r}, smiles={smiles!r})"
            )
        if ccd:
            d["ccd"] = str(ccd).strip().upper()
        out.append({k: v for k, v in d.items() if v is not None})
    return out


def _cofold_sanitize_per_engine(
    per_engine: dict[str, Any] | None,
) -> dict[str, Any]:
    """Reject any MSA key inside a per-engine override (raises), then return the
    cleaned map. MSA is shared + server-enforced; engines never accept an MSA
    override from callers."""
    if not per_engine:
        return {}
    if not isinstance(per_engine, dict):
        raise TypeError("per_engine must be a dict of {engine: {settings}}")
    clean: dict[str, Any] = {}
    for engine, settings in per_engine.items():
        el = str(engine).strip().lower()
        if el not in COFOLD_ENGINES:
            raise ValueError(
                f"per_engine has unknown engine '{engine}'; "
                f"known={list(COFOLD_ENGINES)}"
            )
        if not isinstance(settings, dict):
            clean[el] = settings
            continue
        bad = [k for k in settings if k in _COFOLD_FORBIDDEN_PER_ENGINE_MSA_KEYS]
        if bad:
            raise ValueError(
                f"per_engine['{engine}'] may not set MSA keys {bad} — MSA is a "
                f"single shared control (shared_msa), not per-engine."
            )
        clean[el] = dict(settings)
    return clean


def _parse_cofold(payload: dict[str, Any]) -> dict[str, Any]:
    """Pass-through parser for an ensemble co-fold status payload.

    The co-fold result is a multi-engine record (``result.engines`` keyed by
    engine, plus ``result.summary`` / ``result.billing``); there is no single
    :class:`FoldResult` to coerce to, so we return the server payload's terminal
    ``result`` (falling back to the whole payload)."""
    if not isinstance(payload, dict):
        return {}
    result = payload.get("result")
    return result if isinstance(result, dict) else payload


# ─── Batch fold helpers (v0.5.5) ────────────────────────────────────────────

# The batch-fold endpoint takes either bare AA strings or FASTA blocks. The
# server parses FASTA already, so this list is forwarded verbatim — but we
# strip empty/whitespace-only entries client-side to surface argument errors
# closer to the caller.
def _normalize_batch_peptide_inputs(peptides: list[str]) -> list[str]:
    if not isinstance(peptides, list) or not peptides:
        raise ValueError("peptides must be a non-empty list of strings")
    cleaned: list[str] = []
    for i, p in enumerate(peptides):
        if not isinstance(p, str):
            raise TypeError(f"peptides[{i}] must be a string, got {type(p).__name__}")
        s = p.strip()
        if s:
            cleaned.append(s)
    if not cleaned:
        raise ValueError("peptides list contained no non-empty strings")
    return cleaned


def _resolve_receptor_pdb_arg(receptor_pdb: str | None) -> str | None:
    """Accept either raw PDB content or a filesystem path. Path -> read once."""
    if receptor_pdb is None:
        return None
    if not isinstance(receptor_pdb, str) or not receptor_pdb.strip():
        raise ValueError("receptor_pdb must be a non-empty string")
    # If the value looks like a real PDB record we forward as-is. Otherwise, if
    # it looks like a path that exists on disk, read it.
    has_pdb_header = any(
        receptor_pdb.lstrip().startswith(tok)
        for tok in ("HEADER", "TITLE", "COMPND", "REMARK", "ATOM", "HETATM", "MODEL", "CRYST1")
    )
    if has_pdb_header:
        return receptor_pdb
    # Heuristic: short single-line strings without ATOM records but with a
    # plausible file suffix are treated as paths.
    if len(receptor_pdb) < 4096 and ("/" in receptor_pdb or "\\" in receptor_pdb or receptor_pdb.endswith(".pdb")):
        try:
            path = Path(receptor_pdb).expanduser()
            if path.exists() and path.is_file():
                return path.read_text(encoding="utf-8")
        except OSError:
            # Fall through — caller probably meant a literal string
            pass
    return receptor_pdb


def _build_batch_fold_body(
    *,
    peptides: list[str],
    target_gene: str | None,
    receptor_pdb: str | None,
    receptor_sequence: str | None,
    receptor_name: str | None,
    diffusion_samples: int,
    sampling_steps: int,
    recycling_steps: int | None,
    step_scale: float | None,
    msa_enabled: bool | None,
    glycosylation: bool | None,
    template_mode: bool,
    n_parallel_gpus: int | None,
    session_id: str | None,
    contribute_to_receptordb: bool | None,
    num_trajectories: int | None = None,
    msa_depth: int | None = None,
    use_potentials: bool | None = None,
    msa_source_gene: str | None = None,
    gene_range: tuple[int, int] | None = None,
    # [bd-LIGANDAI_ALPHA_V2-pybsy] Boltz-2 --seed; omitted from the body when None
    # so an unseeded submission is byte-identical to what it was before.
    seed: int | None = None,
) -> dict[str, Any]:
    """Construct the POST /api/v1/folding/predict-batch JSON body.

    ``msa_source_gene`` / ``gene_range`` (bd-dre-h0cuo.1): give an arbitrary
    receptor a proper MSA derived from a homolog-rich PARENT gene instead of a
    fresh, often-degenerate fragment search.

    * ``receptor_sequence=<arbitrary seq>`` + ``msa_source_gene="COL17A1"``
      → server derives the MSA from COL17A1's cached full-length MSA (column-
      subset when the sequence is an exact substring; align otherwise).
    * ``target_gene="COL17A1"`` + ``gene_range=(489, 566)`` (1-based inclusive)
      → server slices the gene's sequence AND MSA columns to that range and
      folds the slice. This is how you fold COL17A1 NC16A (residues 489-566).
    """
    receptor_specified = [v for v in (target_gene, receptor_pdb, receptor_sequence) if v]
    if len(receptor_specified) != 1:
        raise ValueError(
            "Pass exactly one of target_gene=, receptor_pdb=, or receptor_sequence="
        )
    if gene_range is not None and target_gene is None:
        raise ValueError("gene_range= requires target_gene= (it slices that gene's sequence + MSA)")
    if msa_source_gene is not None and receptor_sequence is None:
        raise ValueError(
            "msa_source_gene= only applies to receptor_sequence= "
            "(use target_gene=+gene_range= to fold a gene's domain)"
        )
    if gene_range is not None:
        s, e = int(gene_range[0]), int(gene_range[1])
        if s < 1 or e < s:
            raise ValueError(f"gene_range must be 1-based inclusive with start<=end, got {gene_range!r}")

    body: dict[str, Any] = {
        "peptides": _normalize_batch_peptide_inputs(peptides),
        "diffusion_samples": int(diffusion_samples),
        "sampling_steps": int(sampling_steps),
        "template_mode": bool(template_mode),
    }
    if target_gene is not None:
        body["target_gene"] = target_gene
    if receptor_pdb is not None:
        body["receptor_pdb"] = _resolve_receptor_pdb_arg(receptor_pdb)
    if receptor_sequence is not None:
        body["receptor_sequence"] = receptor_sequence
    if msa_source_gene is not None:
        body["msa_source_gene"] = str(msa_source_gene)
    if gene_range is not None:
        body["gene_range"] = [int(gene_range[0]), int(gene_range[1])]
    if receptor_name is not None:
        body["receptor_name"] = receptor_name
    if recycling_steps is not None:
        body["recycling_steps"] = int(recycling_steps)
    # [bd-LIGANDAI_ALPHA_V2-pybsy] Boltz-2 --seed. Server accepts
    # z.number().int().min(0).max(4294967295).optional(); the worker appends
    # `--seed N` to the boltz argv (and to its retry, so a retried fold is not
    # silently unseeded while the receipt still names a seed).
    if seed is not None:
        body["seed"] = int(seed)
    if step_scale is not None:
        body["step_scale"] = float(step_scale)
    if msa_enabled is not None:
        body["msa_enabled"] = bool(msa_enabled)
    if glycosylation is not None:
        body["glycosylation"] = bool(glycosylation)
    if n_parallel_gpus is not None:
        body["n_parallel_gpus"] = int(n_parallel_gpus)
    if session_id is not None:
        body["session_id"] = session_id
    if contribute_to_receptordb is not None:
        body["contribute_to_receptordb"] = bool(contribute_to_receptordb)
    if num_trajectories is not None:
        body["num_trajectories"] = int(num_trajectories)
    if msa_depth is not None:
        body["msa_depth"] = int(msa_depth)
    # Physics-based steering potentials. Platform default ON (bd-..-j2kc5). Explicit
    # True OR False forwarded (overrides server default); only None omits the key.
    if use_potentials is not None:
        body["use_potentials"] = bool(use_potentials)
        body["usePotentials"] = bool(use_potentials)
    return body


# ─── Grouped batch fold (v0.6.4) ─────────────────────────────────────────────
#
# bd-LIGANDAI_ALPHA_V2-wymqb. The legacy ``fold_batch(peptides=[...],
# target_gene=...)`` form folds N bare peptides against ONE receptor, all de
# novo (no peptide MSA). The grouped form lets each fold job be a
# ``target{partners}`` complex with MSA control settable independently at BOTH
# levels:
#
#   * per target/receptor (a protein; default MSA ON), AND
#   * per co-folding partner — a PEPTIDE (default NO MSA) or a PROTEIN (may
#     request its OWN MSA).
#
# Multiple groups per batch; each group becomes one fold job whose ``entities``
# array is [target=chain A, partner1=chain B, partner2=chain C, ...]. The
# per-entity ``use_msa`` / ``isPeptide`` / ``role`` flags are honored end-to-end
# by the Express route, the BullMQ MSA prefetch, and the Boltz-2 worker
# MSA_GUARD (peptide / use_msa=False → ``msa: empty``; receptor / MSA-flagged
# protein partner → MSA passthrough or ``--use_msa_server``).

# Partner entity ``type`` values accepted by the grouped API. A "peptide"
# defaults to NO MSA; a "protein" partner defaults to MSA ON (like a target).
# [bd-LIGANDAI_ALPHA_V2-pybsy] 'ligand' | 'dna' | 'rna' were absent here, so a
# cofactor could not be expressed on the batch surface at all — while
# modal_workers/boltz2_batch.py has materialised all three since it was written.
# For a ligand, `sequence` carries the CCD code (ZN, MG, CA, MN, FE2, HEM, ...).
_PARTNER_TYPES: frozenset[str] = frozenset({"peptide", "protein", "ligand", "dna", "rna"})
# Entity types that are not polypeptides: never MSA'd, never flagged isPeptide,
# and emitted with their own Boltz-2 entity type rather than as "protein".
_NON_PROTEIN_PARTNER_TYPES: frozenset[str] = frozenset({"ligand", "dna", "rna"})
_CCD_RE = __import__("re").compile(r"^[A-Za-z0-9]{1,5}$")


def _normalize_group_target(target: Any, *, group_index: int) -> dict[str, Any]:
    """Normalize one group's ``target`` spec into a wire entity dict.

    Accepts a dict with exactly one of ``gene`` / ``sequence`` / ``pdb`` and an
    optional ``use_msa`` (default True — a target is a protein/receptor) and
    ``name``. The returned dict is the chain-A entity descriptor the server
    resolves into the receptor sequence.
    """
    if not isinstance(target, dict):
        raise TypeError(
            f"groups[{group_index}]['target'] must be a dict with one of "
            f"gene=/sequence=/pdb=, got {type(target).__name__}"
        )
    modes = [k for k in ("gene", "sequence", "pdb") if target.get(k)]
    if len(modes) != 1:
        raise ValueError(
            f"groups[{group_index}]['target'] must specify exactly one of "
            f"'gene', 'sequence', or 'pdb' (got: {modes or 'none'})"
        )
    use_msa = target.get("use_msa")
    out: dict[str, Any] = {
        # A target is always a protein receptor — chain A.
        "type": "protein",
        # MSA defaults ON for a target/receptor unless explicitly disabled.
        "use_msa": True if use_msa is None else bool(use_msa),
    }
    if target.get("gene"):
        out["gene"] = str(target["gene"])
    elif target.get("sequence"):
        out["sequence"] = str(target["sequence"])
    elif target.get("pdb"):
        # Resolve a path → content once, mirroring _resolve_receptor_pdb_arg.
        out["pdb"] = _resolve_receptor_pdb_arg(str(target["pdb"]))
    if target.get("name"):
        out["name"] = str(target["name"])
    return out


def _normalize_group_partner(partner: Any, *, group_index: int, partner_index: int) -> dict[str, Any]:
    """Normalize one partner into a wire entity dict with explicit MSA flags.

    A partner is a co-folding chain. ``type`` ∈ {"peptide", "protein"}; a bare
    string is treated as a peptide. Default ``use_msa``: peptide → False,
    protein → True. An explicit ``use_msa`` always wins. Peptide chains (or any
    chain with ``use_msa=False``) carry ``isPeptide``/``role='peptide'`` so the
    MSA prefetch guard and worker MSA_GUARD skip the MSA search.
    """
    if isinstance(partner, str):
        partner = {"sequence": partner, "type": "peptide"}
    if not isinstance(partner, dict):
        raise TypeError(
            f"groups[{group_index}]['partners'][{partner_index}] must be a dict "
            f"or sequence string, got {type(partner).__name__}"
        )
    seq = partner.get("sequence") or partner.get("seq")
    if not seq or not isinstance(seq, str) or not seq.strip():
        raise ValueError(
            f"groups[{group_index}]['partners'][{partner_index}] needs a non-empty "
            f"'sequence'"
        )
    ptype = str(partner.get("type") or "peptide").strip().lower()
    if ptype not in _PARTNER_TYPES:
        raise ValueError(
            f"groups[{group_index}]['partners'][{partner_index}]['type'] must be one "
            f"of {sorted(_PARTNER_TYPES)}, got {ptype!r}"
        )
    # NON-PROTEIN entities short-circuit: a ligand/dna/rna is emitted with its own
    # entity type. Forcing type="protein" here (as this function did for every
    # partner) folded a zinc ion as a one-residue PROTEIN chain — a plausible
    # structure for the wrong complex, which is worse than a rejection.
    if ptype in _NON_PROTEIN_PARTNER_TYPES:
        seq_clean = seq.strip().upper() if ptype == "ligand" else seq.strip()
        if ptype == "ligand" and not _CCD_RE.match(seq_clean):
            raise ValueError(
                f"groups[{group_index}]['partners'][{partner_index}] is type 'ligand' "
                f"so 'sequence' must be a CCD code (1-5 alphanumeric chars, e.g. 'ZN', "
                f"'MG', 'HEM'), got {seq_clean[:20]!r}"
            )
        copies = partner.get("copies", 1)
        try:
            copies = int(copies)
        except (TypeError, ValueError):
            copies = -1
        if copies < 1 or copies > 12:
            raise ValueError(
                f"groups[{group_index}]['partners'][{partner_index}]['copies'] must be "
                f"an integer in 1..12, got {partner.get('copies')!r}"
            )
        ent: dict[str, Any] = {
            "type": ptype,
            "sequence": seq_clean,
            "use_msa": False,
            "copies": copies,
        }
        if partner.get("name"):
            ent["name"] = str(partner["name"])
        return ent

    is_peptide = ptype == "peptide"
    explicit_msa = partner.get("use_msa")
    # Default: peptide → no MSA, protein → MSA. Explicit value overrides.
    use_msa = (not is_peptide) if explicit_msa is None else bool(explicit_msa)
    out: dict[str, Any] = {
        "type": "protein",  # Boltz-2 entity type; a peptide is a short protein chain
        "sequence": seq.strip(),
        "use_msa": use_msa,
    }
    # Flag peptide / no-MSA chains so the prefetch guard + worker MSA_GUARD skip
    # them. A protein partner that the caller explicitly opted OUT of MSA also
    # gets the no-MSA flag (use_msa=False is itself a guard signal), but is NOT
    # labelled a peptide.
    if is_peptide:
        out["isPeptide"] = True
        out["is_peptide"] = True
        out["role"] = "peptide"
    if partner.get("name"):
        out["name"] = str(partner["name"])
    return out


def _normalize_fold_groups(groups: Any) -> list[dict[str, Any]]:
    """Normalize the ``groups=`` argument into the wire ``groups`` array.

    Each group → ``{"target": {...}, "partners": [{...}, ...]}`` with chain IDs
    assigned A (target), then B, C, ... for partners.
    """
    if not isinstance(groups, list) or not groups:
        raise ValueError("groups must be a non-empty list of {target, partners} dicts")
    normalized: list[dict[str, Any]] = []
    for gi, group in enumerate(groups):
        if not isinstance(group, dict):
            raise TypeError(
                f"groups[{gi}] must be a dict with 'target' and 'partners', "
                f"got {type(group).__name__}"
            )
        target = _normalize_group_target(group.get("target"), group_index=gi)
        raw_partners = group.get("partners")
        if not isinstance(raw_partners, list) or not raw_partners:
            raise ValueError(
                f"groups[{gi}]['partners'] must be a non-empty list (at least one "
                f"co-folding partner)"
            )
        target = {**target, "chainId": "A"}
        partners: list[dict[str, Any]] = []
        _next_chain_idx = 1  # 0 == "A" == the target
        for pi, partner in enumerate(raw_partners):
            ent = _normalize_group_partner(partner, group_index=gi, partner_index=pi)
            # Chain B, C, D, ... for partners.
            # [bd-LIGANDAI_ALPHA_V2-pybsy] Advance by COPIES, not by partner index.
            # A cofactor with copies=2 occupies two chains; indexing by `pi` gave
            # the next partner the same chain id as the second copy.
            ent = {**ent, "chainId": chr(ord("A") + _next_chain_idx)}
            _next_chain_idx += max(1, int(ent.get("copies", 1) or 1))
            partners.append(ent)
        normalized.append({"target": target, "partners": partners})
    return normalized


def _build_grouped_batch_fold_body(
    *,
    groups: list[dict[str, Any]],
    diffusion_samples: int,
    sampling_steps: int,
    recycling_steps: int | None,
    step_scale: float | None,
    glycosylation: bool | None,
    template_mode: bool,
    n_parallel_gpus: int | None,
    session_id: str | None,
    contribute_to_receptordb: bool | None,
    num_trajectories: int | None = None,
    msa_depth: int | None = None,
    use_potentials: bool | None = None,
    # [bd-LIGANDAI_ALPHA_V2-pybsy] Boltz-2 --seed (grouped form).
    seed: int | None = None,
) -> dict[str, Any]:
    """Construct the grouped POST /api/v1/folding/predict-batch JSON body.

    The body carries a top-level ``groups`` array (the normalized per-entity
    target/partners spec). The shared Boltz-2 knobs apply to every group.
    """
    body: dict[str, Any] = {
        "groups": _normalize_fold_groups(groups),
        "diffusion_samples": int(diffusion_samples),
        "sampling_steps": int(sampling_steps),
        "template_mode": bool(template_mode),
    }
    if recycling_steps is not None:
        body["recycling_steps"] = int(recycling_steps)
    # [bd-LIGANDAI_ALPHA_V2-pybsy] Boltz-2 --seed. Server accepts
    # z.number().int().min(0).max(4294967295).optional(); the worker appends
    # `--seed N` to the boltz argv (and to its retry, so a retried fold is not
    # silently unseeded while the receipt still names a seed).
    if seed is not None:
        body["seed"] = int(seed)
    if step_scale is not None:
        body["step_scale"] = float(step_scale)
    if glycosylation is not None:
        body["glycosylation"] = bool(glycosylation)
    if n_parallel_gpus is not None:
        body["n_parallel_gpus"] = int(n_parallel_gpus)
    if session_id is not None:
        body["session_id"] = session_id
    if contribute_to_receptordb is not None:
        body["contribute_to_receptordb"] = bool(contribute_to_receptordb)
    if num_trajectories is not None:
        body["num_trajectories"] = int(num_trajectories)
    if msa_depth is not None:
        body["msa_depth"] = int(msa_depth)
    if use_potentials is not None:
        body["use_potentials"] = bool(use_potentials)
        body["usePotentials"] = bool(use_potentials)
    return body


class BatchFoldJob:
    """A batch of N peptide-receptor fold jobs submitted by ``Peptides.fold_batch``.

    Internally wraps N :class:`~ligandai.jobs.Job` instances (one per peptide)
    sharing a single ``batch_id``. ``wait()`` blocks until every sub-job is
    terminal and returns the list of parsed :class:`FoldResult` objects in the
    same order as the input peptides.

    The credit charge is taken upfront on submission — sub-job failures do
    NOT automatically refund. Use ``.refunds_pending`` to see how many failed.
    """

    def __init__(
        self,
        transport: Any,
        *,
        batch_id: str,
        jobs: list[dict[str, Any]],
        total_cost_credits: int,
        peptide_count: int,
        trajectories_per_peptide: int,
        receptor: dict[str, Any] | None = None,
        sampling_steps: int | None = None,
        from_cache: bool = False,
    ) -> None:
        from ligandai.jobs import Job  # local to avoid circular import

        self._transport = transport
        # [bd-LIGANDAI_ALPHA_V2-jb73b] Was this handle SUBMITTED, or handed back
        # by the 24h dedupe? Measured 2026-09-23: six separate submissions of
        # one payload returned peptide_ipsae identical to 8 decimal places,
        # because each after the first was the cache. A caller building
        # "independent draws" out of repeat calls gets N copies of one
        # measurement and no error — the previous signal for this lived inside
        # `receptor={'source': 'cached'}`, which nothing computing a spread
        # would think to read.
        self._from_cache = bool(from_cache) or (receptor or {}).get("source") == "cached"
        self._batch_id = batch_id
        self._jobs_meta = jobs
        self._total_cost_credits = int(total_cost_credits)
        self._peptide_count = int(peptide_count)
        self._trajectories_per_peptide = int(trajectories_per_peptide)
        self._receptor = receptor or {}
        self._sampling_steps = sampling_steps
        self._sub_jobs: list[Job[FoldResult]] = []
        for entry in jobs:
            jid = entry.get("job_id") or entry.get("jobId") or ""
            if not jid:
                # Submission failed for this slot — placeholder kept so
                # peptide_index alignment is preserved in .results.
                self._sub_jobs.append(None)  # type: ignore[arg-type]
                continue
            self._sub_jobs.append(
                Job(
                    transport,
                    jid,
                    job_type="folding",
                    parser=_parse_fold,
                    status_path="/api/folding/jobs/{job_id}",
                    cancel_path="/api/folding/jobs/{job_id}",
                    sse_path="/api/folding/jobs/{job_id}/logs/stream",
                    initial={"id": jid, "type": "folding", "status": "queued"},
                )
            )
        self._results: list[FoldResult | None] | None = None

    # ─── Public properties ──────────────────────────────────────────────────

    @property
    def batch_id(self) -> str:
        return self._batch_id

    @property
    def from_cache(self) -> bool:
        """True when the 24-hour dedupe returned this batch instead of running it.

        ⛔ Check this before treating repeat submissions as independent draws.
        A cached handle is the FIRST submission's measurement handed back, so a
        spread, interval or cross-draw band computed over several of them
        describes one fold, not several. Measured 2026-09-23: six separate
        submissions of one payload returned ``peptide_ipsae`` identical to
        eight decimal places.

        ``fold_batch(..., force_resubmit=True)`` bypasses the dedupe, and
        ``campaigns.replicate`` / ``fold_panel(force_fresh=True)`` already do.
        For genuinely independent draws also vary Boltz-2's ``seed=`` — one
        submission per seed, each seed's result counting as ONE observation.
        """
        return self._from_cache

    @property
    def jobs(self) -> list[dict[str, Any]]:
        """Raw per-peptide submission metadata: job_id, peptide_index, sequence."""
        return list(self._jobs_meta)

    @property
    def total_cost_credits(self) -> int:
        return self._total_cost_credits

    @property
    def peptide_count(self) -> int:
        return self._peptide_count

    @property
    def trajectories_per_peptide(self) -> int:
        return self._trajectories_per_peptide

    @property
    def receptor(self) -> dict[str, Any]:
        """Server-resolved receptor metadata: mode, gene, uniprot_id, length, source."""
        return dict(self._receptor)

    @property
    def sub_jobs(self) -> list[Any]:
        """List of :class:`Job` instances aligned with ``peptide_index``.

        Failed-to-submit slots are ``None`` (kept so indices line up with input).
        """
        return list(self._sub_jobs)

    @property
    def results(self) -> list[FoldResult | None]:
        """Parsed FoldResults aligned with ``peptide_index`` (None for failures).

        Triggers a per-job refresh + parse if not already cached. Call ``.wait()``
        first to ensure all sub-jobs are terminal.
        """
        if self._results is None:
            out: list[FoldResult | None] = []
            for sub in self._sub_jobs:
                if sub is None:
                    out.append(None)
                    continue
                try:
                    out.append(sub.results)
                except LigandAILegalAcceptanceRequired:
                    # ⛔ A legal 403 must not become a silent `None` result.
                    raise
                except Exception:
                    out.append(None)
            self._results = out
        return list(self._results)

    @property
    def folds(self) -> list[FoldResult | None]:
        """Alias for :attr:`results` — matches the docstring example surface."""
        return self.results

    @property
    def refunds_pending(self) -> int:
        """Sub-jobs that failed-to-submit or ended non-successfully (refund tally)."""
        n = 0
        for sub in self._sub_jobs:
            if sub is None:
                n += 1
                continue
            try:
                if sub.is_terminal and not sub.succeeded:
                    n += 1
            except Exception:
                n += 1
        return n

    # ─── Polling & waiting ──────────────────────────────────────────────────

    def wait(
        self,
        timeout: float = 7200.0,
        poll_interval: float = 5.0,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> list[FoldResult | None]:
        """Block until every sub-job is terminal. Returns ordered results.

        Parameters
        ----------
        timeout
            Total wall-clock budget (seconds) for the whole batch. Raises
            :class:`LigandAITimeoutError` if exceeded.
        poll_interval
            Sleep between polls (seconds). Each tick refreshes every still-
            running sub-job before sleeping.
        on_progress
            Optional callback receiving a dict ``{"done": int, "total": int,
            "failed": int, "batch_id": str}`` on every poll tick.
        """
        import time
        from ligandai.errors import LigandAINotFoundError, LigandAITimeoutError

        deadline = time.monotonic() + timeout
        total = len(self._sub_jobs)
        # ⛔ MEASURED 2026-09-22: 4 of 18 COMPLETED sub-jobs answered their own
        # status path (/api/folding/jobs/{id}) with 404 while the batch view and
        # /api/jobs/{id}/status both reported "completed". A 404 was swallowed
        # below as "transient" and retried forever, so wait() could never finish
        # those batches — it burned the entire timeout and then reported a
        # LigandAITimeoutError for folds that had been done for half an hour.
        # A not-found that REPEATS is not transient. After a short grace (enough
        # to cover the propagation race right after submission) the batch view is
        # consulted, because it demonstrably answers for exactly these jobs.
        _NOT_FOUND_GRACE = 3
        not_found_streak: dict[int, int] = {}
        batch_view_status: dict[str, str] = {}
        batch_view_read = False

        def _batch_view() -> dict[str, str]:
            nonlocal batch_view_read
            if not batch_view_read:
                batch_view_read = True
                try:
                    payload = self._transport.request(
                        "GET", f"/api/jobs/batch/{self._batch_id}"
                    ) or {}
                    for entry in payload.get("jobs") or []:
                        jid = str(entry.get("id") or "").strip()
                        st = str(entry.get("status") or "").strip().lower()
                        if jid and st:
                            batch_view_status[jid] = st
                except Exception:
                    pass
            return batch_view_status

        while True:
            done = 0
            failed = 0
            batch_view_read = False
            for idx, sub in enumerate(self._sub_jobs):
                if sub is None:
                    failed += 1
                    done += 1
                    continue
                if sub.is_terminal:
                    done += 1
                    if not sub.succeeded:
                        failed += 1
                else:
                    try:
                        sub.refresh()
                        not_found_streak.pop(idx, None)
                        if sub.is_terminal:
                            done += 1
                            if not sub.succeeded:
                                failed += 1
                    except LigandAILegalAcceptanceRequired:
                        # ⛔ NOT transient. Retrying a legal 403 every tick
                        # burns the whole wait() budget and then reports a
                        # LigandAITimeoutError, sending the user to debug a
                        # job that was never the problem. Surface the
                        # acceptance URL immediately.
                        raise
                    except LigandAINotFoundError:
                        streak = not_found_streak.get(idx, 0) + 1
                        not_found_streak[idx] = streak
                        if streak < _NOT_FOUND_GRACE:
                            continue  # still plausibly a propagation race
                        st = _batch_view().get(sub.id)
                        if st in ("completed", "succeeded", "success"):
                            done += 1
                        elif st in ("failed", "error", "cancelled", "canceled"):
                            done += 1
                            failed += 1
                        # Unknown to the batch view too — keep polling; the
                        # deadline below is what ends it.
                    except Exception:
                        # Transient — keep retrying on next tick
                        pass
            if on_progress is not None:
                try:
                    on_progress({
                        "batch_id": self._batch_id,
                        "done": done,
                        "total": total,
                        "failed": failed,
                    })
                except Exception:
                    pass
            if done >= total:
                break
            if time.monotonic() > deadline:
                raise LigandAITimeoutError(
                    f"Batch {self._batch_id} did not complete within {timeout}s "
                    f"({done}/{total} sub-jobs terminal)"
                )
            time.sleep(poll_interval)
        # Force per-job result parse once
        self._results = None
        return self.results

    def stream(
        self,
        timeout: float = 7200.0,
        poll_interval: float = 5.0,
    ) -> "Iterator[BatchFoldEvent]":
        """Yield one :class:`~ligandai.types.BatchFoldEvent` per sub-job as it
        becomes terminal AND its structural payload has landed.

        canonical streaming surface for batch folds.
        Replaces the old pattern of ``[job.wait() for job in batch.sub_jobs]``
        which serialized waits and missed the structural-payload landing race.

        Each event carries the peptide index, peptide sequence, server job_id,
        full PDB content, and confidence metrics so callers can pipeline
        scoring/save-to-disk per-fold instead of waiting for the whole batch.

        Parameters
        ----------
        timeout
            Total wall-clock budget (seconds) for the whole batch. Raises
            :class:`~ligandai.errors.LigandAITimeoutError` when the deadline
            elapses with sub-jobs still pending.
        poll_interval
            Sleep between batch ticks (seconds).

        Yields
        ------
        BatchFoldEvent
            One per fully-resolved sub-job (or one per failed sub-job).

        Example
        -------
        .. code-block:: python

            batch = client.fold_batch(peptides=[P1, P2, P3], target=RECEPTOR)
            for event in batch.stream():
                if event.status == "succeeded":
                    Path(f"./{event.record_id}.pdb").write_text(event.pdb_content)
                    print(f"{event.peptide_sequence}: iptm={event.iptm:.2f}")
        """
        import time as _time
        from datetime import datetime as _datetime, timezone as _timezone
        from ligandai.errors import LigandAITimeoutError as _LigandAITimeoutError
        from ligandai.jobs import _fold_has_durable_payload as _has_durable
        from ligandai.types import BatchFoldEvent as _BatchFoldEvent

        deadline = _time.monotonic() + timeout
        emitted: set[int] = set()
        total = len(self._sub_jobs)

        while len(emitted) < total:
            for idx, sub in enumerate(self._sub_jobs):
                if idx in emitted:
                    continue
                meta = self._jobs_meta[idx] if idx < len(self._jobs_meta) else {}
                record_id = (
                    meta.get("record_id")
                    or meta.get("recordId")
                    or (sub.id if sub is not None else None)
                )
                peptide_sequence = (
                    meta.get("peptide")
                    or meta.get("peptide_sequence")
                    or meta.get("sequence")
                )
                if sub is None:
                    emitted.add(idx)
                    yield _BatchFoldEvent.model_validate({
                        "recordId": record_id,
                        "jobId": meta.get("job_id") or meta.get("jobId") or "",
                        "peptideIndex": idx,
                        "peptideSequence": peptide_sequence,
                        "status": "failed",
                        "phase": "submit_failed",
                        "timestamp": _datetime.now(_timezone.utc),
                    })
                    continue
                try:
                    sub.refresh()
                except LigandAILegalAcceptanceRequired:
                    # ⛔ Not transient — `continue` would spin the stream
                    # forever without ever telling the user why.
                    raise
                except Exception:
                    continue
                if not sub.is_terminal:
                    continue
                durable_ok, _missing, _call_id = _has_durable(
                    sub.info, sub.info.result if sub.info else None,
                )
                if sub.succeeded and not durable_ok:
                    # Skip this tick — payload hasn't landed yet.
                    continue
                emitted.add(idx)
                if sub.succeeded:
                    try:
                        fold = sub.results
                    except LigandAILegalAcceptanceRequired:
                        raise  # never emit an empty event for a legal 403
                    except Exception:
                        fold = None
                    yield _BatchFoldEvent.model_validate({
                        "recordId": record_id,
                        "jobId": sub.id,
                        "peptideIndex": idx,
                        "peptideSequence": peptide_sequence,
                        "status": "succeeded",
                        "pdbContent": getattr(fold, "pdb_data", None),
                        "cifData": getattr(fold, "cif_data", None),
                        "iptm": getattr(fold, "iptm", None),
                        "ipsae": getattr(fold, "ipsae", None),
                        "ipae": getattr(fold, "ipae", None),
                        "ptm": getattr(fold, "ptm", None),
                        "meanPlddt": getattr(fold, "plddt", None),
                        "paeUrl": getattr(fold, "pae_url", None),
                        "confidence": getattr(fold, "confidence", None),
                        "perChain": getattr(fold, "per_chain", None),
                        "phase": "complete",
                        "timestamp": _datetime.now(_timezone.utc),
                    })
                else:
                    yield _BatchFoldEvent.model_validate({
                        "recordId": record_id,
                        "jobId": sub.id,
                        "peptideIndex": idx,
                        "peptideSequence": peptide_sequence,
                        "status": sub.status or "failed",
                        "phase": "failed",
                        "timestamp": _datetime.now(_timezone.utc),
                    })
            if len(emitted) >= total:
                break
            if _time.monotonic() > deadline:
                pending = [i for i in range(total) if i not in emitted]
                raise _LigandAITimeoutError(
                    f"Batch {self._batch_id} did not complete streaming within "
                    f"{timeout}s ({len(emitted)}/{total} folds emitted, "
                    f"pending indices: {pending[:10]}{'...' if len(pending) > 10 else ''})"
                )
            _time.sleep(poll_interval)

    def cancel(self) -> int:
        """Cancel every still-running sub-job. Returns the number canceled."""
        n = 0
        for sub in self._sub_jobs:
            if sub is None:
                continue
            try:
                if not sub.is_terminal and sub.cancel():
                    n += 1
            except LigandAILegalAcceptanceRequired:
                raise  # user is trying to stop paid GPU work; tell them why
            except Exception:
                pass
        return n

    def __len__(self) -> int:
        return len(self._sub_jobs)

    def __repr__(self) -> str:
        return (
            f"BatchFoldJob(batch_id={self._batch_id!r}, "
            f"peptide_count={self._peptide_count}, "
            f"trajectories_per_peptide={self._trajectories_per_peptide}, "
            f"total_cost_credits={self._total_cost_credits})"
        )


class AsyncBatchFoldJob:
    """Async sibling of :class:`BatchFoldJob`. See :class:`BatchFoldJob` for full API."""

    def __init__(
        self,
        transport: Any,
        *,
        batch_id: str,
        jobs: list[dict[str, Any]],
        total_cost_credits: int,
        peptide_count: int,
        trajectories_per_peptide: int,
        receptor: dict[str, Any] | None = None,
        sampling_steps: int | None = None,
    ) -> None:
        from ligandai.jobs import AsyncJob  # local to avoid circular import

        self._transport = transport
        self._batch_id = batch_id
        self._jobs_meta = jobs
        self._total_cost_credits = int(total_cost_credits)
        self._peptide_count = int(peptide_count)
        self._trajectories_per_peptide = int(trajectories_per_peptide)
        self._receptor = receptor or {}
        self._sampling_steps = sampling_steps
        self._sub_jobs: list[Any] = []
        for entry in jobs:
            jid = entry.get("job_id") or entry.get("jobId") or ""
            if not jid:
                self._sub_jobs.append(None)
                continue
            self._sub_jobs.append(
                AsyncJob(
                    transport,
                    jid,
                    job_type="folding",
                    parser=_parse_fold,
                    status_path="/api/folding/jobs/{job_id}",
                    cancel_path="/api/folding/jobs/{job_id}",
                    sse_path="/api/folding/jobs/{job_id}/logs/stream",
                    initial={"id": jid, "type": "folding", "status": "queued"},
                )
            )
        self._results: list[FoldResult | None] | None = None

    @property
    def batch_id(self) -> str:
        return self._batch_id

    @property
    def jobs(self) -> list[dict[str, Any]]:
        return list(self._jobs_meta)

    @property
    def total_cost_credits(self) -> int:
        return self._total_cost_credits

    @property
    def peptide_count(self) -> int:
        return self._peptide_count

    @property
    def trajectories_per_peptide(self) -> int:
        return self._trajectories_per_peptide

    @property
    def receptor(self) -> dict[str, Any]:
        return dict(self._receptor)

    @property
    def sub_jobs(self) -> list[Any]:
        return list(self._sub_jobs)

    async def wait(
        self,
        timeout: float = 7200.0,
        poll_interval: float = 5.0,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> list[FoldResult | None]:
        import asyncio
        import time
        from ligandai.errors import LigandAITimeoutError

        deadline = time.monotonic() + timeout
        total = len(self._sub_jobs)
        while True:
            done = 0
            failed = 0
            for sub in self._sub_jobs:
                if sub is None:
                    failed += 1
                    done += 1
                    continue
                if sub.is_terminal:
                    done += 1
                    if not sub.succeeded:
                        failed += 1
                else:
                    try:
                        await sub.refresh()
                        if sub.is_terminal:
                            done += 1
                            if not sub.succeeded:
                                failed += 1
                    except LigandAILegalAcceptanceRequired:
                        raise  # see BatchFoldJob.wait — not a transient error
                    except Exception:
                        pass
            if on_progress is not None:
                try:
                    on_progress({
                        "batch_id": self._batch_id,
                        "done": done,
                        "total": total,
                        "failed": failed,
                    })
                except Exception:
                    pass
            if done >= total:
                break
            if time.monotonic() > deadline:
                raise LigandAITimeoutError(
                    f"Batch {self._batch_id} did not complete within {timeout}s "
                    f"({done}/{total} sub-jobs terminal)"
                )
            await asyncio.sleep(poll_interval)
        # Force result parse
        out: list[FoldResult | None] = []
        for sub in self._sub_jobs:
            if sub is None:
                out.append(None)
                continue
            try:
                # AsyncJob.results may be a sync property that fetches lazily;
                # if it requires an await on a loader, fall back to a fresh
                # refresh + parse here.
                out.append(sub.results)
            except LigandAILegalAcceptanceRequired:
                raise  # never a silent `None` result
            except Exception:
                out.append(None)
        self._results = out
        return list(out)

    @property
    def results(self) -> list[FoldResult | None]:
        if self._results is None:
            return [None] * len(self._sub_jobs)
        return list(self._results)

    @property
    def folds(self) -> list[FoldResult | None]:
        return self.results

    async def cancel(self) -> int:
        n = 0
        for sub in self._sub_jobs:
            if sub is None:
                continue
            try:
                if not sub.is_terminal and await sub.cancel():
                    n += 1
            except LigandAILegalAcceptanceRequired:
                raise  # see BatchFoldJob.cancel
            except Exception:
                pass
        return n

    def __len__(self) -> int:
        return len(self._sub_jobs)

    def __repr__(self) -> str:
        return (
            f"AsyncBatchFoldJob(batch_id={self._batch_id!r}, "
            f"peptide_count={self._peptide_count}, "
            f"trajectories_per_peptide={self._trajectories_per_peptide}, "
            f"total_cost_credits={self._total_cost_credits})"
        )


def _first_target_gene(payload: dict[str, Any]) -> str | None:
    targets = payload.get("targets")
    if isinstance(targets, list) and targets:
        first = targets[0]
        if isinstance(first, dict):
            return first.get("gene")
    return None


def _set_if_missing(out: dict[str, Any], key: str, value: Any) -> None:
    if value is not None and out.get(key) is None:
        out[key] = value


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _flatten_peptide(raw: dict[str, Any], gene: str | None = None) -> dict[str, Any]:
    """Promote quality_scores sub-fields to top level for Peptide mapping."""
    if not isinstance(raw, dict):
        return {"sequence": str(raw), **({"targetGene": gene} if gene else {})}
    qs = raw.get("quality_scores") or {}
    out = dict(raw)
    if gene and not out.get("targetGene") and not out.get("target_gene"):
        out["targetGene"] = gene

    _set_if_missing(out, "ligandiq", _first_present(raw.get("ligandiq_score"), qs.get("ligandiq_score")))
    _set_if_missing(out, "predictedIpsae", _first_present(raw.get("predicted_ipsae"), qs.get("predicted_ipsae")))
    predicted_iptm = _first_present(
        raw.get("predicted_iptm"),
        raw.get("pred_iptm"),
        raw.get("ligandiq_pred_iptm"),
        qs.get("predicted_iptm"),
        qs.get("pred_iptm"),
        qs.get("ligandiq_pred_iptm"),
    )
    legacy_predicted_ptm = _first_present(raw.get("predicted_ptm"), qs.get("predicted_ptm"))
    # Legacy production LigandIQ payloads normalize the platform's pred_iptm head into
    # quality_scores.predicted_ptm. Expose it only as predicted_iptm; current
    # LigandIQ does not emit a distinct predicted pTM head.
    if predicted_iptm is None and (
        raw.get("ligandiq_score") is not None
        or qs.get("ligandiq_score") is not None
        or raw.get("predicted_ipsae") is not None
        or qs.get("predicted_ipsae") is not None
    ):
        predicted_iptm = legacy_predicted_ptm
    out.pop("predicted_ptm", None)
    out.pop("predictedPtm", None)
    _set_if_missing(out, "predictedIptm", predicted_iptm)
    _set_if_missing(out, "predictedPlddt", _first_present(raw.get("predicted_plddt"), qs.get("predicted_plddt")))
    _set_if_missing(out, "binderProb", _first_present(raw.get("binder_prob"), qs.get("binder_prob")))

    # Stability / immuno / half-life (academia+ tier, may be None).
    #
    # NULL-GRADE ROOT-CAUSE FIX (bd-dre-h0cuo.2.3): the immuno mapping used to
    # read ``immuno_scores.get("immunogenicityScore")`` — a key that DOES NOT
    # EXIST in the persisted immuno_scores dict (real keys are ``immuno_grade``
    # and ``immuno_risk_score``). So immunogenicityScore was always None even
    # when grades were present. Stability read the right key (``stability_grade``)
    # and was fine. Half-life (which rides inside stability_scores) was never
    # surfaced at all. All three are now mapped from their real keys.
    ss = raw.get("stability_scores")
    if isinstance(ss, dict):
        if not out.get("stabilityGrade") and not out.get("stability_grade"):
            out["stabilityGrade"] = ss.get("stability_grade")
        if out.get("halfLifeHours") is None and out.get("predicted_halflife_hours") is None:
            out["halfLifeHours"] = ss.get("predicted_halflife_hours")
        if out.get("halfLifeMin") is None and out.get("predicted_halflife_min") is None:
            out["halfLifeMin"] = ss.get("predicted_halflife_min")
    isd = raw.get("immuno_scores")
    if isinstance(isd, dict):
        if not out.get("immunoGrade") and not out.get("immuno_grade"):
            out["immunoGrade"] = isd.get("immuno_grade")
        # The numeric immunogenicity "score" is immuno_risk_score (0..1, lower
        # = less immunogenic). Keep the historical camelCase output key.
        if out.get("immunogenicityScore") is None and out.get("immunogenicity_score") is None:
            out["immunogenicityScore"] = isd.get("immuno_risk_score")
    return out


def _extract_peptides(payload: dict[str, Any]) -> list[dict[str, Any]]:
    pep = payload.get("peptides")
    if isinstance(pep, list):
        return [_flatten_peptide(p) for p in pep]
    # Dict keyed by gene (session detail format) → flatten all genes
    if isinstance(pep, dict):
        flat: list[dict[str, Any]] = []
        for gene, gene_peps in pep.items():
            if isinstance(gene_peps, list):
                flat.extend(_flatten_peptide(p, gene=str(gene)) for p in gene_peps)
        return flat
    nested = payload.get("results")
    if isinstance(nested, dict) and isinstance(nested.get("peptides"), list):
        return [_flatten_peptide(p) for p in nested["peptides"]]
    if isinstance(nested, list):
        return [_flatten_peptide(p) for p in nested]
    return []


def _has_generation_peptides(payload: dict[str, Any]) -> bool:
    return bool(_extract_peptides(payload))


def _unwrap_session_response(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the session object from the common session endpoint envelopes."""
    if not isinstance(payload, dict):
        return {}
    session = payload.get("session")
    if isinstance(session, dict):
        return session
    data = payload.get("data")
    if isinstance(data, dict):
        nested = data.get("session")
        if isinstance(nested, dict):
            return nested
        if "peptides" in data:
            return data
    return payload


def _session_id_from_payload(payload: dict[str, Any]) -> str | None:
    sid = (
        payload.get("sessionId")
        or payload.get("session_id")
        or payload.get("sessionID")
        or payload.get("id")
    )
    return sid if isinstance(sid, str) else None


def _to_optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _iter_session_fold_results(
    fold_results: Any,
) -> Iterator[tuple[str | None, dict[str, Any]]]:
    """Yield ``(gene, fold_entry)`` pairs from the session ``fold_results`` blob.

    The session detail endpoint returns ``fold_results`` keyed by gene
    (``{"PRLR": [ {...}, ... ]}``). A few legacy/list-shaped payloads return a
    flat list of fold dicts. Handle both so the merge is robust to either.
    """
    if isinstance(fold_results, dict):
        for gene, entries in fold_results.items():
            if isinstance(entries, list):
                for entry in entries:
                    if isinstance(entry, dict):
                        yield (str(gene) if gene is not None else None), entry
    elif isinstance(fold_results, list):
        for entry in fold_results:
            if isinstance(entry, dict):
                yield None, entry


def _merge_fold_results_into_peptides(
    peptides: Any,
    fold_results: Any,
) -> None:
    """Stamp post-fold metrics from ``fold_results`` onto matching peptides.

    Root cause this fixes (bd-LIGANDAI_ALPHA_V2-u4gss): the session detail
    endpoint returns the pre-fold peptide list (``session['peptides']``) and the
    post-fold structural scores (``session['fold_results']``) as SEPARATE
    structures. The server only stamps ``peptide.folded=true`` — it does NOT
    copy ipsae/iptm/deltaG/Kd back onto the peptide. Without this merge,
    ``generate(auto_fold=True)`` returns peptides whose post-fold fields are all
    None, so callers see ``n_folded == 0`` even when folding succeeded.

    Folds are matched to peptides by sequence (within a gene when both carry
    one). When the same sequence has multiple folded conformations we keep the
    one with the highest iPSAE so the surfaced metrics reflect the best pose.

    Mutates the peptide dicts in place. ``peptides`` may be a flat list or a
    dict keyed by gene (session detail format), matching ``_extract_peptides``.
    """
    if not fold_results:
        return

    # Build best-fold-per-(gene, sequence) and per-sequence (gene-agnostic) maps.
    best_by_gene_seq: dict[tuple[str, str], dict[str, Any]] = {}
    best_by_seq: dict[str, dict[str, Any]] = {}
    for gene, fold in _iter_session_fold_results(fold_results):
        seq = fold.get("sequence")
        if not isinstance(seq, str) or not seq:
            continue
        ipsae = _to_optional_float(fold.get("ipsae"))

        def _is_better(existing: dict[str, Any]) -> bool:
            prev = _to_optional_float(existing.get("ipsae"))
            return ipsae is not None and (prev is None or ipsae > prev)

        if best_by_seq.get(seq) is None or _is_better(best_by_seq[seq]):
            best_by_seq[seq] = fold
        if gene is not None:
            key = (gene.upper(), seq)
            if best_by_gene_seq.get(key) is None or _is_better(best_by_gene_seq[key]):
                best_by_gene_seq[key] = fold

    if not best_by_seq:
        return

    def _lookup(pep: dict[str, Any]) -> dict[str, Any] | None:
        seq = pep.get("sequence")
        if not isinstance(seq, str) or not seq:
            return None
        gene = pep.get("targetGene") or pep.get("target_gene") or pep.get("gene")
        if isinstance(gene, str):
            hit = best_by_gene_seq.get((gene.upper(), seq))
            if hit is not None:
                return hit
        return best_by_seq.get(seq)

    def _apply(pep: dict[str, Any]) -> None:
        fold = _lookup(pep)
        if fold is None:
            return
        scores = fold.get("scores") if isinstance(fold.get("scores"), dict) else {}
        # Post-fold structural scores → Peptide field names.
        for field, candidates in (
            ("ipsae", ("ipsae",)),
            ("iptm", ("iptm",)),
            ("ptm", ("ptm",)),
            ("plddt", ("plddt",)),
        ):
            value = _first_present(
                *(fold.get(c) for c in candidates),
                *(scores.get(c) for c in candidates),
            )
            _set_if_missing(pep, field, _to_optional_float(value))
        # DeltaForge ΔG → deltaforge_dg (alias deltaforgeDg).
        dg = _first_present(
            fold.get("deltaG"), fold.get("delta_g"),
            scores.get("deltaG"), scores.get("delta_g"),
        )
        _set_if_missing(pep, "deltaforgeDg", _to_optional_float(dg))
        # Predicted Kd → predicted_kd (alias predictedKd).
        kd = _first_present(
            fold.get("predictedKd"), fold.get("predicted_kd"), fold.get("kd_nm"),
            scores.get("predictedKd"), scores.get("predicted_kd"),
        )
        _set_if_missing(pep, "predictedKd", _to_optional_float(kd))
        pep["folded"] = True

    if isinstance(peptides, list):
        for pep in peptides:
            if isinstance(pep, dict):
                _apply(pep)
    elif isinstance(peptides, dict):
        for gene, gene_peps in peptides.items():
            if not isinstance(gene_peps, list):
                continue
            for pep in gene_peps:
                if isinstance(pep, dict):
                    pep.setdefault("targetGene", str(gene))
                    _apply(pep)


def _generation_result_from_session(
    payload: dict[str, Any],
    session_response: dict[str, Any],
    *,
    fallback_session_id: str | None,
    fallback_gene: str | None,
) -> dict[str, Any]:
    session = _unwrap_session_response(session_response)
    result = dict(payload)

    session_id = (
        _session_id_from_payload(result)
        or _session_id_from_payload(session)
        or fallback_session_id
    )
    if session_id:
        result.setdefault("sessionId", session_id)
        result.setdefault("jobId", session_id)

    gene = (
        result.get("gene")
        or session.get("gene")
        or _first_target_gene(result)
        or _first_target_gene(session)
        or fallback_gene
    )
    if gene:
        result.setdefault("gene", gene)

    if not _has_generation_peptides(result) and session.get("peptides") is not None:
        result["peptides"] = session["peptides"]

    # bd-LIGANDAI_ALPHA_V2-u4gss: the session detail endpoint returns the
    # pre-fold peptide list and the post-fold scores (``fold_results``) as
    # SEPARATE structures; the server only stamps ``peptide.folded=true``. Merge
    # the fold metrics (ipsae/iptm/ptm/plddt/ΔG/Kd) onto the matching peptides so
    # generate(auto_fold=True) surfaces the folded scores instead of reporting
    # n_folded=0. Operates on whichever peptide blob we ended up with (result's
    # own, or the session-copied one).
    session_fold_results = (
        session.get("fold_results")
        or session.get("foldResults")
        or session.get("folds")
    )
    if session_fold_results and result.get("peptides") is not None:
        _merge_fold_results_into_peptides(result["peptides"], session_fold_results)

    if result.get("totalGenerated") is None:
        result["totalGenerated"] = (
            session.get("totalGenerated")
            or session.get("total_generated")
            or session.get("total")
            or len(_extract_peptides(result))
            or None
        )

    if result.get("parameters") is None:
        result["parameters"] = _public_parameters(
            session.get("parameters") or session.get("config")
        )

    return result


# ── Multi-target concurrent generation ────────────────────────────────────
# bd-dre-1u72b. One request, N receptors, one session, concurrent generation
# on the platform side, per-target progress and per-target peptides.
#
# WHY THIS EXISTS AS ITS OWN METHOD RATHER THAN `gene=` GROWING A LIST TYPE:
# ``generate(gene=...)`` already serializes to a ONE-ELEMENT ``targets`` array
# on the wire (``_generation_body`` -> ``"targets": [_generation_target(...)]``)
# and ``POST /api/ptf/parallel/generate`` has accepted an N-element array all
# along — it is literally documented server-side as "Start parallel peptide
# generation across multiple targets". What did not exist was any way for an SDK
# caller to SAY N. Widening ``gene`` to ``str | list[str]`` would have made every
# downstream single-target affordance (``fallback_gene``, the per-gene result
# unwrap, ``sessions/by-gene``) ambiguous at runtime instead of at the type
# level, so the multi-target entry point is explicit.
#
# ⛔ WHAT THIS DOES *NOT* CLAIM. Per-target CONCURRENCY is the platform's, not
# this method's: the orchestrator runs targets in batches (see
# ``GEN_BATCH_SIZE`` in server/ptf-integration-routes.ts) and this method's job
# is to hand it the whole list at once so it can. Submitting N targets in ONE
# request is also the only way to earn the volume discount, which is computed on
# TOTAL peptides across targets, not per target — see ``estimate_multi_cost``.

# CREDIT_PRICING.generation.perPeptide (shared/schema.ts) — credits per DISPLAYED
# peptide. Not the fold rate; folding is billed separately.
_CREDITS_PER_PEPTIDE = 100

_MultiTargetSpec = Union[str, Mapping[str, Any]]

# Per-target keys a caller may vary between targets in one request. Everything
# NOT in this set (model, guidance, length range, peptide count, fold settings)
# is SESSION-level on this endpoint and is therefore shared by every target —
# the server reads ``generatorModelId`` and the guidance flags once, off the
# request root, never off a target (verified against the handler's destructure).
_PER_TARGET_KEYS: frozenset[str] = frozenset({
    "gene", "target_residues", "avoid_residues", "hotspot_residues",
    "target_chains", "targeting_strategy", "variant_id", "user_pdb_id",
    "uniprot_id", "pdb_id", "pdb_data",
})


def _normalize_multi_targets(
    targets: Iterable[_MultiTargetSpec],
) -> list[dict[str, Any]]:
    """Turn the caller's target list into wire ``targets`` entries.

    Accepts a bare gene symbol (``"CD274"``) or a mapping carrying any of
    :data:`_PER_TARGET_KEYS`. Rejects an empty list, a blank gene, and a
    duplicate gene — a duplicate is never a legitimate request here (the
    platform reuses one session per gene, so the second copy could only ever
    return the first copy's peptides) and silently collapsing it would hide
    that from the caller.
    """
    targets = list(targets)
    if not targets:
        raise LigandAIError("generate_multi(targets=...) requires at least one target")

    wire: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for idx, spec in enumerate(targets):
        if isinstance(spec, str):
            spec_map: Mapping[str, Any] = {"gene": spec}
        elif isinstance(spec, Mapping):
            spec_map = spec
        else:
            raise LigandAIError(
                f"generate_multi target #{idx} must be a gene symbol or a mapping, "
                f"got {type(spec).__name__}"
            )

        unknown = set(spec_map) - _PER_TARGET_KEYS
        if unknown:
            raise LigandAIError(
                f"generate_multi target #{idx} ({spec_map.get('gene', '?')}) carries "
                f"{sorted(unknown)}, which are SESSION-level settings on this endpoint, "
                "not per-target ones. Pass them once as generate_multi(...) keyword "
                f"arguments instead. Per-target keys are: {sorted(_PER_TARGET_KEYS)}"
            )

        gene = str(spec_map.get("gene") or "").strip()
        if not gene:
            raise LigandAIError(f"generate_multi target #{idx} has no gene")
        key = gene.upper()
        if key in seen:
            raise LigandAIError(
                f"generate_multi target #{idx} repeats gene {key!r} (already at index "
                f"{seen[key]}). The platform serves one session per gene, so the "
                "duplicate could only return the first target's peptides."
            )
        seen[key] = idx

        strategy = spec_map.get("targeting_strategy")
        target_residues = spec_map.get("target_residues")
        effective_strategy = (
            strategy if strategy is not None
            else ("pocket_targeted" if target_residues else "full_surface")
        )
        entry = _generation_target(
            gene,
            target_residues,                       # type: ignore[arg-type]
            effective_strategy,                    # type: ignore[arg-type]
            spec_map.get("variant_id"),            # type: ignore[arg-type]
            spec_map.get("user_pdb_id"),           # type: ignore[arg-type]
        )
        # Optional per-target extras the Express handler lifts off each target.
        avoid = _serialize_residue_ranges(spec_map.get("avoid_residues"))  # type: ignore[arg-type]
        if avoid is not None:
            entry["avoidResidues"] = avoid
        hotspots = spec_map.get("hotspot_residues")
        if hotspots:
            entry["hotspotResidues"] = [int(h) for h in hotspots]
        chains = spec_map.get("target_chains")
        if chains:
            entry["targetChains"] = list(chains)
        for src, dst in (("uniprot_id", "uniprotId"), ("pdb_id", "pdbId"), ("pdb_data", "pdbData")):
            if spec_map.get(src) is not None:
                entry[dst] = spec_map[src]
        wire.append(entry)
    return wire


def _guidance_was_requested(kwargs: Mapping[str, Any]) -> bool:
    """True iff this call asked for any guidance head.

    Kept in terms of the SAME knobs the platform's guidance transport reads, so
    the reuse guard fires on exactly the settings the reuse key omits.
    """
    if kwargs.get("immunogenicity") or kwargs.get("serum_stability") or kwargs.get("cellfree"):
        return True
    if kwargs.get("residue_composition") is not None or kwargs.get("halflife") is not None:
        return True
    if kwargs.get("cysteine_enabled") is True:
        return True
    return False


def _guard_reused_session(
    payload: Mapping[str, Any],
    *,
    requested_model: str | None,
    guidance_requested: bool,
    allow_reused_session: bool,
) -> None:
    """⛔ P0 GUARD — refuse to pass off a REUSED session as a fresh run.

    ``POST /api/ptf/parallel/generate`` answers ``success: true`` with a real
    ``sessionId`` in three different situations, and only one of them is the run
    the caller asked for:

    1. a genuinely new session,
    2. a 60-second idempotency-cache hit (``idempotent: true``), and
    3. a 2-hour gene reuse (``deduplicated: true`` + ``skippedGenes``).

    Neither (2) nor (3) hashes ``generatorModelId`` or any guidance flag, so a
    request that changes ONLY the model or ONLY the guidance lands on the
    earlier session and its peptides come back under the new request's label.
    Measured 2026-08-21 across all five generator models: every accepted cell
    resolved to one v6.5.21-no-guidance session until the TARGETING was varied.

    This raises when the caller pinned a model or asked for guidance — i.e.
    precisely when the reuse is known to be lying — and stays quiet otherwise,
    because reusing a session for an identical repeat request is the behaviour
    that feature was built for (it stops double-debits on a retry).
    """
    if allow_reused_session:
        return
    reused_gene_dedup = bool(payload.get("deduplicated"))
    reused_idempotent = bool(payload.get("idempotent"))
    if not (reused_gene_dedup or reused_idempotent):
        return
    if not (requested_model or guidance_requested):
        return  # identical repeat request — reuse is correct and intended
    layer = "gene_dedup" if reused_gene_dedup else "idempotency_cache"
    sid = (
        payload.get("existingSessionId")
        or payload.get("sessionId")
        or payload.get("session_id")
    )
    skipped = payload.get("skippedGenes") or []
    asked = []
    if requested_model:
        asked.append(f"model={requested_model!r}")
    if guidance_requested:
        asked.append("guidance heads")
    raise LigandAIReusedSessionError(
        "The platform returned an EXISTING generation session "
        f"({sid}) instead of running this request. Its reuse key ({layer}) does not "
        f"include the generator model or any guidance flag, and this call asked for "
        f"{' and '.join(asked)} — so those peptides were NOT produced with the "
        "settings requested here. Vary the TARGETING (hotspot_residues= / "
        "target_residues=) to force a genuinely new run, or pass "
        "allow_reused_session=True to accept the existing session knowingly.",
        session_id=sid if isinstance(sid, str) else None,
        requested_model=requested_model,
        guidance_requested=guidance_requested,
        reuse_layer=layer,
        skipped_genes=[str(g) for g in skipped] if isinstance(skipped, list) else [],
        response=dict(payload),
    )


def estimate_multi_cost(num_targets: int, peptides_per_target: int) -> dict[str, Any]:
    """Credit cost of an N-target generation, and what it saves over N separate runs.

    ⛔ MIRRORS the server's formula (shared/schema.ts CREDIT_PRICING +
    shared/forge-pricing.ts GENERATION_VOLUME_TIERS, applied in the
    ``/api/ptf/parallel/generate`` handler). It is an ESTIMATE, not a quote —
    the server is the authority, tier caps can reduce the billable count, and a
    superadmin/unlimited account is not charged at all.

    Guidance is FREE as priced today: the four per-peptide guidance surcharges
    (``qualityGuidedGenPerPeptide``, ``ligandiqFoldRankingPerPeptide``,
    ``immunoGuidancePerPeptide``, ``stabilityGuidancePerPeptide``) are all 0 in
    the platform's pricing table, so this deliberately does NOT take guidance
    arguments — adding them would imply a price difference that does not exist.
    FOLDING is billed separately and is not counted here.

    THE WHOLE POINT: the volume discount is computed on TOTAL peptides across
    every target in ONE request, not per target. Ten targets x 100 peptides
    submitted together is 1,000 peptides and earns the 1,000-peptide tier;
    submitted as ten separate runs it is ten lots of 100 and earns only the
    100-peptide tier, ten times. Same GPUs, same peptides, different bill.
    """
    n = int(num_targets) * int(peptides_per_target)
    if n <= 0:
        raise LigandAIError("estimate_multi_cost needs positive targets and peptides")

    def _bulk_pct(total: int) -> int:
        if total >= 5000:
            return 50
        if total >= 1000:
            return 30
        if total >= 300:
            return 20
        if total >= 100:
            return 10
        return 0

    def _cost(total: int) -> float:
        return total * _CREDITS_PER_PEPTIDE * (1 - _bulk_pct(total) / 100)

    joint = math.ceil(_cost(n))
    serial = int(num_targets) * math.ceil(_cost(int(peptides_per_target)))
    return {
        "num_targets": int(num_targets),
        "peptides_per_target": int(peptides_per_target),
        "total_peptides": n,
        "bulk_discount_percent": _bulk_pct(n),
        "credits_one_request": joint,
        "credits_n_separate_runs": serial,
        "credits_saved": serial - joint,
        "credits_per_target": round(joint / int(num_targets), 1),
    }


def _attach_multi_target_helpers(job: Any, genes: list[str]) -> None:
    """Bolt per-target accessors onto a generation :class:`~ligandai.jobs.Job`.

    ⛔ WHY ON THE JOB AND NOT ON THE RESULT. ``GenerationResult`` is a
    single-gene shape — ``_generation_result_from_session`` unwraps ONE gene out
    of the session's gene-keyed ``peptides`` dict, using the fallback gene the
    job was created with. For an N-target run that would quietly hand back the
    first target's peptides as if they were the whole run. These accessors read
    the gene-keyed structures the server actually returns, so nothing is
    unwrapped and nothing is lost.

    ``target_status()``      -> ``{gene: {"status", "peptidesGenerated", ...}}``
                                from ``GET /api/ptf/parallel/{id}/status``,
                                which already returns one row per target.
    ``peptides_by_target()`` -> ``{gene: [peptide, ...]}`` from
                                ``GET /api/ptf/sessions/{id}``, whose
                                ``session.peptides`` is keyed by gene.
    ``targets``              -> the gene list this job was submitted with.
    """
    transport = job._transport
    job_id = job.id

    def target_status() -> dict[str, dict[str, Any]]:
        payload = transport.request("GET", f"/api/ptf/parallel/{job_id}/status") or {}
        rows = payload.get("generation")
        out: dict[str, dict[str, Any]] = {}
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, Mapping) and row.get("gene"):
                    out[str(row["gene"])] = dict(row)
        # A target the server has not reported on yet is PENDING, not missing —
        # returning a short dict would read as "that target is done".
        for gene in genes:
            out.setdefault(gene, {"gene": gene, "status": "pending", "peptidesGenerated": 0})
        return out

    def peptides_by_target() -> dict[str, list[dict[str, Any]]]:
        payload = transport.request("GET", f"/api/ptf/sessions/{job_id}") or {}
        session = payload.get("session") or {}
        by_gene = session.get("peptides")
        out: dict[str, list[dict[str, Any]]] = {}
        if isinstance(by_gene, Mapping):
            for gene, peps in by_gene.items():
                out[str(gene)] = list(peps) if isinstance(peps, list) else []
        for gene in genes:
            out.setdefault(gene, [])
        return out

    async def atarget_status() -> dict[str, dict[str, Any]]:
        payload = await transport.request("GET", f"/api/ptf/parallel/{job_id}/status") or {}
        rows = payload.get("generation")
        out: dict[str, dict[str, Any]] = {}
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, Mapping) and row.get("gene"):
                    out[str(row["gene"])] = dict(row)
        for gene in genes:
            out.setdefault(gene, {"gene": gene, "status": "pending", "peptidesGenerated": 0})
        return out

    async def apeptides_by_target() -> dict[str, list[dict[str, Any]]]:
        payload = await transport.request("GET", f"/api/ptf/sessions/{job_id}") or {}
        session = payload.get("session") or {}
        by_gene = session.get("peptides")
        out: dict[str, list[dict[str, Any]]] = {}
        if isinstance(by_gene, Mapping):
            for gene, peps in by_gene.items():
                out[str(gene)] = list(peps) if isinstance(peps, list) else []
        for gene in genes:
            out.setdefault(gene, [])
        return out

    is_async = isinstance(job, AsyncJob)
    job.targets = list(genes)                                   # type: ignore[attr-defined]
    job.target_status = atarget_status if is_async else target_status          # type: ignore[attr-defined]
    job.peptides_by_target = apeptides_by_target if is_async else peptides_by_target  # type: ignore[attr-defined]



def _load_generation_result(
    transport: Any,
    info: Any,
    *,
    fallback_session_id: str | None,
    fallback_gene: str | None,
) -> dict[str, Any] | None:
    payload = dict(info.result or {})
    if _has_generation_peptides(payload):
        return payload
    session_id = _session_id_from_payload(payload) or fallback_session_id or getattr(info, "id", None)
    if not session_id:
        return payload
    session_response = transport.request("GET", f"/api/ptf/sessions/{session_id}") or {}
    return _generation_result_from_session(
        payload,
        session_response,
        fallback_session_id=session_id,
        fallback_gene=fallback_gene,
    )


async def _aload_generation_result(
    transport: Any,
    info: Any,
    *,
    fallback_session_id: str | None,
    fallback_gene: str | None,
) -> dict[str, Any] | None:
    payload = dict(info.result or {})
    if _has_generation_peptides(payload):
        return payload
    session_id = _session_id_from_payload(payload) or fallback_session_id or getattr(info, "id", None)
    if not session_id:
        return payload
    session_response = await transport.request("GET", f"/api/ptf/sessions/{session_id}") or {}
    return _generation_result_from_session(
        payload,
        session_response,
        fallback_session_id=session_id,
        fallback_gene=fallback_gene,
    )


# -- Sync resource ----------------------------------------------------------


#: ``GET /api/ptf/peptides/by-pocket`` answers
#: ``{"success", "gene", "totalResults", "sessionBreakdown", "results": [...]}``
#: and ``GET /api/ptf/parallel/{sid}/elite`` answers
#: ``{"success", "sessionId", "threshold", "totalEliteHits", "eliteHits": [...]}``
#: — measured against blue 2026-10-02. Both were read under ``peptides``, a key
#: neither route has ever sent, so both methods reported zero prior designs for
#: a gene that had them. [bd-dre-3imtv.3]
_BY_POCKET_KEYS = ("results", "peptides", "data")
_ELITE_KEYS = ("eliteHits", "elite_hits", "peptides", "results")

#: ``GET /api/ptf/sessions/by-gene/{gene}`` rows key the session id
#: ``sessionId``; ``session_id`` is what ``/api/ptf/sessions`` sends.
_SESSION_ID_KEYS = ("sessionId", "session_id", "id")


def _session_id_for_gene(payload: object, gene: str | None) -> str | None:
    """Newest session id out of ``/api/ptf/sessions/by-gene/{gene}``.

    ⛔ This used to be ``payload.get("id")`` against a body that has no ``id``
    at the top level — it carries ``{"success", "gene", "totalSessions",
    "sessions": [...]}`` (232 sessions for EGFR, measured 2026-10-02). So
    ``get_elite(gene=...)`` raised "No active session" for every gene on the
    platform, including ones with hundreds. [bd-dre-3imtv.3]
    """
    if not isinstance(payload, dict):
        return None
    for key in _SESSION_ID_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    rows = payload.get("sessions")
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, str) and row:
                return row
            if isinstance(row, dict):
                for key in _SESSION_ID_KEYS:
                    value = row.get(key)
                    if isinstance(value, str) and value:
                        return value
    return None


class Peptides(Resource):
    """Generation, folding, scoring, and search."""

    def generate(
        self,
        gene: str,
        num_peptides: int | None = None,
        length_range: tuple[int, int] = (20, 70),
        # PDB RESIDUE NUMBERS as numbered in the receptor structure being designed
        # against — NOT indices into any internal array, and not sequence offsets.
        # `chain` is honoured. If your construct is renumbered 1..N, use the
        # construct's numbering.
        target_residues: list[ResidueRange] | None = None,
        # Receptor regions to keep clear, in the same PDB numbering as
        # target_residues (list of ResidueRange, or dicts with chain/start/end).
        # The server removes these residues — and, by default, the residues packing
        # against them within 8 A (CA-CA) — from the pocket the generator is
        # conditioned on.
        #
        # Applied in every targeting mode: you do NOT need to supply
        # target_residues/hotspot_residues/pocket_config for it to reach the
        # featurization. (It previously only subtracted from an already-narrowed
        # target selection, so naming an avoid region alone did nothing at all.)
        #
        # ⚠ It does NOT keep designs off that surface. Measured on the deployed
        # generator, the exclusion did not change the generated sequence
        # distribution (p = 1.00, JSD 2.4e-5), and gate-passing designs still
        # contacted the excluded patch at ~2x the chance rate. It is a record of
        # intent on the request, not a constraint on the output. Establish
        # avoidance from the folded complex — never from the request.
        avoid_residues: list[ResidueRange] | None = None,
        # Flat list of PDB residue numbers (e.g. [792, 855]) naming the site of
        # interest — NOT indices, NOT sequence positions.
        # `{"chain": "A", "residue": 792}` dicts are also accepted (chain is
        # dropped before the request is sent; hotspot_residues has no chain on
        # the wire). Validated client-side (>= 1) before any HTTP call.
        #
        # ⚠ This selects which receptor rows are featurized, and it does
        # measurably change which sequences are generated (six receptor faces,
        # p ~ 1e-4, n=64/arm). It does NOT aim: contact at the named face was
        # not enriched on either receptor tested, and a randomly scattered
        # residue set moved the distribution as much as a real contiguous face.
        # Use it to vary a run; establish the binding site from the fold.
        hotspot_residues: list[int] | None = None,
        # Ca-Ca radius (Angstrom) of the packing shell removed around
        # avoid_residues, in addition to the named residues themselves.
        # None = server default (8.0 A); 0 disables the shell (named
        # residues only). Valid range [0, 50]. [bd-LIGANDAI_ALPHA_V2-x4vt8]
        avoid_shell_angstrom: float | None = None,
        target_chains: list[str] | None = None,
        fold_partners: _FoldPartnerMode | list[str] | None = None,
        # Run LABEL only — recorded on the run for bookkeeping/display. The
        # generation backend does not read it, and it does not by itself move the
        # design surface; the featurized residue set comes from target_residues /
        # hotspot_residues / pocket_config / avoid_residues. When target_residues
        # are supplied the SDK auto-switches this to "pocket_targeted" unless you
        # override here.
        targeting_strategy: _TargetingStrategy | None = None,
        # Receptor topology FACE override (bd-LIGANDAI_ALPHA_V2-q3z1b). For a
        # membrane receptor target, restrict the design pocket to a specific
        # topological face instead of the whole folded surface:
        #   "extracellular" → NC (non-cytoplasmic / extracellular) ranges only
        #   "ec_tm"         → NC + TM (the explicit form of the receptor default)
        #   "transmembrane" → TM ranges only
        #   "intracellular" → CY (cytoplasmic / intracellular) ranges only
        #   "full"          → no face restriction (whole surface)
        #   None (default)  → backend applies its receptor default
        #                     (extracellular + TM, excluding the intracellular face)
        # An extracellular peptide cannot reach the intracellular face in vivo,
        # so the default excludes IC; pass "intracellular" to override that for
        # cytoplasmic-delivery designs. The server resolves an explicit face to
        # the matching membrane-topology residue ranges and uses them as the
        # explicit-pocket target_residues.
        target_face: _TargetFace | None = None,
        # Hotspot pocket expansion radius in Angstroms. When target_residues
        # are provided, the server includes every residue within this radius
        # of any listed hotspot atom in the design pocket. This is the right
        # default for "design against residue X" — the peptide needs the
        # surrounding shell to make contacts, not just X itself. Set to 0 to
        # use the literal residues only (no expansion into the orthosteric site).
        # 0.0 is a valid, meaningful value — the server honors it as
        # "hotspots-only" regardless of how many residues surround the target.
        pocket_expansion_radius_a: float | None = 6.0,
        # Receptor flexible residues — 1-based receptor residue indices the
        # backend may relax (side-chain / limited backbone flexibility) during
        # generation/docking. None (default) keeps the receptor rigid.
        receptor_flexible_residues: Sequence[int] | None = None,
        auto_fold: bool = True,
        top_n_fold: int | None = None,
        ec_domain_trimming: bool = True,
        deimmunize_mode: bool = False,
        variant_id: int | None = None,
        user_pdb_id: int | None = None,
        gen_gpus: int = 1,
        fold_gpus: int = 1,
        program_id: int | str | None = None,
        conversation_id: str | None = None,
        cysteine_mode: _CysteineMode = "disulfide_only",
        # Tri-state switch for the cysteine kernel itself, independent of the
        # policy in ``cysteine_mode`` (bd-LIGANDAI_ALPHA_V2-b4f34). See the Args
        # section for why this exists and what it costs.
        cysteine_enabled: bool | None = None,
        quality_guided: bool = True,
        quality_guidance_scale: float = 1.0,
        # Generator model to design with — see ``_GeneratorModel`` above for the
        # per-model scope statements. Sent on the wire as ``generatorModelId``.
        # None (default) sends NO key, so the server applies the platform
        # default; the SDK deliberately keeps no copy of that default so a
        # platform-side change reaches you without an SDK upgrade.
        # All models currently known to this SDK build support the four
        # guidance heads. A future model without guidance support raises
        # LigandAIInvalidConfig client-side rather than dropping them silently.
        model: _GeneratorModel | str | None = None,
        immunogenicity: bool = False,
        immuno_strength: float = 2.0,
        immuno_modules: dict[str, bool] | None = None,
        serum_stability: bool = False,
        stability_strength: float = 2.0,
        stability_mode: _StabilityMode = "resist",
        stability_modules: dict[str, bool] | None = None,
        # Residue & terminal composition guidance (RENAMED from ``halflife`` in
        # bd-LIGANDAI_ALPHA_V2-b4f34 — it never measured a half-life). Coupled
        # with serum_stability/proteolytic; not an independent dial. Default
        # strength 2.0, server ceiling 4.0, unsupported above 2.0. See the Args
        # section below for the full statement of what the kernel does.
        residue_composition: _ResidueCompositionTarget | None = None,
        residue_composition_strength: float | None = None,
        # ⛔ DEPRECATED ALIASES — accepted so no existing caller breaks. Passing
        # either emits a DeprecationWarning; if both spellings are supplied and
        # they disagree, the NEW name wins. Default None (not 2.0) so "did the
        # caller actually pass this?" is answerable.
        halflife: _ResidueCompositionTarget | None = None,
        halflife_strength: float | None = None,
        # Cell-free expressability guidance. Lowers LOCAL HYDROPHOBICITY
        # (rolling 7-residue GRAVY window) inside the same v6.5-line penalty
        # loop as the other heads — it does NOT penalize K/R clusters (K/R
        # content and local cluster density both RISE under this guidance).
        # NOT a global net-charge objective; net charge moves ~-14.3% as a
        # SIDE EFFECT of the hydrophobicity term, not the target.
        # ⛔ 1.5 here is a KNOWN CROSS-LAYER MISMATCH with the worker signature's
        # own 2.0 (ligandforge_v6_5_guided_inference_unified.py:2118). See the
        # Args entry — it is flagged for decision, not silently reconciled.
        cellfree: bool = False,
        cellfree_strength: float = 1.5,
        # Charge / solubility filtering (server-tier gated; server activates
        # filtered design worker when any non-default constraint is present).
        charge_mode: _ChargeMode | None = None,
        charge_value: float | None = None,
        charge_min: float | None = None,
        charge_max: float | None = None,
        min_solubility: float | None = None,
        # Cyclization (tier-gated: academia/pro/enterprise).
        # Pass ``cyclic_mode="disulfide"`` for terminal Cys-Cys bridge (primary
        # recombinant-shippable mode), ``"lactam"`` for head-to-tail amide
        # closure (prediction/viz layer only), or ``"head_tail_contact"`` for
        # soft B-matrix bias without a synthesis constraint.
        # insufficient-tier users receive HTTP 403 from the server.
        cyclic_mode: _CyclicMode | None = None,
        cyclic_strength: float = 2.0,
        strict_recombinant: bool = True,
        dual_fold_viz: bool = False,
        folding_mode: str | None = None,
        # Default fold strategy: rank candidates by composite (LigandIQ × predicted
        # iPTM) before folding. The top peptides by this score get fold GPUs first
        # so credits go to the most promising designs. Override to ``"distributed"``
        # for round-robin across targets, ``"consolidated"`` for sequential
        # single-target folding, or pass ``fold_strategy=None`` to defer to server.
        fold_strategy: str | None = "quality_ranked",
        folding_conformations: str | list[str] | None = None,
        max_folds_per_target: int | None = None,
        enable_expansion: bool | None = None,
        auto_conformation_expansion: bool | None = None,
        clash_resolution_enabled: bool | None = None,
        md_relaxation_enabled: bool | None = None,
        num_trajectories: int | None = None,
        sampling_steps: int | None = None,
        glycosylation_enabled: bool | None = None,
        segments: list[PeptideSegment] | list[dict] | None = None,
        segment_config: SegmentConfig | dict | None = None,
        pdc_config: PdcConfig | dict | None = None,
        ec_trimming_config: EcTrimmingConfig | dict | None = None,
        # Ensemble auto-fold (v0.6.7). Default None = legacy single-Boltz-2
        # auto-fold (unchanged). Provide >=2 engines, or any engine other than
        # boltz2, to run the Phase-2 auto-fold of each top-N peptide on an
        # ENSEMBLE of engines with per-engine settings + a single shared
        # OUR-OWN MSA. camelCase aliases foldEngines/foldPerEngine/foldSharedMsa
        # accepted; snake_case wins on conflict. Refs: bd-LIGANDAI_ALPHA_V2-7y1uh
        fold_engines: list[str] | None = None,
        fold_per_engine: dict[str, Any] | None = None,
        fold_shared_msa: str | dict[str, Any] | None = None,
        # Cascade auto-fold (v0.6.7). Default off = legacy behaviour. When True,
        # fold the top-N peptides on Boltz-2 FIRST, then run the OTHER selected
        # engines ONLY on the Boltz-2 ipSAE/ipTM winners (boltz2 is reused, never
        # re-folded). Requires boltz2 in fold_engines. camelCase aliases accepted;
        # snake_case wins on conflict. Refs: bd-LIGANDAI_ALPHA_V2-7y1uh
        fold_cascade: bool = False,
        cascade_gate_metric: str = "ipsae",
        cascade_gate_threshold: float | None = 0.67,
        cascade_gate_top_n: int | None = None,
        # ── Local (on-prem) GPU routing — SUPERADMIN ONLY (bd-d7b23) ────────
        local: bool = False,
        # ── Contrastive guidance + avoidance (bd-dre-if562) ─────────────────
        contrastive: "ContrastiveGuidance | None" = None,
        avoidance: "AvoidancePolicy | None" = None,
        allow_unforwarded_contrastive: bool = False,
        # Name the off-target genes yourself — the customer-reachable surface
        # for paralog-conditioned generation (bd-dre-siz9d). Sends
        # cfg_reference_genes (and cfgReferenceGenes) on the wire, restricting
        # the server-side paralog reference producer to these genes instead of
        # requiring the raw (entitlement-gated) pocket-feature width that
        # contrastive=ContrastiveGuidance(...) needs. Requires a CFG-capable
        # model (model='v6.9.21' or 'v6.9.22') — raises LigandAIValidationError
        # naming them otherwise. Mutually exclusive with contrastive=: they are
        # two ways of naming one panel, and preferring one silently is how a
        # caller ends up steered away from something they did not choose.
        # None (default): with a CFG-capable model, the platform still derives
        # and applies its own real paralog references — you are just not
        # choosing which ones.
        avoid_paralogs: _StrSequence[str] | None = None,
        foldEngines: list[str] | None = None,
        foldPerEngine: dict[str, Any] | None = None,
        foldSharedMsa: str | dict[str, Any] | None = None,
        foldCascade: bool | None = None,
        cascadeGateMetric: str | None = None,
        cascadeGateThreshold: float | None = None,
        cascadeGateTopN: int | None = None,
        **extra: Any,
    ) -> Job[GenerationResult]:
        """Submit a peptide generation job. Returns a :class:`Job`.

        Args:
            gene: Target gene symbol (e.g. ``"EGFR"``).
            num_peptides: Peptides to generate per target. Tier caps are free=10,
                basic=100, academia/pro=300, enterprise=1000.
            length_range: ``(min_aa, max_aa)`` length bounds (default ``(20, 70)``).
            target_residues: Optional pocket residue ranges for guided targeting.
                ``start``/``end`` are **PDB residue numbers as numbered in the
                receptor structure** — not indices into an internal array, not
                sequence offsets — and ``chain`` is honoured. Multiple ranges may
                target one or more receptor chains. Use
                ``ResidueRange.from_residues([32, 33, 34], chain="A")`` to
                compress selected residues into continuous chain-local ranges.
            avoid_residues: Receptor residue ranges you want kept clear, in the
                same PDB numbering as ``target_residues``. The server removes
                them — and, by default, the residues packing against them within
                8 Å (Ca-Ca) — from the residue set the generator is featurized
                on. Applied in every targeting mode: you do not need to supply
                ``target_residues``, ``hotspot_residues`` or a pocket config for
                it to reach the featurization.

                ⚠ **It does not keep designs off that surface, and it does not
                change what is generated.** Measured on the deployed generator,
                the same request run with and against the exclusion produced
                indistinguishable sequence distributions (``p = 1.00``,
                Jensen-Shannon divergence ``2.4e-5``), and designs that cleared
                the downstream quality gates still contacted the excluded patch
                at roughly twice the chance rate. Treat this as a record of
                intent on the request and in run provenance. Establish avoidance
                from the **folded complex** — see
                :meth:`Evaluator.filter_folds` and the
                ``avoid_conformance_violation`` fold-stage diagnostic.

                Residue numbers with no counterpart in the receptor's featurized
                residue set are reported as ``unresolved_resnums`` on the
                generation stream's targeting events instead of failing the job.
            hotspot_residues: Flat list of PDB residue numbers naming the site of
                interest, e.g. ``[792, 855]`` — **PDB numbering, never an array
                index or sequence position**. A ``{"chain": "A", "residue": 792}``
                dict is also accepted per entry for bookkeeping convenience; only
                the residue number reaches the server (this field has no chain on
                the wire). Validated client-side (values must be integers >= 1)
                before any HTTP call — raises
                :class:`~ligandai.errors.LigandAIValidationError` immediately
                rather than submitting a request that silently no-ops.

                ⚠ **This is not placement control.** Selecting the featurized
                rows does measurably change which sequences are generated (six
                receptor faces, ``p ~ 1e-4``, 64 designs per arm) — but the
                change is not aimed at the named site: contact there was not
                enriched on either receptor tested, and a randomly scattered
                residue set moved the distribution as much as a genuine
                contiguous face. Use it to vary a run, then determine the actual
                binding site from the fold.
            avoid_shell_angstrom: Ca-Ca radius (Angstrom) of the packing shell
                removed around ``avoid_residues``, in addition to the named
                residues themselves. The default (``None`` → server default of
                8.0 Å) also removes residues packing directly against an avoided
                one, so the exclusion covers a contiguous patch of surface rather
                than isolated positions. Pass ``0`` to remove only the
                literally-named residues. Valid range ``[0, 50]``; validated
                client-side before any HTTP call. Widening the shell removes more
                rows from the featurization; it does not make the exclusion
                any more binding on the output.
            target_chains: Optional list of chain IDs (e.g. ``["C"]``) to
                restrict design to. Use this when a multimer (e.g. PDB
                ``9MIR``) has chains A/B/C/D and you only want peptides
                designed against chain C. The server filters conformations
                and the binding surface to only those chains.
            targeting_strategy: ``"full_surface"`` or ``"pocket_targeted"``.
                Recorded on the run as a label; the generation backend does not
                read it and it does not by itself move the design surface. The
                featurized residue set comes from ``target_residues`` /
                ``hotspot_residues`` / ``avoid_residues``.
            receptor_flexible_residues: 1-based receptor residue indices the
                backend may relax (side-chain / limited backbone flexibility)
                during generation/docking. ``None`` (default) keeps the receptor
                rigid; emitted on the wire as ``flexibleResidues`` only when set.
            auto_fold: Run Boltz-2 folding automatically after generation.
            top_n_fold: Cap on how many peptides to fold per target.
            ec_domain_trimming: Trim signal peptide / EC domain before generation.
            deimmunize_mode: Apply post-generation deimmunization.
            variant_id: Protein variant ID (from ``peptides.variants``).
            gen_gpus: GPU count for generation. Generation uses a one-GPU
                server path; higher values are ignored or clamped server-side.
            fold_gpus: GPU count for folding (default 1). Folding caps are
                free=1, basic=4, academia=16, pro=25, enterprise=50.
            program_id: Program/workstream ID to associate session with.
            cysteine_mode: Cysteine placement policy. **Default
                ``"disulfide_only"``.** Accepted values: ``"allow"`` /
                ``"allow_all"`` (unconstrained — this is the OFF position, the
                kernel returns zeros), ``"disulfide_only"`` /
                ``"stability_only"`` (0 cysteines, or a pair whose spacing can
                form one intramolecular disulfide), ``"exclude"`` /
                ``"exclude_all"`` (no cysteine at all).

                Enforced twice on the v6.5 line: as a logit penalty inside the
                sampling loop AND as a post-hoc sequence filter that DROPS
                non-conforming designs. The second one is a yield cost, not
                just a bias — rejection sampling with backpressure refill means
                ``num_peptides=N`` still returns N.
            cysteine_enabled: Tri-state switch for the cysteine kernel itself,
                separate from the policy in ``cysteine_mode``. **Default
                ``None``.**

                * ``True`` — the kernel fires with your ``cysteine_mode``, and
                  counts as a guidance request on its own. This is how you ask
                  for cysteine policy WITHOUT enabling any other head.
                * ``False`` — the kernel does NOT fire, whatever
                  ``cysteine_mode`` says.
                * ``None`` — legacy behaviour: the kernel fires if and only if
                  guidance is effectively active (at least one head enabled
                  *with strength > 0*, or an explicit
                  ``cysteine_enabled=True``).

                ⛔ WHY THIS EXISTS. On the v6.9 line the cysteine kernel was not
                independently controllable: it fired whenever any head carried
                an enabled flag, *regardless of strength*, so
                ``residue_composition`` enabled at strength 0.0 still activated
                it at ``CYSTEINE_KERNEL_STRENGTH=2.5``. It costs -17.7%
                between-receptor JSD (95% CI [-32.5%, -3.7%]) — a real,
                significant cost that callers were paying without asking for
                it, and which was for a while misattributed to the
                residue-composition knob's dose response. ``None`` keeps the old
                resolution for existing callers; the strength>0 condition is
                what closes "all knobs at zero is not the same as guidance off".

                Request provenance records ``cys_mode``, ``cysteine_enabled``
                and the RESOLVED ``cysteine_fired`` so what actually ran is
                recoverable after the fact.
            quality_guided: Request quality guidance. Defaults to True. Available
                to all authenticated tiers, including free; server-side credits and
                retention/licensing terms still apply.

                ⛔ EVIDENCE LEVEL: NOT IN-LOOP STEERING on the Modal generator
                deployments, which is where this call runs. Audited 2026-10-15
                (bd-dre-hq6u6): none of the six deployed generator worker modules
                contains any quality-head code, and the v6.5.12 quality heads live
                in a separate Modal app invoked only AFTER generation. So this flag
                does not change which sequences are sampled, and output from this
                call must not be described as quality-guided GENERATION.

                It is NOT inert. Measured: it selects the quality diffusion-step
                tier (100 steps with the flag vs 50 without, recorded as
                ``inference_config.num_diffusion_steps``), and the heads still
                score the designs afterwards
                (``guidance_heads.quality.role = post_generation_scoring``). The
                run record states which roles applied —
                ``guidance_heads.quality.in_loop_steering`` with a reason, plus a
                ``guidance_warnings`` entry and a request-stage diagnostic readable
                from the first status poll.

                The previous wording here claimed that without this flag "the base
                generator can collapse to low-diversity, degenerate sequences".
                That describes a sampling-time effect this path does not have.
                In-loop steering (Gumbel-softmax gradients through the quality
                predictor) exists only on the parallel-v2 batch surface and the
                local guided fallback.
            quality_guidance_scale: Scale for quality guidance (default 1.0).
            model: Generator model to design with, sent as ``generatorModelId``.
                Leave it unset (``None``, the default) to use the platform
                default — the SDK sends no key at all in that case, so the
                server decides and a platform-side change to the default reaches
                you without an SDK upgrade.

                * ``"v6.5.21"`` (default) — Receptor-conditioned. Designs
                  respond to the receptor you select. Replaces v6.5, which was
                  trained with all-zero receptor features and produced the same
                  peptides regardless of target.
                * ``"v6.9.21"`` — A different, larger receptor feature space
                  (107 per-residue channels vs 48), with classifier-free
                  guidance applied at inference. Validated for distinguishing
                  sequence composition between receptors ONLY — the paired
                  500-design fold screen (w0 1/284 vs w2 3/216) was
                  underpowered (resolves >=12x, observed 3.9x). No option on
                  this model moves WHERE the peptide contacts the structure —
                  selection, not guidance, delivers placement. NOT strictly
                  better than v6.5 (measured contact_logits 0.6281 vs
                  v6.5.21's 0.6719).

                  The guidance heads ARE supported here — same v6.5 penalty
                  kernels and strength calibration, routed through the normal
                  batch-spawn path rather than the streaming guided pool
                  v6.5/v6.5.21 use. ``immuno_strength`` /
                  ``stability_strength`` saturate at their ceilings (1.5 /
                  2.0); ``residue_composition_strength`` and
                  ``cellfree_strength`` are clamped at 4.0.

                  ⛔ READ THE DECOMPOSITION BEFORE QUOTING A NUMBER. The
                  measured drop against an all-knobs-off control at
                  ``residue_composition_strength=4`` is -26.7% between-receptor
                  JSD, and it is TWO effects, not one:

                  * -17.7 percentage points is a FIXED cysteine-kernel cost
                    (95% CI [-32.5%, -3.7%], significant). It fires whenever
                    ANY guidance head is enabled at ANY strength, including 0.
                    It is not this knob's dose effect and does not scale with
                    it.
                  * The knob's OWN dose share at strength 4 is -11.5%, 95% CI
                    [-27.2%, +4.7%] — NOT statistically significant. It first
                    reaches significance at strength 8 (-22.3%, CI [-38.3%,
                    -6.4%]), which is why the ceiling is 4.0.

                  Any statement that this knob costs ~28% JSD at strength 4 is
                  the two effects added together and attributed to one of them.
                  Do not propagate it.

                  ``serum_stability``/proteolytic and
                  ``residue_composition`` are ONE coupled axis, not independent
                  dials. ``cellfree`` lowers LOCAL HYDROPHOBICITY (rolling
                  7-residue GRAVY window), NOT K/R clusters.
                * ``"v6.5"`` — Legacy, deprecated. Trained with all-zero
                  receptor features, so its output does not depend on the
                  receptor you select. Retained only to reproduce runs made
                  before August 2026.

                Combining ``model=`` with a guidance argument raises
                :class:`~ligandai.errors.LigandAIInvalidConfig` before any HTTP
                call ONLY when the resolved model is known not to support
                guidance, naming the conflicting arguments — they are never
                dropped silently, because a design generated without the
                guidance you asked for is not the design you asked for. No
                model shipped with this SDK build currently triggers it.

                Ids this SDK build does not recognize are forwarded as-is; the
                server's model registry is the authority on which ids exist.
            immunogenicity: Enable immune guidance (academia+ tier). Steers
                generation away from MHC-binding motifs toward sequences with low
                predicted immunogenicity. Uses MHC-I/II anchor avoidance + scoring
                during diffusion.
            immuno_strength: Immune guidance strength. **Default 2.0, minimum
                0.0, server ceiling 1.5.** Values above 1.5 are clamped by the
                generation worker and logged; the SDK warns rather than
                rewriting your number. The ceiling is the composition guard
                (L1-distance-to-natural, MAX_L1 = 45): clean on all 5 seeds at
                1.5, trips at 1.75. The objective is still improving at the
                ceiling — 1.5 is where the damage budget runs out, not where
                the knob stops working.
            immuno_modules: Optional per-module override, e.g.
                ``{"mhc_i": True, "mhc_ii": True}``.
            serum_stability: Enable proteolytic-stability guidance (academia+
                tier). Suppresses protease-recognition motifs to resist
                serum/lysosomal cleavage. Use ``stability_mode="target"`` to
                flip this for prodrug designs. Coupled with
                ``residue_composition`` — each measurably moves the other's
                metric, they are not independent.
            stability_strength: Proteolytic guidance strength. **Default 2.0,
                minimum 0.0, server ceiling 2.0.** 2.0 is both the measured
                optimum and the clamp: the cleavage metric REVERSES above it
                (2.033 off → 1.535 at 2.0 → 1.625 at 4.0) while L1-to-natural
                runs 40 → 70 → 95 and yield RISES to 100%, so a yield-based
                guard never sees the damage (bd-qmhck).
            stability_mode: ``"resist"`` (avoid cleavage) or ``"target"`` (prodrug).
            stability_modules: Optional dict enabling specific protease modules,
                e.g. ``{"trypsin": True, "dppiv": True}``.
            residue_composition: Residue & terminal composition target —
                ``"extended"`` (kernel at full strength) or ``"moderate"``
                (0.4x strength). ``None`` (default) disables the head.

                ⛔ THIS IS NOT A HALF-LIFE CONTROL. It was called ``halflife``
                through v0.11.x and the name was wrong. The deployed kernel
                shapes SEQUENCE-LEVEL COMPOSITION and nothing else:

                * tryptophan load — an escalating penalty (1.0x / 2.5x / 4.0x
                  as the running Trp count reaches 0 / 1 / 2),
                * basic fraction — an R/K/H penalty that steps up as the
                  fraction of basic residues crosses 0.15 and 0.25,
                * tyrosine content — a reward when Tyr is under-represented,
                * terminal residues — rewards for P/Q at the N-terminus and
                  P/D at the C-terminus, under a proline reward cap.

                It contains NO N-end-rule term, NO PEST term, and consults no
                measured half-life data. It does not predict a half-life and
                nothing downstream treats its output as one. On the standard
                N-end (Varshavsky) metric it is MONOTONE ADVERSE rather than
                inert: +0.0% / +0.3% / +2.1% / +4.1% at strength 1/2/4/8 and
                +13.6% at 16 (z=+5.32, significant) — the wrong direction.

                The N-end rule IS implemented on this platform, inside the
                PROTEOLYTIC head: ``serum_stability`` strength 0.5→2.0 moves
                that metric -52%. Reach for ``serum_stability``, never this
                knob, when N-end-rule stability is the goal.

                ``"rapid"`` is accepted for backward compatibility but is a
                NO-OP — the worker returns a zero penalty tensor for any target
                outside {"extended", "moderate"}. Coupled with
                ``serum_stability``/proteolytic; not an independent dial.
            residue_composition_strength: Strength for
                ``residue_composition``. **Default 2.0, minimum 0.0, server
                ceiling 4.0; unsupported above 2.0.**

                The ceiling is where the dose effect on between-receptor JSD
                stops being indistinguishable from zero: -11.5% at strength 4
                (95% CI [-27.2%, +4.7%], NOT significant), reaching
                significance at strength 8 (-22.3%, CI [-38.3%, -6.4%]). At
                8.0, 61% of generated peptides start with the same two
                residues; at 16.0 that rises to 93%.

                ⛔ The -17.7 percentage points of JSD loss attributed to the
                CYSTEINE KERNEL (95% CI [-32.5%, -3.7%]) are NOT part of this
                knob's dose effect. That cost is fixed, fires whenever any
                guidance head is enabled at any strength including 0, and is
                controlled by ``cysteine_enabled`` / ``cysteine_mode``. Adding
                the two together and billing the sum to this knob is where the
                retired "~28% at strength 4" figure came from.
            halflife: ⚠ **DEPRECATED** — the old name for
                ``residue_composition``. Still accepted so existing callers do
                not break; emits a :class:`DeprecationWarning`. If both
                spellings are supplied and they disagree, the NEW name wins and
                the warning says so.
            halflife_strength: ⚠ **DEPRECATED** — the old name for
                ``residue_composition_strength``. Same acceptance and
                precedence rules as ``halflife``.
            cellfree: Enable cell-free expressability guidance. Lowers LOCAL
                HYDROPHOBICITY across the sequence (rolling 7-residue GRAVY
                window, measured 0.001 → -0.576; windows above the kernel's
                -0.3 trigger drop 32.5%) to improve cell-free expression
                yield and handling. It does NOT penalize K/R clusters —
                deployed measurement shows K/R content RISES +20.4% and
                local K/R cluster density (window 5) RISES +32.2% under this
                guidance. Net charge shifts -14.3% as a SIDE EFFECT of the
                hydrophobicity term, not the objective — this is NOT a
                net-charge target. Free — measured zero entropy/quality cost.
            cellfree_strength: Strength for ``cellfree``. **Default 1.5 here,
                minimum 0.0, server ceiling 4.0.**

                ⛔ KNOWN CROSS-LAYER DEFAULT MISMATCH, stated rather than
                silently reconciled. This SDK, the Node SDK, the website and the
                FastAPI layer all default to **1.5**; the generation worker's own
                signature defaults to **2.0**
                (``ligandforge_v6_5_guided_inference_unified.py:2118``). Because
                every client sends the key explicitly, the worker's 2.0 never
                applies to a client-originated run — so the two numbers disagree
                and one of them is wrong. Changing it alters what every default
                request does, so it is a platform decision rather than a
                unilateral SDK edit; flagged under
                bd-LIGANDAI_ALPHA_V2-b4f34. Pass the value explicitly if the
                distinction matters to your run.

                ⚠ The 4.0 ceiling is a JUDGEMENT CALL, not a measurement. The
                kernel was measured to SATURATE above strength 4 (bd-b8k08) —
                i.e. higher values buy nothing, not that they were shown to do
                harm. The 0.5-8.0 composition sweep was clean throughout (L1
                14.6-19.5, no collapse at any strength). Stated as a judgement
                so it is not later cited as a measured limit.
            charge_mode: Charge filter mode (academia+ tier). ``"lt"`` keeps peptides
                with net charge < ``charge_value``; ``"gt"`` keeps those above;
                ``"between"`` uses ``charge_min``/``charge_max`` bounds;
                ``"off"`` disables filtering (server default).
            charge_value: Threshold for ``charge_mode="lt"`` or ``"gt"``.
            charge_min: Lower bound for ``charge_mode="between"``.
            charge_max: Upper bound for ``charge_mode="between"``.
            min_solubility: Minimum GRAVY-based solubility score filter.
            cyclic_mode: Cyclization constraint — ``"disulfide"`` (terminal
                Cys-Cys, primary recombinant-shippable), ``"lactam"``
                (head-to-tail amide, prediction only), or
                ``"head_tail_contact"`` (soft bias). Requires
                academia/pro/enterprise tier.
            cyclic_strength: Soft-constraint strength for cyclic guidance.
            strict_recombinant: For ``cyclic_mode="disulfide"``, forbid internal
                Cys residues (required for Adaptyv synthesis path).
            dual_fold_viz: For ``cyclic_mode="lactam"``, also fold the
                Cys-wrapped (disulfide) variant for side-by-side comparison.
            folding_mode: Server folding mode override, e.g. ``"serial"`` or
                ``"parallel"``.
            fold_strategy: Server fold strategy override.
            folding_conformations: Conformation set to fold, e.g.
                ``"generation"`` or ``["generation", "apo"]``.
            max_folds_per_target: Explicit fold cap. Overrides ``top_n_fold``
                when both are provided.
            enable_expansion: Enable server-side conformation/peptide expansion.
            auto_conformation_expansion: Let the server expand conformations
                automatically.
            clash_resolution_enabled: Enable server-side clash resolution before
                folding/scoring.
            md_relaxation_enabled: Enable MD relaxation when server-supported.
            num_trajectories: Diffusion samples / trajectories per folded
                peptide. ReceptorDB defaults to 4; set 1 to reduce runtime.
            local: **SUPERADMIN ONLY.** Route this run's GPU work onto Ligandal's
                own cards (delta, via the gpusched broker) instead of Modal.
                Changes three things at once and is gated for all three: the
                cards are a shared finite tenancy, the sequences are computed on
                a multi-tenant box rather than an isolated Modal container, and
                the work is not metered as Modal spend. A key that is not a
                platform superadmin gets **HTTP 403**, never a quiet fall back to
                Modal — being told your work ran locally when it did not would
                mean a different bill and a different confidentiality posture
                with nothing in the response saying so. Left at ``False`` the
                field is not sent at all and the request is byte-identical to
                one from a caller who never heard of it.
            fold_engines: Opt-in ENSEMBLE auto-fold. ``None`` (default) or
                ``["boltz2"]`` keeps the legacy single-Boltz-2 Phase-2 auto-fold
                unchanged. Provide 1-5 of ``esmfold2``/``boltz2``/``protenix``/
                ``openfold3``/``opendde`` (a list with >1 engine, or any engine
                other than boltz2) to fold each top-N peptide × conformation on
                every listed engine. ``opendde`` is opt-in only and never a
                default. Each engine **bills separately** (free-pays-credits — any
                tier with a positive credit balance may run it; a zero-balance
                caller is rejected the same way the single path is), mirroring
                :meth:`cofold`.
            fold_per_engine: NON-MSA per-engine setting overrides for the
                ensemble auto-fold, keyed by engine. **MSA keys are rejected** —
                MSA is a single shared control (``fold_shared_msa``), never
                per-engine. Ignored unless ``fold_engines`` resolves to a real
                ensemble.
            fold_shared_msa: ONE shared-MSA control for the ensemble auto-fold.
                A mode string (``"auto"`` = compute/look up OUR-OWN MSA once on
                the receptor chains and hand the same MSA to every engine, the
                default; ``"none"`` = single-sequence for every engine) or a dict
                ``{"mode": "auto"|"none"}``. NEVER an external/public MSA server.
            fold_cascade: Opt-in CASCADE auto-fold. ``False`` (default) keeps the
                ensemble/legacy behaviour. When ``True``, the stratified top-N
                peptides are folded on **Boltz-2 first**; the Boltz-2 winners (by
                ``cascade_gate_metric``) then run the OTHER selected engines
                (``fold_engines`` MINUS ``boltz2``). Boltz-2 is reused from stage 1,
                NEVER re-folded — so it is charged once (stage 1) and only the
                winners incur the additional ensemble cost. Requires ``boltz2`` in
                ``fold_engines`` (it is the stage-1 gate); raises ``ValueError``
                otherwise.
            cascade_gate_metric: Winner-selection metric — ``"ipsae"`` (default)
                or ``"iptm"``. Applied to each peptide's stage-1 Boltz-2 fold.
            cascade_gate_threshold: Winners are stage-1 Boltz-2 folds with the gate
                metric ``>=`` this value (default ``0.67``). ``None`` disables the
                threshold filter (use ``cascade_gate_top_n`` alone).
            cascade_gate_top_n: When set, take the top-N by gate metric among the
                threshold-passers. If the threshold filter yields ZERO winners but
                ``cascade_gate_top_n`` is set, fall back to the top-N by metric
                IGNORING the threshold (so an ensemble always runs on the best N).
        """
        # Resolve camelCase aliases — snake_case wins when both are supplied.
        fold_engines = fold_engines if fold_engines is not None else foldEngines
        fold_per_engine = fold_per_engine if fold_per_engine is not None else foldPerEngine
        fold_shared_msa = fold_shared_msa if fold_shared_msa is not None else foldSharedMsa
        # Cascade aliases — snake wins. ``fold_cascade`` defaults to False (not
        # None), so only override from the camelCase alias when the snake value
        # was left at its default AND a camelCase value was supplied.
        if not fold_cascade and foldCascade is not None:
            fold_cascade = bool(foldCascade)
        if cascade_gate_metric == "ipsae" and cascadeGateMetric is not None:
            cascade_gate_metric = cascadeGateMetric
        if cascade_gate_threshold == 0.67 and cascadeGateThreshold is not None:
            cascade_gate_threshold = cascadeGateThreshold
        if cascade_gate_top_n is None and cascadeGateTopN is not None:
            cascade_gate_top_n = cascadeGateTopN
        # Resolve the residue-composition knob from its canonical and legacy
        # names BEFORE the tier gate, so a caller using the deprecated spelling
        # is gated identically to one using the new one.
        (
            residue_composition,
            resolved_rc_strength,
            rc_alias_used,
        ) = _resolve_residue_composition(
            residue_composition=residue_composition,
            residue_composition_strength=residue_composition_strength,
            halflife=halflife,
            halflife_strength=halflife_strength,
        )
        # Each warning is gated on its OWN head being enabled. immuno_strength
        # defaults to 2.0 against a 1.5 ceiling, so an ungated check would warn
        # on every call — including calls with immuno guidance switched off.
        _warn_strength_out_of_band(
            "immuno_strength", immuno_strength, head_enabled=immunogenicity
        )
        _warn_strength_out_of_band(
            "stability_strength", stability_strength, head_enabled=serum_stability
        )
        _warn_strength_out_of_band(
            "cellfree_strength", cellfree_strength, head_enabled=cellfree
        )
        if self._client is not None:
            self._client._require_feature("generate_peptides")
            if _requests_advanced_guidance(
                immunogenicity=immunogenicity,
                immuno_modules=immuno_modules,
                serum_stability=serum_stability,
                stability_modules=stability_modules,
                residue_composition=residue_composition,
                cellfree=cellfree,
                cyclic_mode=cyclic_mode,
                extra=extra,
            ):
                self._client._require_feature("advanced_guidance")
        # v0.2.0: cys/cyclic controls passed via extra={...} are deprecated;
        # use the typed kwargs above. Hard-rejected in v0.3.0.
        _warn_deprecated_cys_extra(extra)
        # ⛔ P0 REUSE GUARD (bd-dre-1u72b). Popped BEFORE the body is built so it
        # never reaches the wire — it is a client-side policy switch, not a
        # server field. See _guard_reused_session() for what it protects against.
        _allow_reused_session = bool(extra.pop("allow_reused_session", False))
        _reuse_guidance_requested = _guidance_was_requested({
            "immunogenicity": immunogenicity,
            "serum_stability": serum_stability,
            "cellfree": cellfree,
            "residue_composition": residue_composition,
            "halflife": halflife,
            "cysteine_enabled": cysteine_enabled,
        })
        effective_segment_config = _resolve_segment_config(segments, segment_config)
        # An AvoidancePolicy is a convenience wrapper over the two avoidance
        # kwargs plus the off-target/hard-negative bookkeeping the campaign loop
        # needs. Explicit kwargs WIN — a caller who passed both meant the
        # explicit one, and silently preferring the policy would be the kind of
        # invisible override this SDK refuses to ship.
        avoid_residues, avoid_shell_angstrom = _merge_avoidance(
            avoidance, avoid_residues, avoid_shell_angstrom
        )
        body = _generation_body(
            gene=gene,
            num_peptides=num_peptides,
            length_range=length_range,
            target_residues=target_residues,
            avoid_residues=avoid_residues,
            hotspot_residues=hotspot_residues,
            avoid_shell_angstrom=avoid_shell_angstrom,
            target_chains=target_chains,
            fold_partners=fold_partners,
            targeting_strategy=targeting_strategy,
            target_face=target_face,
            pocket_expansion_radius_a=pocket_expansion_radius_a,
            receptor_flexible_residues=receptor_flexible_residues,
            auto_fold=auto_fold,
            top_n_fold=top_n_fold,
            ec_domain_trimming=ec_domain_trimming,
            deimmunize_mode=deimmunize_mode,
            variant_id=variant_id,
            user_pdb_id=user_pdb_id,
            gen_gpus=gen_gpus,
            fold_gpus=fold_gpus,
            program_id=program_id,
            conversation_id=conversation_id,
            cysteine_mode=cysteine_mode,
            cysteine_enabled=cysteine_enabled,
            quality_guided=quality_guided,
            quality_guidance_scale=quality_guidance_scale,
            model=model,
            immunogenicity=immunogenicity,
            immuno_strength=immuno_strength,
            immuno_modules=immuno_modules,
            serum_stability=serum_stability,
            stability_strength=stability_strength,
            stability_mode=stability_mode,
            stability_modules=stability_modules,
            residue_composition=residue_composition,
            residue_composition_strength=resolved_rc_strength,
            residue_composition_alias_used=rc_alias_used,
            cellfree=cellfree,
            cellfree_strength=cellfree_strength,
            charge_mode=charge_mode,
            charge_value=charge_value,
            charge_min=charge_min,
            charge_max=charge_max,
            min_solubility=min_solubility,
            cyclic_mode=cyclic_mode,
            cyclic_strength=cyclic_strength,
            strict_recombinant=strict_recombinant,
            dual_fold_viz=dual_fold_viz,
            folding_mode=folding_mode,
            fold_strategy=fold_strategy,
            folding_conformations=folding_conformations,
            max_folds_per_target=max_folds_per_target,
            enable_expansion=enable_expansion,
            auto_conformation_expansion=auto_conformation_expansion,
            clash_resolution_enabled=clash_resolution_enabled,
            md_relaxation_enabled=md_relaxation_enabled,
            num_trajectories=num_trajectories,
            sampling_steps=sampling_steps,
            glycosylation_enabled=glycosylation_enabled,
            segment_config=effective_segment_config,
            pdc_config=pdc_config,
            ec_trimming_config=ec_trimming_config,
            fold_engines=fold_engines,
            fold_per_engine=fold_per_engine,
            fold_shared_msa=fold_shared_msa,
            fold_cascade=fold_cascade,
            cascade_gate_metric=cascade_gate_metric,
            cascade_gate_threshold=cascade_gate_threshold,
            cascade_gate_top_n=cascade_gate_top_n,
            contrastive=contrastive,
            allow_unforwarded_contrastive=allow_unforwarded_contrastive,
            avoid_paralogs=avoid_paralogs,
            local=local,
            extra=extra,
        )
        payload = self._transport.request("POST", "/api/ptf/parallel/generate", json=body) or {}
        _guard_reused_session(
            payload,
            requested_model=model,
            guidance_requested=_reuse_guidance_requested,
            allow_reused_session=_allow_reused_session,
        )
        job_id = payload.get("sessionId") or payload.get("jobId") or payload.get("session_id") or ""
        if not job_id:
            raise LigandAIError(
                "Server did not return a session_id/jobId for generation",
                response=payload,
            )
        return Job(
            self._transport,
            job_id,
            job_type="generation",
            parser=_parse_generation,
            status_path="/api/ptf/parallel/{job_id}/status",
            cancel_path="/api/ptf/parallel/{job_id}/cancel",
            sse_path="/api/ptf/parallel/{job_id}/stream",
            initial={"id": job_id, "type": "generation", "status": "queued", **payload},
            result_loader=lambda info: _load_generation_result(
                self._transport,
                info,
                fallback_session_id=job_id,
                fallback_gene=gene,
            ),
        )

    def generate_multi(
        self,
        targets: Iterable[_MultiTargetSpec],
        num_peptides: int | None = None,
        *,
        avoid_paralogs: _StrSequence[str] | None = None,
        **kwargs: Any,
    ) -> Job[GenerationResult]:
        """Generate against **N receptors in one concurrent run**.

        One request, one session, one bill, N targets generated concurrently by
        the platform orchestrator. Every non-targeting setting — generator
        ``model``, guidance heads, ``length_range``, ``num_peptides``, fold
        settings — is SESSION-level and applies to every target, because that is
        how ``POST /api/ptf/parallel/generate`` reads them (it destructures
        ``generatorModelId`` and the guidance flags once, off the request root,
        never off an individual target). Per-target you may vary only the
        TARGETING: see :data:`_PER_TARGET_KEYS`.

        Args:
            targets: Gene symbols and/or per-target mappings. A bare string is a
                whole-surface target; a mapping may carry ``gene``,
                ``target_residues``, ``avoid_residues``, ``hotspot_residues``,
                ``target_chains``, ``targeting_strategy``, ``variant_id``,
                ``user_pdb_id``, ``uniprot_id``, ``pdb_id``, ``pdb_data``.
            num_peptides: Peptides **per target** (not the total). The bill and
                the volume discount are computed on ``len(targets) *
                num_peptides`` — see :func:`estimate_multi_cost`.
            avoid_paralogs: Name the off-target genes to steer every target's
                generation away from, session-wide — see :meth:`generate` for
                the full contract (CFG-capable model required, mutually
                exclusive with ``contrastive=``).
            **kwargs: Every other :meth:`generate` keyword, applied session-wide.

        Returns:
            The same waitable :class:`~ligandai.jobs.Job` :meth:`generate`
            returns. ``job.wait()`` resolves once EVERY target is done;
            ``job.target_status()`` reports per-target progress while it runs and
            ``job.peptides_by_target()`` splits the finished peptides by gene.

        Raises:
            LigandAIError: empty target list, a blank or duplicated gene, or a
                session-level setting passed inside a per-target mapping.
            LigandAIReusedSessionError: the platform answered with a PRE-EXISTING
                session rather than running this request (see that class — its
                reuse key ignores the generator model and every guidance flag).

        Example::

            job = client.peptides.generate_multi(
                targets=["CD274", "EGFR", {"gene": "ERBB2", "hotspot_residues": [310]}],
                num_peptides=100,
                model="v6.9.22",
                immunogenicity=True,
            )
            for gene, state in job.target_status().items():
                print(gene, state["status"], state["peptidesGenerated"])
            job.wait()
            by_gene = job.peptides_by_target()

        ⛔ TIER CAPS ARE THE SERVER'S: free 3 simultaneous targets, basic /
        academia / pro 5, enterprise / discovery_partner 25 — enforced in the
        handler, which 400s with ``TIER_TARGET_LIMIT_EXCEEDED``. This method does
        not keep a copy of that table, so a tier change reaches you without an
        SDK upgrade.
        """
        wire_targets = _normalize_multi_targets(targets)
        # The first gene seeds generate()'s single-target scaffolding (session
        # fallback gene, targeting auto-strategy). `targets=` then overrides the
        # one-element array _generation_body built, because **extra is merged
        # into the body LAST. Nothing about the single-target path changes.
        first_gene = str(wire_targets[0]["gene"])
        kwargs.pop("gene", None)
        job = self.generate(
            gene=first_gene,
            num_peptides=num_peptides,
            targets=wire_targets,
            avoid_paralogs=avoid_paralogs,
            **kwargs,
        )
        _attach_multi_target_helpers(job, [str(t["gene"]) for t in wire_targets])
        return job


    def reattach(self, job_id: str) -> Job[GenerationResult]:
        """Re-create a waitable :class:`~ligandai.jobs.Job` for an in-flight or
        completed **parallel generation** session from its id alone.

        Use this to resume polling after a disconnect when all you kept is the
        ``session_id`` / ``job_id`` that :meth:`generate` returned (e.g. the
        process that launched the run exited). The returned handle polls
        ``GET /api/ptf/parallel/{job_id}/status`` and resolves to a
        :class:`~ligandai.types.GenerationResult` via ``.wait()`` / ``.results``
        exactly like the handle :meth:`generate` returns::

            job = client.peptides.generate(gene="CD274", num_peptides=300)
            session_id = job.id          # persist this
            # ... later, in a fresh process / after a disconnect ...
            result = client.peptides.reattach(session_id).wait()

        Unlike :meth:`Jobs.get`, which returns a one-shot
        :class:`~ligandai.types.JobInfo` snapshot AND only knows the generic
        ``/api/jobs/{id}`` lookup (which 404s on parallel-gen session ids),
        this returns a *waitable* Job bound to the parallel-generation status
        path.

        Args:
            job_id: The parallel-generation session id (``session_parallel_*``),
                or any id accepted by ``/api/ptf/parallel/{id}/status``.
        """
        if not job_id:
            raise LigandAIError("reattach(job_id) requires a non-empty job_id")
        return Job(
            self._transport,
            job_id,
            job_type="generation",
            parser=_parse_generation,
            status_path="/api/ptf/parallel/{job_id}/status",
            cancel_path="/api/ptf/parallel/{job_id}/cancel",
            sse_path="/api/ptf/parallel/{job_id}/stream",
            initial={"id": job_id, "type": "generation", "status": "queued"},
            result_loader=lambda info: _load_generation_result(
                self._transport,
                info,
                fallback_session_id=job_id,
                fallback_gene=None,
            ),
        )

    def fold(
        self,
        sequences: list[Sequence | str | dict[str, Any]],
        target_gene: str | None = None,
        auto_score: bool = True,
        template_mode: bool = False,
        msa_enabled: bool | None = None,
        glycosylation: bool | None = None,
        pegylation: bool | None = None,
        gpu_count: int = 1,
        diffusion_samples: int = 1,
        sampling_steps: int | None = None,
        recycling_steps: int | None = None,
        num_trajectories: int | None = None,
        step_scale: float | None = None,
        contribute_to_receptordb: bool | None = None,
        n_parallel_gpus: int | None = None,
        # Hardening kwargs
        gpu: str | None = None,
        force_resubmit: bool = False,
        # ESMFold2 + approach selection
        fold_approach: str | None = None,
        num_seeds: int | None = None,
        num_recycles: int | None = None,
        return_pdb: bool | None = None,
        use_potentials: bool | None = True,
    ) -> Job[FoldResult]:
        """Submit a folding job (monomer or multimer).

        ``fold_approach`` (default ``"boltz2_affinity"``) selects the upstream
        folding method:

          - ``"boltz2_affinity"`` — 2-chain Boltz-2 with affinity
          - ``"esmfold2"`` — single-sequence ESMFold2 on B200+ (~3-5 s/peptide)
          - ``"esmfold2_fast"`` — fast warm-pool variant (~50 ms)

        ``num_seeds`` / ``num_recycles`` / ``return_pdb`` are forwarded to the
        folding method; ``num_recycles`` only affects ESMFold approaches.

        Args:
            n_parallel_gpus: Concurrent fold-worker GPUs. When None (default),
                the platform uses the tier-default (free=1, basic=4,
                academia=16, pro=25, enterprise=50). When set above the
                caller's tier cap, the platform returns HTTP 400 with the cap
                value in the response body. Use estimate_fold_time() to
                pre-compute the wall-clock impact.
            gpu: GPU type. Only ``"b200_plus"`` is accepted; other GPU strings
                raise :class:`~ligandai.errors.LigandAIInvalidConfig` BEFORE
                any HTTP call. Distinct from ``gpu_count`` (slot count, not type).
            force_resubmit: Bypass the local 24-hour dedupe and re-submit an
                identical fold call. Default ``False`` — identical fold calls
                return the cached :class:`~ligandai.jobs.Job` handle.
            use_potentials: Opt into Boltz-2 physics-based steering potentials
                (boltz ``--use_potentials``; inference-time FK steering + physical
                guidance during diffusion). Default ``None``/``False`` = off,
                reproducing current behavior. May improve physical validity of the
                predicted complex at a modest speed cost. Boltz-2 path only —
                ignored by ESMFold approaches.

        ⛔ FLAG YOUR DESIGNED CHAINS. A ``protein`` chain with no peptide flag is
        folded as a RECEPTOR, and a receptor is entitled to a multiple-sequence
        alignment. A LigandForge design — or any de novo binder — has no
        homologs, so that MSA search can only come back degenerate, and when the
        MSA service cannot answer the fold is REFUSED for an alignment that
        cannot exist. Mark every designed chain with ``is_peptide=True`` (or
        ``use_msa=False``, or ``role="peptide"`` — any one is enough); the
        receptor chain keeps its MSA::

            job = client.peptides.fold(
                sequences=[
                    {"chainId": "A", "sequence": BRD4_ET_DOMAIN, "geneName": "BRD4"},
                    {"chainId": "B", "sequence": DESIGNED_BINDER, "is_peptide": True},
                ],
                target_gene="BRD4",
                diffusion_samples=4,
            )

        Chains may be plain strings, dicts as above, or
        :class:`~ligandai.types.Sequence` objects
        (``Sequence(sequence=..., is_peptide=True)``). For the common two-chain
        case ``client.fold(receptor_seq, binder_seq)`` flags chain B for you.
        """
        if self._client is not None:
            self._client._require_feature("predict_structure")
        # SDK hardening pre-flight
        from ligandai._hardening import (
            attach_job_id,
            build_fold_params_for_hash,
            dedupe_lookup_cached,
            enforce_concurrency,
            estimate_single_fold_credits,
            mark_failed,
            preflight_credits,
            receptor_seq_for_hash,
            record_submission,
            validate_gpu,
        )
        from ligandai._dedupe import compute_submission_hash

        canonical_gpu = validate_gpu(gpu)
        submitted_set = self._client.submitted_set if self._client is not None else None
        credit_ledger = self._client.credit_ledger if self._client is not None else None
        api_key_hash = self._client.api_key_hash if self._client is not None else ""

        # Identity = concatenation of normalized sequences. For a 2-chain
        # complex (receptor + peptide), this captures both. The first sequence
        # is the receptor when len(sequences) >= 2.
        normalized_seqs = [_norm_seq(s) for s in sequences]
        seq_strings = [str(s.get("sequence", "")) for s in normalized_seqs]
        if len(seq_strings) >= 2:
            rec_for_hash = receptor_seq_for_hash(
                target_gene=target_gene,
                receptor_sequence=seq_strings[0],
                receptor_pdb=None,
            )
            peptide_for_hash: str | list[str] = "|".join(seq_strings[1:])
        else:
            rec_for_hash = receptor_seq_for_hash(
                target_gene=target_gene,
                receptor_sequence=seq_strings[0] if seq_strings else None,
                receptor_pdb=None,
            )
            peptide_for_hash = ""

        sub_hash = compute_submission_hash(
            peptide_seq=peptide_for_hash,
            receptor_seq=rec_for_hash,
            gpu=canonical_gpu,
            params=build_fold_params_for_hash(
                target_gene=target_gene,
                diffusion_samples=(
                    num_trajectories if num_trajectories is not None
                    else diffusion_samples
                ),
                sampling_steps=sampling_steps,
                recycling_steps=recycling_steps,
                step_scale=step_scale,
                msa_enabled=msa_enabled,
                glycosylation=glycosylation,
                template_mode=template_mode,
                extra={
                    "kind": "fold",
                    "pegylation": bool(pegylation) if pegylation is not None else None,
                    # Only include the potentials key when ON, so a default (off)
                    # fold hashes identically to pre-feature submissions (dedupe-stable).
                    **({"use_potentials": bool(use_potentials)} if use_potentials is not None else {}),
                },
            ),
        )

        enforce_concurrency(self._client, submitted_set)

        cached = dedupe_lookup_cached(
            submitted_set,
            submission_hash=sub_hash,
            api_key_hash=api_key_hash,
            force_resubmit=force_resubmit,
        )
        if cached and cached.get("job_id"):
            cached_job_id = str(cached["job_id"])
            return Job(
                self._transport,
                cached_job_id,
                job_type="folding",
                parser=_parse_fold,
                status_path="/api/folding/jobs/{job_id}",
                cancel_path="/api/folding/jobs/{job_id}",
                sse_path="/api/folding/jobs/{job_id}/logs/stream",
                initial={"id": cached_job_id, "type": "folding", "status": "cached"},
            )

        # Single-fold cost (local pre-flight estimate; the platform is
        # authoritative): ligandai.pricing.fold_credits — 10 credits for a
        # default 4-trajectory fold, proportional per trajectory, ceil(steps/50).
        effective_trajectories = int(
            num_trajectories if num_trajectories is not None else diffusion_samples
        )
        estimated = estimate_single_fold_credits(
            trajectories=effective_trajectories,
            sampling_steps=sampling_steps,
        )
        balance_before, _ = preflight_credits(
            self._client, estimated=estimated, kind="fold",
        )

        body = _fold_body(
            sequences,
            auto_score=auto_score,
            template_mode=template_mode,
            msa_enabled=msa_enabled,
            target_gene=target_gene,
            glycosylation=glycosylation,
            pegylation=pegylation,
            gpu_count=gpu_count,
            diffusion_samples=diffusion_samples,
            sampling_steps=sampling_steps,
            recycling_steps=recycling_steps,
            num_trajectories=num_trajectories,
            step_scale=step_scale,
            contribute_to_receptordb=contribute_to_receptordb,
            n_parallel_gpus=n_parallel_gpus,
            fold_approach=fold_approach,
            num_seeds=num_seeds,
            num_recycles=num_recycles,
            return_pdb=return_pdb,
            use_potentials=use_potentials,
        )

        record_submission(
            submitted_set,
            credit_ledger,
            submission_hash=sub_hash,
            api_key_hash=api_key_hash,
            kind="fold",
            gpu=canonical_gpu,
            estimated_credits=estimated,
            balance_before=balance_before,
            meta={"target_gene": target_gene, "chains": len(seq_strings)},
        )

        try:
            payload = self._transport.request("POST", "/api/folding/predict", json=body) or {}
        except Exception as exc:
            mark_failed(
                submitted_set, submission_hash=sub_hash,
                api_key_hash=api_key_hash, reason=type(exc).__name__,
            )
            raise

        job_id = payload.get("jobId") or payload.get("id") or ""
        if not job_id:
            mark_failed(
                submitted_set, submission_hash=sub_hash,
                api_key_hash=api_key_hash, reason="server_no_job_id",
            )
            raise LigandAIError("Server did not return a jobId for fold", response=payload)
        attach_job_id(
            submitted_set, submission_hash=sub_hash,
            api_key_hash=api_key_hash, job_id=job_id,
        )
        return Job(
            self._transport,
            job_id,
            job_type="folding",
            parser=_parse_fold,
            status_path="/api/folding/jobs/{job_id}",
            cancel_path="/api/folding/jobs/{job_id}",
            sse_path="/api/folding/jobs/{job_id}/logs/stream",
            initial={"id": job_id, "type": "folding", "status": "queued", **payload},
        )

    def fold_batch(
        self,
        peptides: list[str] | None = None,
        *,
        groups: list[dict[str, Any]] | None = None,
        target_gene: str | None = None,
        receptor_pdb: str | None = None,
        receptor_sequence: str | None = None,
        receptor_name: str | None = None,
        # bd-dre-h0cuo.1 — arbitrary-seq MSA:
        #   msa_source_gene / parent_gene: derive a receptor_sequence's MSA from
        #     a homolog-rich parent gene's cached MSA (column-subset on substring
        #     match, align otherwise) instead of a fresh fragment search.
        #   gene_range: with target_gene=, fold a 1-based inclusive residue range
        #     (a domain) of the gene using the gene's MSA sliced to those columns.
        msa_source_gene: str | None = None,
        parent_gene: str | None = None,
        gene_range: tuple[int, int] | None = None,
        diffusion_samples: int = 1,
        sampling_steps: int = 50,
        recycling_steps: int | None = None,
        step_scale: float | None = None,
        msa_enabled: bool | None = None,
        glycosylation: bool | None = None,
        template_mode: bool = False,
        n_parallel_gpus: int | None = None,
        session_id: str | None = None,
        contribute_to_receptordb: bool | None = None,
        num_trajectories: int | None = None,
        msa_depth: int | None = None,
        use_potentials: bool | None = True,
        # [bd-LIGANDAI_ALPHA_V2-pybsy] Boltz-2 --seed. `boltz predict --seed N` is a
        # GLOBAL CLI option: one invocation per seed, never a per-row field. Omit for
        # the previous (unseeded) behaviour. To replicate, submit the SAME batch once
        # per seed and treat each seed's median over its diffusion samples as ONE
        # observation — never pool samples across seeds as independent replicates.
        seed: int | None = None,
        on_credit_exhausted: Callable[[LigandAICreditError, dict[str, Any]], bool] | None = None,
        # ─── hardening kwargs ────────────────────
        # gpu: only "b200_plus" is accepted by the SDK; anything else raises
        # LigandAIInvalidConfig BEFORE any HTTP call.
        gpu: str | None = None,
        # force_resubmit: bypass the local 24h dedupe and POST anyway.
        force_resubmit: bool = False,
    ) -> BatchFoldJob:
        """Submit a batch of Boltz-2 folds — either N peptides vs one receptor,
        or N grouped ``target{partners}`` complexes with per-entity MSA control.

        ``POST /api/v1/folding/predict-batch``

        Two mutually-exclusive input forms (pass exactly one):

        **1. Flat form (``peptides=`` + one receptor)** — backward compatible.
        Each peptide folds as a 2-chain complex (chain A = receptor, chain B =
        de novo peptide, no peptide MSA). See the receptor / FASTA notes below.

        **2. Grouped form (``groups=``)** — each group is one fold job with a
        ``target`` (a protein/receptor) and one or more co-folding ``partners``.
        MSA is settable independently at BOTH levels:

        - per **target/receptor** — a protein; ``use_msa`` defaults **True**;
        - per **partner** — a ``"peptide"`` (``use_msa`` default **False**) or a
          ``"protein"`` (``use_msa`` default **True**, may request its own MSA).

        Example (grouped)::

            job = client.peptides.fold_batch(groups=[
                {"target": {"gene": "EGFR", "use_msa": True},
                 "partners": [
                     {"sequence": "ACDEFGHIK", "type": "peptide"},        # no MSA
                     {"sequence": "MKLVAA...", "type": "protein", "use_msa": True},  # MSA
                 ]},
                {"target": {"sequence": "MEEPQS..."},
                 "partners": [{"sequence": "WYLKPRSTV", "type": "peptide"}]},
            ], diffusion_samples=4, use_potentials=True)
            results = job.wait()

        Each group becomes one fold job whose ``entities`` array is
        ``[target=chain A, partner1=chain B, partner2=chain C, ...]``. The
        per-entity ``use_msa`` / ``isPeptide`` flags route end-to-end: peptide /
        ``use_msa=False`` chains fold with ``msa: empty``; receptor /
        MSA-flagged protein partners get MSA passthrough (``--use_msa_server``).
        ⛔ A chain flagged peptide / ``use_msa=False`` is NEVER sent to the MSA
        service. (bd-LIGANDAI_ALPHA_V2-wymqb, relates 5319n.)

        ``POST /api/v1/folding/predict-batch``

        Each peptide is folded as a 2-chain complex (chain A = receptor,
        chain B = peptide). The full peptide list is folded in parallel
        subject to server-side concurrency limits (tier + GPU quota).

        Billing
        -------
        **10 credits ($0.10) per fold at the default 4 trajectories** —
        2.5 credits per trajectory, floored at a 10-credit minimum per fold
        (so a single-trajectory fold still costs 10). Sampling steps above 50
        multiply the cost by ``ceil(sampling_steps / 50)``. The figures live in
        :mod:`ligandai.pricing`; the platform is authoritative and the returned
        ``total_cost_credits`` is what is charged. Total cost is charged
        upfront before any GPU work begins; HTTP 402 is raised when balance
        is insufficient.

        Graceful credit-exhaustion (``on_credit_exhausted``)
        ----------------------------------------------------
        When the server rejects the batch with ``HTTP 402 INSUFFICIENT_CREDITS``,
        the SDK raises :class:`~ligandai.errors.LigandAICreditError`. The error
        instance carries the structured server response (``shortfall``,
        ``recovery_url``, ``top_up_usd``, ``upgrade_url``) so callers can
        prompt the user to top up.

        Pass ``on_credit_exhausted=my_callback`` to handle the error inline
        instead of having it bubble up. The callback receives ``(err, payload)``
        where ``err`` is the :class:`LigandAICreditError` and ``payload`` is
        the JSON body the SDK was about to POST. Return ``True`` to retry the
        submission (after the user tops up); return ``False`` (or anything
        falsy) to re-raise the original error. Example::

            def topup_prompt(err, payload):
                print(f"Need {err.shortfall:,} more credits (${err.top_up_usd}).")
                print(f"Top up at: {err.recovery_url}")
                input("Press Enter once topped up to resume...")
                return True

            job = client.peptides.fold_batch(
                peptides=peps, target_gene="EGFR",
                on_credit_exhausted=topup_prompt,
            )

        Because ``fold_batch`` is a single POST (the server dispatches all
        peptide sub-jobs atomically), there is no "in-flight batch" to pause
        — the entire request is rejected before any GPU work begins. The
        callback simply gives the caller a clean hook to top up and retry
        the same batch without restructuring their loop.

        Receptor (pass exactly one)
        ---------------------------
        ``target_gene``
            Gene symbol (e.g. ``"EGFR"``). Resolved server-side via the
            canonical predicted PDB if available, otherwise via the human
            proteome UniProt SQLite.
        ``receptor_pdb``
            Raw PDB content, base64-encoded PDB, or a local path to a .pdb
            file (the SDK reads the file once and forwards the content).
        ``receptor_sequence``
            Raw amino-acid sequence. The server attempts a UniProt match for
            attribution and falls back to the literal sequence labelled with
            ``receptor_name`` (or ``"custom_sequence"``).

        Peptide input
        -------------
        Each entry in ``peptides`` may be a bare AA string OR a FASTA block
        (string starting with ``>``). Multi-record FASTA is supported: every
        record produces one fold job. Duplicates are de-duplicated server-side.

        Examples
        --------
        Gene-based receptor::

            job = client.peptides.fold_batch(
                peptides=["ACDEFGHIK", "WYLKPRSTV", "MNPQRSTAV"],
                target_gene="EGFR",
                diffusion_samples=4,
            )
            result = job.wait()
            for fold in result:
                if fold is not None:
                    print(fold.iptm, fold.ipsae)

        FASTA input::

            with open("candidates.fasta") as fh:
                job = client.peptides.fold_batch(
                    peptides=[fh.read()],
                    target_gene="CD47",
                    diffusion_samples=4,
                )

        Custom PDB receptor (e.g. an Adaptyv-expressed mutant)::

            job = client.peptides.fold_batch(
                peptides=peptide_library,
                receptor_pdb="receptors/my_target.pdb",
                receptor_name="MY_TARGET_v2",
                sampling_steps=100,        # 2x billing multiplier
            )

        Returns
        -------
        :class:`BatchFoldJob`
            ``.batch_id``, ``.jobs`` (per-peptide job metadata),
            ``.total_cost_credits``, and ``.wait()`` for results.
        """
        if self._client is not None:
            self._client._require_feature("predict_structure")
        # ─── Form selection: flat (peptides=) XOR grouped (groups=) ──────────
        if (peptides is None) == (groups is None):
            raise ValueError(
                "Pass exactly one of peptides=[...] (flat: N peptides vs one "
                "receptor) or groups=[...] (grouped: target{partners} complexes "
                "with per-entity MSA control)."
            )
        if groups is not None:
            return self._fold_batch_grouped(
                groups=groups,
                diffusion_samples=diffusion_samples,
                sampling_steps=sampling_steps,
                recycling_steps=recycling_steps,
                step_scale=step_scale,
                glycosylation=glycosylation,
                template_mode=template_mode,
                n_parallel_gpus=n_parallel_gpus,
                session_id=session_id,
                contribute_to_receptordb=contribute_to_receptordb,
                num_trajectories=num_trajectories,
                msa_depth=msa_depth,
                use_potentials=use_potentials,
                seed=seed,
                on_credit_exhausted=on_credit_exhausted,
                gpu=gpu,
                force_resubmit=force_resubmit,
            )
        # ─── SDK hardening pre-flight ──────────────
        # 1) GPU type allowlist (raises LigandAIInvalidConfig if bad)
        # 2) Local concurrency cap (raises LigandAIConcurrencyLimit)
        # 3) Dedupe lookup (returns cached BatchFoldJob if recent match)
        # 4) Credit pre-flight (raises LigandAIInsufficientCredits)
        # 5) Record submission in submitted.db + credit_ledger.db
        # Network POST only happens AFTER all five guards pass.
        from ligandai._hardening import (
            attach_job_id,
            dedupe_lookup_cached,
            enforce_concurrency,
            estimate_fold_batch_credits,
            mark_failed,
            preflight_credits,
            receptor_seq_for_hash,
            record_submission,
            validate_gpu,
            build_fold_params_for_hash,
        )
        from ligandai._dedupe import compute_submission_hash

        canonical_gpu = validate_gpu(gpu)
        submitted_set = self._client.submitted_set if self._client is not None else None
        credit_ledger = self._client.credit_ledger if self._client is not None else None
        api_key_hash = self._client.api_key_hash if self._client is not None else ""

        # Compute submission hash from the SAME inputs that determine result
        # identity. Receptor identity uses sequence > pdb > gene.
        rec_for_hash = receptor_seq_for_hash(
            target_gene=target_gene,
            receptor_sequence=receptor_sequence,
            receptor_pdb=receptor_pdb,
        )
        sub_hash = compute_submission_hash(
            peptide_seq=peptides,
            receptor_seq=rec_for_hash,
            gpu=canonical_gpu,
            params=build_fold_params_for_hash(
                target_gene=target_gene,
                diffusion_samples=diffusion_samples,
                sampling_steps=sampling_steps,
                recycling_steps=recycling_steps,
                step_scale=step_scale,
                msa_enabled=msa_enabled,
                glycosylation=glycosylation,
                template_mode=template_mode,
                extra={
                    "kind": "fold_batch",
                    "receptor_name": receptor_name,
                    # Only present when ON so default batches hash unchanged.
                    **({"use_potentials": bool(use_potentials)} if use_potentials is not None else {}),
                    **({"msa_depth": int(msa_depth)} if msa_depth is not None else {}),
                },
            ),
        )

        # 1) Concurrency cap (only after passing GPU validation; raises if full)
        enforce_concurrency(self._client, submitted_set)

        # 2) Dedupe — return cached batch handle if a recent identical
        #    submission exists and force_resubmit was not requested.
        cached = dedupe_lookup_cached(
            submitted_set,
            submission_hash=sub_hash,
            api_key_hash=api_key_hash,
            force_resubmit=force_resubmit,
        )
        if cached and cached.get("job_id"):
            # Reconstruct a thin BatchFoldJob from the cached batch_id. We
            # cannot recover per-sub-job metadata, but the user can call
            # .wait() / .results via the server status endpoint.
            cached_batch_id = str(cached["job_id"])
            return BatchFoldJob(
                self._transport,
                batch_id=cached_batch_id,
                jobs=[],
                total_cost_credits=int(cached.get("estimated_credits") or 0),
                peptide_count=len(peptides),
                trajectories_per_peptide=int(diffusion_samples),
                receptor={"source": "cached"},
                sampling_steps=int(sampling_steps),
                from_cache=True,
            )

        # 3) Credit pre-flight — estimate locally, compare to balance, raise
        #    LigandAIInsufficientCredits if short (skipped for unlimited tiers).
        estimated = estimate_fold_batch_credits(
            peptide_count=len(peptides),
            trajectories=int(diffusion_samples),
            sampling_steps=int(sampling_steps),
        )
        balance_before, _ = preflight_credits(
            self._client, estimated=estimated, kind="fold_batch",
        )

        # parent_gene is an alias for msa_source_gene; reject conflicting values.
        _effective_msa_gene = msa_source_gene if msa_source_gene is not None else parent_gene
        if msa_source_gene is not None and parent_gene is not None and msa_source_gene != parent_gene:
            raise ValueError("Pass only one of msa_source_gene= or parent_gene= (they are aliases)")

        body = _build_batch_fold_body(
            peptides=peptides,
            target_gene=target_gene,
            receptor_pdb=receptor_pdb,
            receptor_sequence=receptor_sequence,
            receptor_name=receptor_name,
            diffusion_samples=diffusion_samples,
            sampling_steps=sampling_steps,
            recycling_steps=recycling_steps,
            step_scale=step_scale,
            msa_enabled=msa_enabled,
            glycosylation=glycosylation,
            template_mode=template_mode,
            n_parallel_gpus=n_parallel_gpus,
            session_id=session_id,
            contribute_to_receptordb=contribute_to_receptordb,
            num_trajectories=num_trajectories,
            msa_depth=msa_depth,
            use_potentials=use_potentials,
            msa_source_gene=_effective_msa_gene,
            gene_range=gene_range,
            seed=seed,
        )

        # 4) Record submission BEFORE the POST. If the POST fails we'll mark
        #    the row 'failed' so the next attempt isn't blocked by dedupe.
        record_submission(
            submitted_set,
            credit_ledger,
            submission_hash=sub_hash,
            api_key_hash=api_key_hash,
            kind="fold_batch",
            gpu=canonical_gpu,
            estimated_credits=estimated,
            balance_before=balance_before,
            meta={
                "peptide_count": len(peptides),
                "target_gene": target_gene,
                "receptor_name": receptor_name,
                "trajectories": int(diffusion_samples),
                "sampling_steps": int(sampling_steps),
            },
        )

        # retry-after-topup hook. The submission is a
        # single POST; on HTTP 402 INSUFFICIENT_CREDITS, the SDK invokes the
        # caller-supplied callback and, if it returns truthy, re-POSTs the
        # same body once. One retry is intentional — repeated retries on a
        # still-empty balance would just hammer the server. Callers wanting
        # bounded retry loops should drive that from outside fold_batch.
        try:
            payload = self._transport.request("POST", "/api/v1/folding/predict-batch", json=body) or {}
        except LigandAICreditError as cred_err:
            if on_credit_exhausted is None:
                mark_failed(
                    submitted_set, submission_hash=sub_hash,
                    api_key_hash=api_key_hash, reason="credit_exhausted",
                )
                raise
            try:
                should_retry = bool(on_credit_exhausted(cred_err, body))
            except Exception:
                # Never let a callback exception swallow the original credit
                # error — re-raise the credit error so the caller sees the
                # actionable shortfall/recovery_url metadata.
                mark_failed(
                    submitted_set, submission_hash=sub_hash,
                    api_key_hash=api_key_hash, reason="credit_exhausted_callback_error",
                )
                raise cred_err from None
            if not should_retry:
                mark_failed(
                    submitted_set, submission_hash=sub_hash,
                    api_key_hash=api_key_hash, reason="credit_exhausted_no_retry",
                )
                raise
            payload = self._transport.request("POST", "/api/v1/folding/predict-batch", json=body) or {}
        except Exception as exc:
            mark_failed(
                submitted_set, submission_hash=sub_hash,
                api_key_hash=api_key_hash, reason=type(exc).__name__,
            )
            raise
        batch_id = payload.get("batch_id") or payload.get("batchId") or ""
        if not batch_id:
            mark_failed(
                submitted_set, submission_hash=sub_hash,
                api_key_hash=api_key_hash, reason="server_no_batch_id",
            )
            raise LigandAIError("Server did not return a batch_id for fold_batch", response=payload)
        # 5) Persist the batch_id onto the dedupe row so a repeat call returns
        #    the cached handle.
        attach_job_id(
            submitted_set, submission_hash=sub_hash,
            api_key_hash=api_key_hash, job_id=batch_id,
        )
        return BatchFoldJob(
            self._transport,
            batch_id=batch_id,
            jobs=payload.get("jobs") or [],
            total_cost_credits=int(payload.get("total_cost_credits") or 0),
            peptide_count=int(payload.get("peptide_count") or 0),
            trajectories_per_peptide=int(payload.get("trajectories_per_peptide") or diffusion_samples),
            receptor=payload.get("receptor"),
            sampling_steps=int(payload.get("sampling_steps") or sampling_steps),
        )

    def cofold(
        self,
        chains: list[Sequence | str | dict[str, Any]],
        engines: list[str] = ["boltz2"],
        *,
        per_engine: dict[str, Any] | None = None,
        shared_msa: str | dict[str, Any] | None = None,
        run_deltaforge: bool = True,
        length_bin: str | None = None,
        gene: str | None = None,
        include_structures: bool = False,
        audit_id: str | None = None,
        # camelCase aliases (accepted; snake_case wins on conflict)
        perEngine: dict[str, Any] | None = None,
        sharedMsa: str | dict[str, Any] | None = None,
        runDeltaforge: bool | None = None,
        lengthBin: str | None = None,
        includeStructures: bool | None = None,
        auditId: str | None = None,
        # Cofactors the hosted route carries (server CofoldStartRequest.cofactors,
        # bd-dre-3jqok). Engines that cannot carry one make the server REFUSE the
        # request unless allow_cofactor_absent=True.
        cofactors: list[Any] | None = None,
        allow_cofactor_absent: bool = False,
    ) -> "Job[dict[str, Any]]":
        """Ensemble co-fold — fold the SAME chains on 1-5 engines in parallel.

        Each engine runs in its own single-B200 container; the result is a
        per-engine record (canonical scores best-of + ranges) plus an optional
        DeltaForge v10.2 score on each engine's best structure.

        **Engine notes:**

        ``boltz2`` (default) — Boltz-2, the platform default. Recommended for
        all peptide-binder tasks. In our 200-fold no-MSA benchmark (complexes
        from >= 2023-06-02), Boltz-2 outperforms OpenDDE on structure recovery
        (DockQ 0.353 vs 0.197) and binder discrimination (peptide iPSAE AUROC
        0.625 vs 0.556) at roughly one-third the GPU cost.

        ``opendde`` (opt-in) — AF3-lineage all-atom co-folding model from
        Aureka AI Research (656 M params, Apache-2.0, training-data cutoff
        2021-09-30). Emits a PAE matrix; peptide iPSAE IS computable. Does
        not predict Kd. Reported strength is antibody–antigen / MSA-driven
        co-folding, which we have not benchmarked. Never a default; never
        included in a default ensemble.

        Args:
            chains: 1-8 chains. Receptor chain(s) first, peptide last (a bare
                string or dict with no ``kind`` defaults to ``peptide`` when it
                is the last of a multi-chain payload, else ``receptor``).
            engines: 1-5 of ``esmfold2``, ``boltz2``, ``protenix``,
                ``openfold3``, ``opendde`` (opt-in; see engine notes below).
            per_engine: NON-MSA setting overrides keyed by engine. **MSA keys
                are rejected** — MSA is a single shared control (``shared_msa``),
                never per-engine.
            shared_msa: ONE shared-MSA control for the whole ensemble. Either a
                mode string (``"auto"`` = compute/look up OUR-OWN MSA once on the
                receptor chains and hand the same MSA to every engine, the
                default; ``"none"`` = single-sequence for every engine) or a dict
                ``{"mode": "auto"|"none"}``. NEVER an external/public MSA server.
            run_deltaforge: Score each engine's best structure with DeltaForge
                v10.2 (default ``True``).
            gene: Optional HGNC symbol for labelling + MSA lookup.
            include_structures: Include mmCIF/PDB text inline in the records
                (default ``False`` keeps poll payloads small).
            cofactors: Cofactors to co-fold with the complex — CCD codes
                (``["ZN", "ZN"]``), dicts (``{"ccd": "ZN", "copies": 2}`` or
                ``{"smiles": ...}``) or ``CofactorSpec``. Sent as the server's
                ``cofactors`` field. Engines that cannot carry one
                (``opendde``, ``promera``) make the server refuse the request
                unless ``allow_cofactor_absent=True``; each engine's record then
                says ``cofactor.honoured`` and an arm that dropped it is never
                pooled with arms that carried it.
            allow_cofactor_absent: run engines that cannot carry the cofactor
                anyway (their arms come back ``cofactor.honoured=False``).

        Returns:
            A :class:`~ligandai.jobs.Job` polling ``/api/v1/cofold/{job_id}/
            status``. ``job.wait()`` blocks until the job is terminal and
            returns the multi-engine result dict (``engines``, ``summary``,
            ``billing``); it rides out the intermittent 404 "job not found"
            the hosted job store answers between API workers (see
            ``not_found_grace``).

        Free-pays-credits — any tier (including free) may submit a co-fold as
        long as the account has a positive credit balance; the server reserves
        the per-engine credit estimate and rejects a zero-balance caller with
        HTTP 402. Refs: bd-LIGANDAI_ALPHA_V2-7wfnp
        """
        # Resolve camelCase aliases — snake_case wins when both are supplied.
        per_engine = per_engine if per_engine is not None else perEngine
        shared_msa = shared_msa if shared_msa is not None else sharedMsa
        if runDeltaforge is not None and run_deltaforge is True:
            run_deltaforge = runDeltaforge
        length_bin = length_bin if length_bin is not None else lengthBin
        if includeStructures is not None and include_structures is False:
            include_structures = includeStructures
        audit_id = audit_id if audit_id is not None else auditId

        # Folding (incl. ensemble co-fold) is free-pays-credits: free keys with a
        # positive credit balance may submit. The server enforces the balance
        # (gpuSubmitGuard) — no client-side tier gate here. Refs:
        # bd-LIGANDAI_ALPHA_V2-7wfnp

        norm_engines = _cofold_normalize_engines(engines)
        norm_chains = _cofold_chains(chains)
        clean_per_engine = _cofold_sanitize_per_engine(per_engine)

        # Normalize the shared-MSA control to {"mode": "auto"|"none"}.
        if shared_msa is None:
            shared_msa_spec: dict[str, Any] = {"mode": "auto"}
        elif isinstance(shared_msa, str):
            mode = shared_msa.strip().lower()
            if mode not in ("auto", "none"):
                raise ValueError("shared_msa must be 'auto' or 'none'")
            shared_msa_spec = {"mode": mode}
        elif isinstance(shared_msa, dict):
            mode = str(shared_msa.get("mode", "auto")).strip().lower()
            if mode not in ("auto", "none"):
                raise ValueError("shared_msa.mode must be 'auto' or 'none'")
            shared_msa_spec = {"mode": mode}
        else:
            raise TypeError("shared_msa must be a str ('auto'|'none') or a dict")

        body: dict[str, Any] = {
            "chains": norm_chains,
            "engines": norm_engines,
            "shared_msa": shared_msa_spec,
            "run_deltaforge": bool(run_deltaforge),
            "include_structures": bool(include_structures),
        }
        if clean_per_engine:
            body["per_engine"] = clean_per_engine
        if length_bin is not None:
            body["length_bin"] = length_bin
        if gene is not None:
            body["gene"] = gene
        if audit_id is not None:
            body["audit_id"] = audit_id
        if cofactors:
            body["cofactors"] = _cofold_cofactors(cofactors)
        if allow_cofactor_absent:
            body["allow_cofactor_absent"] = True

        payload = self._transport.request("POST", "/api/v1/cofold/start", json=body) or {}
        job_id = payload.get("job_id") or payload.get("jobId") or payload.get("id") or ""
        if not job_id:
            raise LigandAIError(
                "Server did not return a job_id for cofold", response=payload
            )
        return Job(
            self._transport,
            str(job_id),
            job_type="cofold",
            parser=_parse_cofold,
            status_path="/api/v1/cofold/{job_id}/status",
            cancel_path="/api/v1/cofold/{job_id}/cancel",
            sse_path="/api/v1/cofold/{job_id}/stream",
            initial={"id": str(job_id), "type": "cofold", "status": "queued", **payload},
        )

    def _fold_batch_grouped(
        self,
        *,
        groups: list[dict[str, Any]],
        diffusion_samples: int,
        sampling_steps: int,
        recycling_steps: int | None,
        step_scale: float | None,
        glycosylation: bool | None,
        template_mode: bool,
        n_parallel_gpus: int | None,
        session_id: str | None,
        contribute_to_receptordb: bool | None,
        num_trajectories: int | None,
        msa_depth: int | None,
        use_potentials: bool | None,
        on_credit_exhausted: Callable[[LigandAICreditError, dict[str, Any]], bool] | None,
        gpu: str | None,
        force_resubmit: bool,
    # [bd-LIGANDAI_ALPHA_V2-pybsy] Boltz-2 --seed (grouped form).
    seed: int | None = None,
    ) -> BatchFoldJob:
        """Grouped fold_batch submission (one fold job per group).

        Mirrors the flat-form hardening pre-flight (GPU allowlist → concurrency
        → dedupe → credit pre-flight → record), but the unit of cost is the
        GROUP (one complex fold) rather than the single peptide. Billing:
        2.5 cr/group/trajectory × ``ceil(sampling_steps / 50)``, floored at a
        10-credit minimum per group (a default 4-trajectory group is 10
        credits, $0.10), matching the server-side computation for the grouped
        envelope. See :mod:`ligandai.pricing`.
        """
        from ligandai._hardening import (
            attach_job_id,
            build_fold_params_for_hash,
            dedupe_lookup_cached,
            enforce_concurrency,
            estimate_fold_batch_credits,
            mark_failed,
            preflight_credits,
            record_submission,
            validate_gpu,
        )
        from ligandai._dedupe import compute_submission_hash

        canonical_gpu = validate_gpu(gpu)
        submitted_set = self._client.submitted_set if self._client is not None else None
        credit_ledger = self._client.credit_ledger if self._client is not None else None
        api_key_hash = self._client.api_key_hash if self._client is not None else ""

        # Build the wire body (also validates / normalizes groups up-front).
        body = _build_grouped_batch_fold_body(
            groups=groups,
            diffusion_samples=diffusion_samples,
            sampling_steps=sampling_steps,
            recycling_steps=recycling_steps,
            step_scale=step_scale,
            glycosylation=glycosylation,
            template_mode=template_mode,
            n_parallel_gpus=n_parallel_gpus,
            session_id=session_id,
            contribute_to_receptordb=contribute_to_receptordb,
            num_trajectories=num_trajectories,
            msa_depth=msa_depth,
            use_potentials=use_potentials,
            seed=seed,
        )
        normalized_groups = body["groups"]
        group_count = len(normalized_groups)

        # Dedupe / hash from the normalized groups + shared params. Reuse the
        # generic submission-hash machinery: the "peptide" axis is the JSON of
        # every group's entity sequences, the "receptor" axis is the kind tag.
        import json as _json
        group_signature = _json.dumps(normalized_groups, sort_keys=True)
        sub_hash = compute_submission_hash(
            peptide_seq=[group_signature],
            receptor_seq="grouped_batch_fold",
            gpu=canonical_gpu,
            params=build_fold_params_for_hash(
                target_gene=None,
                diffusion_samples=diffusion_samples,
                sampling_steps=sampling_steps,
                recycling_steps=recycling_steps,
                step_scale=step_scale,
                msa_enabled=None,
                glycosylation=glycosylation,
                template_mode=template_mode,
                extra={
                    "kind": "fold_batch_grouped",
                    "group_count": group_count,
                    **({"use_potentials": bool(use_potentials)} if use_potentials is not None else {}),
                    **({"msa_depth": int(msa_depth)} if msa_depth is not None else {}),
                },
            ),
        )

        enforce_concurrency(self._client, submitted_set)

        cached = dedupe_lookup_cached(
            submitted_set,
            submission_hash=sub_hash,
            api_key_hash=api_key_hash,
            force_resubmit=force_resubmit,
        )
        if cached and cached.get("job_id"):
            return BatchFoldJob(
                self._transport,
                batch_id=str(cached["job_id"]),
                jobs=[],
                total_cost_credits=int(cached.get("estimated_credits") or 0),
                peptide_count=group_count,
                trajectories_per_peptide=int(diffusion_samples),
                receptor={"source": "cached"},
                sampling_steps=int(sampling_steps),
            )

        # Credit pre-flight — cost unit is the group (one complex fold).
        estimated = estimate_fold_batch_credits(
            peptide_count=group_count,
            trajectories=int(num_trajectories or diffusion_samples),
            sampling_steps=int(sampling_steps),
        )
        balance_before, _ = preflight_credits(
            self._client, estimated=estimated, kind="fold_batch",
        )

        record_submission(
            submitted_set,
            credit_ledger,
            submission_hash=sub_hash,
            api_key_hash=api_key_hash,
            kind="fold_batch_grouped",
            gpu=canonical_gpu,
            estimated_credits=estimated,
            balance_before=balance_before,
            meta={
                "group_count": group_count,
                "trajectories": int(num_trajectories or diffusion_samples),
                "sampling_steps": int(sampling_steps),
            },
        )

        try:
            payload = self._transport.request("POST", "/api/v1/folding/predict-batch", json=body) or {}
        except LigandAICreditError as cred_err:
            if on_credit_exhausted is None:
                mark_failed(
                    submitted_set, submission_hash=sub_hash,
                    api_key_hash=api_key_hash, reason="credit_exhausted",
                )
                raise
            try:
                should_retry = bool(on_credit_exhausted(cred_err, body))
            except Exception:
                mark_failed(
                    submitted_set, submission_hash=sub_hash,
                    api_key_hash=api_key_hash, reason="credit_exhausted_callback_error",
                )
                raise cred_err from None
            if not should_retry:
                mark_failed(
                    submitted_set, submission_hash=sub_hash,
                    api_key_hash=api_key_hash, reason="credit_exhausted_no_retry",
                )
                raise
            payload = self._transport.request("POST", "/api/v1/folding/predict-batch", json=body) or {}
        except Exception as exc:
            mark_failed(
                submitted_set, submission_hash=sub_hash,
                api_key_hash=api_key_hash, reason=type(exc).__name__,
            )
            raise
        batch_id = payload.get("batch_id") or payload.get("batchId") or ""
        if not batch_id:
            mark_failed(
                submitted_set, submission_hash=sub_hash,
                api_key_hash=api_key_hash, reason="server_no_batch_id",
            )
            raise LigandAIError("Server did not return a batch_id for fold_batch", response=payload)
        attach_job_id(
            submitted_set, submission_hash=sub_hash,
            api_key_hash=api_key_hash, job_id=batch_id,
        )
        return BatchFoldJob(
            self._transport,
            batch_id=batch_id,
            jobs=payload.get("jobs") or [],
            total_cost_credits=int(payload.get("total_cost_credits") or 0),
            peptide_count=int(payload.get("peptide_count") or group_count),
            trajectories_per_peptide=int(payload.get("trajectories_per_peptide") or diffusion_samples),
            receptor=payload.get("receptor"),
            sampling_steps=int(payload.get("sampling_steps") or sampling_steps),
        )

    def fold_custom_mutation(
        self,
        gene: str,
        mutations: list[str],
        alias: str | None = None,
    ) -> Job[FoldResult]:
        """``POST /api/ptf/fold-custom-mutation`` — fold a mutated variant."""
        if self._client is not None:
            self._client._require_feature("predict_structure")
        body: dict[str, Any] = {"gene": gene, "mutations": mutations}
        if alias is not None:
            body["alias"] = alias
        payload = self._transport.request("POST", "/api/ptf/fold-custom-mutation", json=body) or {}
        job_id = payload.get("jobId") or payload.get("id") or ""
        if not job_id:
            raise LigandAIError("Server did not return a jobId for custom mutation fold", response=payload)
        return Job(
            self._transport,
            job_id,
            job_type="folding",
            parser=_parse_fold,
            status_path="/api/folding/jobs/{job_id}",
            cancel_path="/api/folding/jobs/{job_id}",
            initial={"id": job_id, "type": "folding", "status": "queued", **payload},
        )

    def continue_folding(
        self,
        session_id: str | None = None,
        gene: str | None = None,
        top_n: int = 25,
        gpu_count: int = 5,
        template_mode: bool = False,
    ) -> Job[GenerationResult]:
        """``POST /api/ptf/parallel/{sid}/continue`` — fold more peptides from an existing session."""
        if not session_id and not gene:
            raise ValueError("Pass session_id= or gene=")
        if not session_id:
            assert gene is not None
            from_session = self._transport.request("GET", f"/api/ptf/sessions/by-gene/{gene}") or {}
            session_id = from_session.get("id")
            if not session_id:
                raise LigandAIError(f"No active session found for gene {gene!r}")
        body = {
            "topN": top_n,
            "gpuCount": gpu_count,
            "templateMode": template_mode,
        }
        payload = (
            self._transport.request("POST", f"/api/ptf/parallel/{session_id}/continue", json=body) or {}
        )
        job_id = payload.get("jobId") or session_id
        return Job(
            self._transport,
            job_id,
            job_type="generation",
            parser=_parse_generation,
            status_path="/api/ptf/parallel/{job_id}/status",
            cancel_path="/api/ptf/parallel/{job_id}/cancel",
            sse_path="/api/ptf/parallel/{job_id}/stream",
            initial={"id": job_id, "type": "generation", "status": "running", **payload},
            result_loader=lambda info: _load_generation_result(
                self._transport,
                info,
                fallback_session_id=session_id,
                fallback_gene=gene,
            ),
        )

    def score_complex(
        self,
        binder_sequence: str,
        target_sequence: str,
        binder_name: str = "binder",
        target_name: str = "target",
        scorer: _DeltaForgeScorer = "auto",
    ) -> Job[DeltaForgeScore]:
        """``POST /api/binder-scoring/fold-and-score`` — submit a fold + DeltaForge scoring job.

        Returns a :class:`Job[DeltaForgeScore]`. Poll with ``.wait()`` and read
        the parsed ``DeltaForgeScore`` from ``.results``.
        """
        body = {
            "binderSequence": binder_sequence,
            "targetSequence": target_sequence,
            "binderName": binder_name,
            "targetName": target_name,
            "scorer": scorer,
        }
        payload = self._transport.request("POST", "/api/binder-scoring/fold-and-score", json=body) or {}
        job_id = payload.get("jobId") or payload.get("id") or ""
        if not job_id:
            raise LigandAIError("Server did not return a jobId", response=payload)

        def parse(data: dict[str, Any]) -> DeltaForgeScore:
            return _parse_deltaforge_score(data)

        return Job(
            self._transport,
            job_id,
            job_type="scoring",
            parser=parse,
            status_path=f"/api/binder-scoring/job/{{job_id}}?scorer={scorer}",
            initial={"id": job_id, "type": "scoring", "status": "submitted"},
        )

    def score_pdb(
        self,
        *,
        pdb_content: str | None = None,
        pdb_file: str | Path | None = None,
        receptor_chains: list[str] | None = None,
        peptide_chain: str | None = None,
        chain_a: str | None = None,
        chain_b: str | None = None,
        scorer: _DeltaForgeScorer = "auto",
        aggregate_method: _DeltaForgeAggregateMethod = "boltzmann_parallel",
        include_features: bool = False,
        include_pae: bool = False,
        fold_ipsae: float | None = None,
        fold_iptm: float | None = None,
        fold_ptm: float | None = None,
        fold_plddt_mean: float | None = None,
        fold_complex_plddt: float | None = None,
        fold_complex_iplddt: float | None = None,
    ) -> DeltaForgeScore:
        """Score a user-provided PDB with DeltaForge.

        Pass either ``pdb_content=`` or ``pdb_file=``. ``receptor_chains`` and
        ``peptide_chain`` are preferred; ``chain_a`` / ``chain_b`` are accepted
        as aliases for single-interface scoring. Optional ``fold_*`` metrics
        are forwarded to the production binder/non-binder gate; affinity
        ``dg``/``kd_nm`` still return independently when the gate calls
        ``not_binder``.

        ``include_pae=True`` asks the server to attach the NxN PAE matrix
        (Angstroms) on :attr:`DeltaForgeScore.pae` when a matching artifact is
        available; otherwise ``pae`` is ``None`` and ``pae_status`` explains why
        (``'pending'`` while a backend pull completes, ``'unavailable'`` when no
        artifact exists for the uploaded structure). Credits are charged only on
        a successful score.
        """
        if not pdb_content and not pdb_file:
            raise ValueError("Pass pdb_content= or pdb_file=")
        if pdb_content and pdb_file:
            raise ValueError("Pass only one of pdb_content= or pdb_file=")
        content = pdb_content if pdb_content is not None else Path(pdb_file).read_text()
        receptors = receptor_chains or ([chain_a] if chain_a else None)
        peptide = peptide_chain or chain_b
        if not receptors or not peptide:
            raise ValueError("Pass receptor_chains= and peptide_chain=, or chain_a= and chain_b=")

        payload = self._transport.request(
            "POST",
            "/api/v1/deltaforge/score-pdb",
            json={
                "pdbContent": content,
                "receptorChains": receptors,
                "peptideChain": peptide,
                "scorer": scorer,
                "aggregateMethod": aggregate_method,
                "includeFeatures": include_features,
                "includePae": include_pae,
                "foldIpsae": fold_ipsae,
                "foldIptm": fold_iptm,
                "foldPtm": fold_ptm,
                "foldPlddtMean": fold_plddt_mean,
                "foldComplexPlddt": fold_complex_plddt,
                "foldComplexIplddt": fold_complex_iplddt,
            },
        ) or {}
        return _parse_deltaforge_score(payload)

    def score_with_ligandiq(
        self,
        session_id: str | None = None,
        gene: str | None = None,
        top_n: int = 20,
    ) -> list[LigandIQScore]:
        """LigandIQ scoring on a session's peptides — synchronous (CPU-only)."""
        if not session_id and not gene:
            raise ValueError("Pass session_id= or gene=")
        if not session_id:
            from_session = self._transport.request("GET", f"/api/ptf/sessions/by-gene/{gene}") or {}
            session_id = from_session.get("id")
            if not session_id:
                raise LigandAIError(f"No active session found for gene {gene!r}")
        body = {"topN": top_n}
        payload = (
            self._transport.request(
                "POST", f"/api/ptf/parallel/{session_id}/ligandiq-score", json=body
            )
            or {}
        )
        items = payload.get("scores") or payload.get("results") or []
        return [LigandIQScore.model_validate(s) for s in items]

    def analyze_solubility(
        self,
        peptides: list[PeptideInput | dict[str, Any] | str],
        gravy_threshold: float = 0.0,
        flag_multi_cys: bool = True,
    ) -> list[SolubilityResult]:
        """``POST /api/peptide-features/solubility`` — GRAVY + cysteine + disulfide check."""
        normalized = [
            (p.model_dump(by_alias=True) if isinstance(p, PeptideInput) else
             {"sequence": p} if isinstance(p, str) else p)
            for p in peptides
        ]
        body = {
            "peptides": normalized,
            "gravyThreshold": gravy_threshold,
            "flagMultiCys": flag_multi_cys,
        }
        payload = (
            self._transport.request("POST", "/api/peptide-features/solubility", json=body)
            or {}
        )
        items = payload.get("results") or payload.get("solubility") or []
        return [SolubilityResult.model_validate(s) for s in items]

    def score_developability(
        self,
        sequences: list[str] | None = None,
        *,
        peptide_ids: list[int] | None = None,
        fold_ids: list[str | int] | None = None,
        modules: list[str] | None = None,
        stability: bool | None = None,
        immuno: bool | None = None,
        halflife: bool | None = None,
    ) -> list[DevelopabilityResult]:
        """``POST /api/v1/peptides/score-developability`` — rescore existing peptides.

        Compute the three developability grades — **stability**,
        **immunogenicity**, and **half-life** — for already-generated peptides
        with NO regeneration and NO refold. This is what lets you grade prior
        design jobs (e.g. PRLR / CD200 / NC16A / LGR5 batches) without re-running
        them.

        Provide any combination of:

        * ``sequences`` — raw amino-acid strings to grade directly.
        * ``peptide_ids`` — ``ptf_generated_peptides.id`` values; resolved to
          sequences server-side AND the grades are persisted back onto those
          rows so subsequent reads return them.
        * ``fold_ids`` — ``ptf_fold_results.id`` values; resolved to sequences.

        Tier: academia and above (the three modules default ON). Opt a module
        out with ``stability=False`` / ``immuno=False`` / ``halflife=False`` (or
        an explicit ``modules=[...]`` whitelist). Free / basic tiers are rejected
        with an upgrade-required error.

        Args:
            sequences: Peptide sequences to grade directly.
            peptide_ids: ptf_generated_peptides primary keys to grade + persist.
            fold_ids: ptf_fold_results primary keys to grade.
            modules: Optional explicit module whitelist; subset of
                ``["stability", "immuno", "halflife"]``. Defaults to all three.
            stability: Set ``False`` to skip the stability grade.
            immuno: Set ``False`` to skip the immunogenicity grade.
            halflife: Set ``False`` to skip the half-life value.

        Returns:
            One :class:`~ligandai.types.DevelopabilityResult` per resolved
            sequence, each carrying the nested ``stability_scores`` /
            ``immuno_scores`` objects plus the headline grades and half-life.
        """
        body = _build_developability_body(
            sequences=sequences,
            peptide_ids=peptide_ids,
            fold_ids=fold_ids,
            modules=modules,
            stability=stability,
            immuno=immuno,
            halflife=halflife,
        )
        payload = (
            self._transport.request("POST", "/api/v1/peptides/score-developability", json=body)
            or {}
        )
        items = payload.get("results") or []
        return [DevelopabilityResult.model_validate(s) for s in items]

    def search(
        self,
        gene: str | None = None,
        classification: str | None = None,
        ipsae_min: float | None = None,
        iptm_min: float | None = None,
        plddt_min: float | None = None,
        kd_max: float | None = None,
        dg_max: float | None = None,
        pde_max: float | None = None,
        pde_engine: str | None = None,
        binder_pct_min: float | None = None,
        length_min: int | None = None,
        length_max: int | None = None,
        is_elite: bool | None = None,
        super_elite: bool | None = None,
        super_elite_affinity: bool | None = None,
        super_elite_thermo: bool | None = None,
        acceptance_criterion: Literal[
            "great_plus", "elite", "super_elite", "super_elite_affinity"
        ] | None = None,
        require_linker_leg: bool | None = None,
        require_ensemble: bool | None = None,
        hotspot_residues: list[str] | None = None,
        pocket_residues: list[str] | None = None,
        hotspot_hit: bool | None = None,
        pocket_hit: bool | None = None,
        contact_distance_a: float | None = None,
        stability_grade: list[str] | None = None,
        immuno_grade: list[str] | None = None,
        conformation: str | None = None,
        program_id: int | None = None,
        session_id: str | None = None,
        pdb_id: str | None = None,
        sort: str = "ipsae",
        order: str = "desc",
        min_ipsae: float | None = None,  # legacy alias
        limit: int = 20,
        offset: int = 0,
    ) -> list[Peptide]:
        """``GET /api/v1/peptides/search`` — rich cross-program peptide search.

        Supports every criterion the workspace UI exposes plus a few extras
        for SDK-driven workflows. All filters AND-combine.

        Args:
            gene: Gene symbol filter (uppercased server-side).
            ipsae_min / iptm_min / plddt_min: Minimum Boltz-2 quality scores.
            kd_max: Maximum predicted Kd in Molar (e.g. ``1e-8`` = 10 nM).
            dg_max: Maximum predicted ΔG in kcal/mol (negative is better; pass
                ``-8.0`` for "ΔG ≤ -8 kcal/mol").
            pde_max: Maximum predicted distance error in Angstroms. PDE runs
                OPPOSITE to iPSAE/iPTM/pLDDT — **lower is better** — so this is
                a ceiling, not a floor. On the 199-fold labelled holdout the MIN
                interface PDE scored AUROC 0.806 [0.743, 0.862], statistically
                indistinguishable from iPSAE's 0.811.

                ⛔ Without ``pde_engine`` this filters the server's
                ``pde_rankable`` column, which is NULL for any fold whose PDE is
                a PAE-derived proxy or sits on an unverified scale (Boltz-2).
                Those folds are therefore EXCLUDED rather than compared on an
                incompatible scale. That is deliberate: a PDE number is only
                comparable to another PDE number from an engine on the same
                scale.
            pde_engine: Pin the query to a single fold engine (a FoldEngineId,
                e.g. ``"protenix"``). Required if you want ``pde_max`` to filter
                the raw ``interface_pde_min`` — including proxy / unverified-
                scale rows — since within one engine the values are mutually
                comparable even when they are not comparable across engines.
            binder_pct_min: Minimum DeltaForge binder probability (0..1).
            length_min / length_max: Peptide length range in residues.
            is_elite: Server-flagged elite (iPSAE ≥ 0.80 by default).
            super_elite: Proteina-Complexa structural-confidence gate
                (bioRxiv v27): iPSAE ≥ 0.67 AND iPTM ≥ 0.80 AND
                pLDDT ≥ 88 (0–100 scale; null passes). The 3-metric
                structural gate. Use this for the headline
                "super-elite" count.
            super_elite_affinity: Affinity super-elite — structural gate
                above PLUS predicted Kd < 100 nM (DeltaForge). The
                synthesis-priority subset. Reported as a SEPARATE
                bucket from the structural gate.
            super_elite_thermo: Deprecated alias for ``super_elite_affinity``.
            acceptance_criterion: Filter by the ONE canonical acceptance ladder
                (``shared/acceptance-gate.json`` — the same spec ``is_elite`` /
                ``super_elite`` / ``super_elite_affinity`` above are shorthand
                for). One of ``"great_plus"`` (``peptide_ipsae >= 0.66`` — the
                primary hit-rate metric), ``"elite"`` (``peptide_ipsae >= 0.85``
                single-metric — NOT the same 0.85 as the legacy ``is_elite``
                flag, which historically used 0.80), ``"super_elite"``
                (``peptide_ipsae >= 0.67`` AND ``iptm >= 0.80`` AND
                ``plddt >= 88`` — the structural/publication gate), or
                ``"super_elite_affinity"`` (the structural gate PLUS
                ``predicted_kd < 100`` nanomolar). Every returned
                :class:`Peptide` also carries :attr:`Peptide.quality_tier`
                (the strongest criterion it clears) regardless of whether you
                filter on one here.

                ⛔ UNIT TRAPS: ``plddt`` is 0–100 (``0.88`` is a silent no-op
                that passes everything); ``predicted_kd`` is nanomolar
                (compare to ``100``, never ``100e-9``); the structural leg
                gates ``peptide_ipsae``, never whole-complex ``ipsae``.
            require_linker_leg: Also require the DESIGNED region (not a
                purification cassette / linker) to carry the interface —
                ``scaffold_mediated`` must be false AND
                ``binder_contact_fraction_rankable > 0.5``. A fold whose
                construct frame could not be verified is EXCLUDED
                (``indeterminate``), never silently admitted as a pass.
                Defaults to the spec default (``True``) when omitted.
            require_ensemble: The DUAL GATE. Require the design to clear TWO
                independent screens on the SAME construct frame: the Boltz-2
                structural criterion you selected above, AND the LigandIQ
                Pre-Fold confirmatory screen at its own calibrated threshold.
                A high-recall ranker followed by a high-precision filter —
                not two scores averaged. Each engine clears its own bar.

                ⛔ ENTERPRISE-ONLY. A caller whose plan does not include it
                receives an explicit ``403``; the flag is never silently
                dropped in favour of a single-engine result reported as
                though the dual gate had been applied.

                ⛔ IT TRADES RECALL FOR PRECISION. The confirmatory screen
                discards a large share of true binders by construction, so
                expect substantially fewer surviving designs. A design it
                does not confirm comes back ``co_validated=False`` — that is
                NOT evidence the design is bad.

                When the confirmatory screen has no result for a design the
                verdict is ``'indeterminate'``, never a pass. If nothing in
                your result set carries a confirmatory result, that is a
                statement about the screen not having been run over those
                designs — not a finding of "zero co-validated designs".

                Defaults to the spec default (``False``) when omitted.
            hotspot_residues: List of ``"chain:resi"`` strings (PDB
                numbering) for the residues you wanted the peptide to
                contact, e.g. ``["A:60", "A:62"]``.
            pocket_residues: Same shape, for the surrounding pocket.
            hotspot_hit: Require the peptide's fold to contact AT LEAST one
                listed hotspot residue within ``contact_distance_a`` Å.
            pocket_hit: Require contact with the hotspot OR pocket set.
            contact_distance_a: Heavy-atom cutoff for "hit" (default 5.0 Å).
            stability_grade / immuno_grade: List of acceptable grades
                (e.g. ``["A", "B"]``).
            conformation: Exact conformation string filter.
            program_id / session_id / pdb_id: Scope filters.
            sort: Sort key — one of ``ipsae`` | ``iptm`` | ``plddt`` |
                ``kd`` | ``dg`` | ``pde`` | ``length`` | ``created_at``.

                ``sort="pde"`` ranks on ``pde_rankable`` (cross-engine safe)
                unless ``pde_engine`` pins the query, in which case it ranks on
                the raw ``interface_pde_min`` for that engine. Pair it with
                ``order="asc"`` — lower PDE is better.
            order: ``"asc"`` or ``"desc"`` (default ``"desc"``).
            limit: Page size (max 200).
            offset: Pagination offset.

        Returns:
            List of peptides matching all criteria, sorted by ``sort``/``order``.
            Each peptide includes ``hotspot_contacts`` / ``pocket_contacts``
            arrays when residue criteria were specified, with per-residue
            heavy-atom distances.
        """
        if min_ipsae is not None and ipsae_min is None:
            ipsae_min = min_ipsae
        if super_elite_thermo is not None:
            warnings.warn(
                "super_elite_thermo is deprecated; use super_elite_affinity instead.",
                DeprecationWarning,
                stacklevel=2,
            )
            if super_elite_affinity is None:
                super_elite_affinity = super_elite_thermo
        params: dict[str, Any] = {"limit": limit, "offset": offset, "sort": sort, "order": order}
        if gene is not None: params["gene"] = gene.upper()
        if classification is not None: params["classification"] = classification
        if ipsae_min is not None: params["ipsae_min"] = ipsae_min
        if iptm_min is not None: params["iptm_min"] = iptm_min
        if plddt_min is not None: params["plddt_min"] = plddt_min
        if kd_max is not None: params["kd_max"] = kd_max
        if dg_max is not None: params["dg_max"] = dg_max
        # PDE (migration 170). LOWER IS BETTER, so this is a MAX threshold.
        # Without pde_engine the server filters pde_rankable — the DB-generated
        # cross-engine-safe column — so proxy and unverified-scale folds are
        # excluded rather than silently mixed in. Supplying pde_engine pins the
        # query to one engine and filters the raw interface_pde_min instead.
        if pde_max is not None: params["pde_max"] = pde_max
        if pde_engine is not None: params["pde_engine"] = pde_engine
        if binder_pct_min is not None: params["binder_pct_min"] = binder_pct_min
        if length_min is not None: params["length_min"] = length_min
        if length_max is not None: params["length_max"] = length_max
        if is_elite is not None: params["is_elite"] = "true" if is_elite else "false"
        if super_elite is not None: params["super_elite"] = "true" if super_elite else "false"
        if super_elite_affinity is not None: params["super_elite_affinity"] = "true" if super_elite_affinity else "false"
        # Acceptance-gate ladder [bd-LIGANDAI_ALPHA_V2-dl6pj]. The SDK does not
        # validate `acceptance_criterion` against the four canonical names —
        # that whitelist lives in shared/acceptance-gate.json and the server
        # is the authority; an unrecognised value is the server's 4xx to raise,
        # not the SDK's to pre-empt.
        if acceptance_criterion is not None: params["acceptance_criterion"] = acceptance_criterion
        if require_linker_leg is not None: params["require_linker_leg"] = "true" if require_linker_leg else "false"
        if require_ensemble is not None: params["require_ensemble"] = "true" if require_ensemble else "false"
        if hotspot_hit is not None: params["hotspot_hit"] = "true" if hotspot_hit else "false"
        if pocket_hit is not None: params["pocket_hit"] = "true" if pocket_hit else "false"
        if hotspot_residues: params["hotspot_residues"] = ",".join(hotspot_residues)
        if pocket_residues: params["pocket_residues"] = ",".join(pocket_residues)
        if contact_distance_a is not None: params["contact_distance_a"] = contact_distance_a
        if stability_grade: params["stability_grade"] = ",".join(stability_grade)
        if immuno_grade: params["immuno_grade"] = ",".join(immuno_grade)
        if conformation is not None: params["conformation"] = conformation
        if program_id is not None: params["program_id"] = program_id
        if session_id is not None: params["session_id"] = session_id
        if pdb_id is not None: params["pdb_id"] = pdb_id.upper()

        payload = self._transport.request(
            "GET", "/api/v1/peptides/search", params=params
        ) or {}
        items = payload.get("peptides", []) if isinstance(payload, dict) else (payload or [])
        return [Peptide.model_validate(p) for p in items]

    def fill_until(
        self,
        gene: str,
        target_count: int = 10,
        criteria: dict[str, Any] | None = None,
        batch_size: int = 100,
        max_iterations: int = 3,
        budget_credits_max: int = 50000,
        mode: str = "plan",
    ) -> dict[str, Any]:
        """``POST /api/v1/peptides/auto-generate-until`` — generate-and-fold loop.

        Plan or kick off a generate-and-fold loop until ``target_count``
        peptides match ``criteria``. ``criteria`` accepts the same keys as
        :meth:`search` (e.g. ``ipsae_min``, ``kd_max``, ``hotspot_residues``,
        ``hotspot_hit``, ``super_elite``).

        With ``mode="plan"`` (default) the server returns:
            * ``current_passing_count`` — peptides already in the DB that match
            * ``remaining`` — how many more you need
            * ``plan.batches_recommended`` / ``est_credits`` / ``est_minutes``

        With ``mode="start"`` the server validates the plan against your
        ``budget_credits_max`` and returns a ``next_action`` describing the
        exact /api/ptf/parallel/generate calls to make. The SDK does NOT
        currently spawn the loop server-side — you wrap it in your own
        ``while`` so you can checkpoint progress and inspect intermediate
        peptides.

        Example (loop client-side until 25 super-elite peptides found):

            crit = {"super_elite": True, "hotspot_residues": ["A:60","A:62"], "hotspot_hit": True}
            for _ in range(5):
                plan = client.peptides.fill_until("BMPR1A", target_count=25, criteria=crit, mode="plan")
                if plan["remaining"] == 0:
                    break
                client.peptides.generate(gene="BMPR1A", num_peptides=plan["plan"]["batch_size"])
                client.peptides.wait_for_session(...)  # poll generation status
            results = client.peptides.search(gene="BMPR1A", **crit, limit=25)
        """
        body = {
            "gene": gene.upper() if gene else None,
            "target_count": target_count,
            "criteria": criteria or {},
            "batch_size": batch_size,
            "max_iterations": max_iterations,
            "budget_credits_max": budget_credits_max,
            "mode": mode,
        }
        return self._transport.request(
            "POST", "/api/v1/peptides/auto-generate-until", json=body
        ) or {}

    def pocket_for_hotspots(
        self,
        pdb_id: str | None = None,
        hotspots: list[str] | None = None,
        radius_a: float = 8.0,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """``GET /api/v1/structures/{pdb_id}/pocket`` — pocket residues
        within ``radius_a`` Å of one or more hotspots.

        Args:
            pdb_id: RCSB PDB code (e.g. ``"9MIR"``). Required unless
                ``session_id`` is provided.
            hotspots: ``["A:60", "A:62"]`` — chain:residue PDB numbering.
            radius_a: Heavy-atom radius for pocket inclusion (default 8.0).
            session_id: Compute the pocket from a fold session's structure
                instead of the canonical PDB.

        Returns:
            ``{ pdb_id, hotspots, radius_a, pocket_residues, n_pocket_residues }``.
            Each pocket residue is ``{ chain, residue (PDB), resname, distance_a }``.
        """
        if not pdb_id and not session_id:
            raise ValueError("pocket_for_hotspots requires either pdb_id or session_id")
        if not hotspots:
            raise ValueError("pocket_for_hotspots requires hotspots, e.g. ['A:60','A:62']")
        params: dict[str, Any] = {"hotspots": ",".join(hotspots), "radius_a": radius_a}
        if session_id:
            params["session_id"] = session_id
        path = f"/api/v1/structures/{(pdb_id or '').upper()}/pocket"
        return self._transport.request("GET", path, params=params) or {}

    def search_by_pocket(
        self,
        gene: str,
        chain: str | None = None,
        start_residue: int | None = None,
        end_residue: int | None = None,
        targeted_only: bool = True,
    ) -> list[Peptide]:
        """``GET /api/ptf/peptides/by-pocket`` — find prior peptides targeting a pocket."""
        params: dict[str, Any] = {"gene": gene, "targeted_only": targeted_only}
        if chain is not None:
            params["chain"] = chain
        if start_residue is not None:
            params["start_residue"] = start_residue
        if end_residue is not None:
            params["end_residue"] = end_residue
        payload = self._transport.request(
            "GET", "/api/ptf/peptides/by-pocket", params=params
        )
        items = unwrap_rows(
            payload,
            _BY_POCKET_KEYS,
            what=f"GET /api/ptf/peptides/by-pocket for {gene!r}",
        )
        return [Peptide.model_validate(p) for p in items]

    def get_elite(
        self,
        session_id: str | None = None,
        gene: str | None = None,
    ) -> list[Peptide]:
        """``GET /api/ptf/parallel/{sid}/elite`` — elite peptides for a session."""
        if not session_id and not gene:
            raise ValueError("Pass session_id= or gene=")
        if not session_id:
            from_session = self._transport.request(
                "GET", f"/api/ptf/sessions/by-gene/{gene}"
            )
            session_id = _session_id_for_gene(from_session, gene)
            if not session_id:
                raise LigandAIError(f"No active session for gene {gene!r}")
        payload = self._transport.request(
            "GET", f"/api/ptf/parallel/{session_id}/elite"
        )
        items = unwrap_rows(
            payload,
            _ELITE_KEYS,
            what=f"GET /api/ptf/parallel/{session_id}/elite",
        )
        return [Peptide.model_validate(p) for p in items]

    # ------------------------------------------------------------------
    # Selectivity / negative-selection validation (fold-compare)
    # ------------------------------------------------------------------
    #
    # Post-hoc selectivity is the SUPPORTED negative-selection path: after
    # generation + fold, co-fold each candidate binder against the on-target's
    # isoforms (and any off-targets) and score the complexes to rank by
    # selectivity margin. Uses the real fold-compare surface
    # (/api/v1/fold-compare/*). See validate_selectivity() to run this directly
    # on a completed generation session.
    #
    # ⛔ CORRECTED 2026-08-31 (bd-dre-if562). The paragraph here used to say
    # generation-time negative selection "is NOT wired into the current
    # generator". That was true of the v6.5 pocket-conditioned line, which has no
    # multi-structure repulsion term, and it is FALSE of the v6.9 line: those
    # models are served with a paralog-mean CFG reference and will not run
    # without REAL off-target receptor pockets to steer away from — the platform
    # derives them and refuses the request rather than substituting an all-zero
    # reference. So generation-time negative selection exists and is the default
    # on v6.9; what is missing is the ability to CHOOSE the panel (see
    # ligandai.contrastive and client.campaigns).
    #
    # The two mechanisms answer different questions and both are wanted:
    # contrastive guidance shapes what gets generated, fold_compare /
    # client.campaigns.fold_panel MEASURE what was generated. Guidance is never
    # evidence of selectivity — folding against the panel is.

    def fold_compare(
        self,
        binder: str,
        on_target: str,
        *,
        isoforms: str | Sequence[str] | None = None,
        off_targets: Sequence[str] | None = None,
        engine: str = "boltz2",
        binder_id: str = "binder",
        on_target_sequence: str | None = None,
        organism: str | None = None,
        species: str | None = None,
        wait: bool = True,
        poll_interval: float = 10.0,
        timeout: float = 1800.0,
    ) -> dict[str, Any]:
        """Fold-and-score a designed binder against a target's isoforms + off-targets.

        Negative-selection / selectivity validation for a designed binder. Co-folds
        the binder against (a) the on-target's isoforms (``"all"`` or a chosen
        subset) and (b) arbitrary off-target proteins, then scores each complex to
        return a ranked selectivity report. Real folding engines + our own shared
        MSA (no external MSA, no fabricated Kd/AUC).

        Provided isoform STRUCTURES are supported by passing the isoform amino-acid
        sequences: use ``on_target_sequence`` for an explicit on-target sequence,
        and pass isoform sequences via ``off_targets`` (raw sequences are accepted
        and folded verbatim) when you want to score against exact provided
        structures rather than gene-enumerated canonicals.

        POSTs to ``/api/v1/fold-compare/start`` and (when ``wait``) polls
        ``/api/v1/fold-compare/{job_id}/status`` to a terminal state.

        Args:
            binder: Designed binder amino-acid sequence.
            on_target: On-target gene symbol / UniProt accession (drives isoform
                enumeration), or pass ``on_target_sequence`` for an explicit seq.
            isoforms: ``"all"`` or a list of isoform ids / accessions / names.
                ``None`` → off-targets only.
            off_targets: Off-target specs — gene symbol, UniProt accession, OR a
                raw amino-acid sequence (e.g. an isoform sequence pulled from a
                provided structure).
            engine: ``"boltz2"`` (default) or ``"protenix"``.
            binder_id: Label for the binder in the report.
            on_target_sequence: Explicit on-target sequence (overrides the resolved
                canonical; isoforms still enumerate from ``on_target``).
            organism: ``"human"`` (default) or ``"mouse"`` for gene/isoform
                resolution. Falls back to the client ``default_organism``;
                coerced to ``"human"`` when the key is not entitled (fail-closed),
                so a crafted ``organism="mouse"`` never forces mouse. Raw
                sequences are species-agnostic and unaffected.
            species: Alias for ``organism`` (server-parity). ``organism`` wins.
            wait: When True (default), poll to a terminal state and return the
                report; else return the ``{job_id, ...}`` submit response.
            poll_interval: Seconds between status polls when ``wait``.
            timeout: Max seconds to wait when ``wait``.

        Returns:
            The ranked selectivity report (wait=True) or the raw submit dict.
        """
        if not binder or not isinstance(binder, str):
            raise LigandAIValidationError("binder must be an amino-acid sequence")
        if not on_target and not on_target_sequence:
            raise LigandAIValidationError(
                "on_target (gene / UniProt) or on_target_sequence is required",
            )
        body: dict[str, Any] = {
            "binder": {"id": binder_id, "sequence": binder.strip().upper()},
            "engine": engine,
        }
        # Fail-closed effective species (entitlement-gated). Always stamped so the
        # server sees the SDK's resolved namespace; a non-entitled key -> human.
        effective = self._resolve_organism(organism, species)
        body["organism"] = effective
        body["species"] = effective
        if on_target_sequence:
            body["on_target"] = {
                "gene": on_target,
                "sequence": on_target_sequence.strip().upper(),
            }
        else:
            body["on_target"] = {"gene": on_target}
        if isoforms is not None:
            body["isoforms"] = isoforms if isoforms == "all" else list(isoforms)
        if off_targets:
            body["off_targets"] = list(off_targets)

        submit = self._transport.request(
            "POST", "/api/v1/fold-compare/start", json=body
        ) or {}
        if not wait:
            return submit
        job_id = submit.get("job_id") or submit.get("jobId")
        if not job_id:
            raise LigandAIValidationError(
                f"fold-compare start returned no job_id: {submit}"
            )
        return self._poll_fold_compare(
            job_id, poll_interval=poll_interval, timeout=timeout
        )

    def _resolve_organism(self, organism: str | None, species: str | None) -> str:
        """Fail-closed effective species for a fold-compare call.

        ``organism`` wins over ``species``; both fall back to the client default
        and are coerced to ``"human"`` when the key is not entitled. Falls back
        to ``"human"`` gracefully when no parent client is attached.
        """
        requested = organism if organism is not None else species
        if self._client is not None:
            return str(self._client._resolve_species(requested))
        from ligandai.species import normalize_species

        return str(normalize_species(requested)) if requested is not None else "human"

    def _poll_fold_compare(
        self,
        job_id: str,
        *,
        poll_interval: float = 10.0,
        timeout: float = 1800.0,
    ) -> dict[str, Any]:
        import time

        start = time.time()
        terminal = {"completed", "done", "succeeded", "failed", "cancelled", "error"}
        while True:
            status = self._transport.request(
                "GET", f"/api/v1/fold-compare/{job_id}/status"
            ) or {}
            state = str(status.get("status") or status.get("state") or "").lower()
            if state in terminal:
                return status
            if time.time() - start > timeout:
                raise LigandAIWaitTimeout(f"fold-compare job timed out: {job_id}")
            time.sleep(poll_interval)

    def validate_selectivity(
        self,
        session_id: str,
        on_target: str,
        *,
        isoforms: str | Sequence[str] | None = "all",
        off_targets: Sequence[str] | None = None,
        top_n: int = 10,
        engine: str = "boltz2",
        organism: str | None = None,
        species: str | None = None,
        wait: bool = True,
        poll_interval: float = 10.0,
        timeout: float = 1800.0,
    ) -> list[dict[str, Any]]:
        """Run isoform / off-target negative selection on a completed generation.

        Convenience bridge: pulls the elite folded candidates from a finished
        generation ``session_id`` via :meth:`get_elite`, takes the top ``top_n``
        by iPSAE, and runs :meth:`fold_compare` on each against the on-target
        isoforms + off-targets. This is the supported negative-selection path
        from the generation workflow — design first, then score selectivity
        against provided ET isoforms.

        Args:
            session_id: A completed generation session (from ``generate(...)``).
            on_target: On-target gene / UniProt for isoform enumeration.
            isoforms: ``"all"`` (default), a subset list, or None.
            off_targets: Extra off-target specs (gene / UniProt / raw sequence —
                pass isoform sequences from provided structures here).
            top_n: How many top elite candidates to validate.
            engine: Fold engine for the comparison.
            organism: ``"human"`` (default) / ``"mouse"``. Falls back to the client
                ``default_organism``; coerced to ``"human"`` when not entitled
                (fail-closed). ``species`` is an accepted alias.
            species: Alias for ``organism`` (server-parity). ``organism`` wins.
            wait / poll_interval / timeout: Forwarded to :meth:`fold_compare`.

        Returns:
            List of ``{binder, fold_compare}`` dicts, one per validated candidate.
        """
        if top_n < 1:
            raise LigandAIValidationError("top_n must be at least 1")
        elite = self.get_elite(session_id=session_id)

        def _ipsae(p: Peptide) -> float:
            # ⛔ peptide_ipsae is THE binder metric — the peptide<->receptor
            # interface only. Whole-complex `ipsae` is inflated by the receptor's
            # confidence in itself on any multimeric target, so ranking on it
            # sends the wrong candidates into the selectivity comparison. Fall
            # back to `ipsae` ONLY when the binder metric was never computed, and
            # sort a never-computed row last rather than treating it as a zero.
            v = getattr(p, "peptide_ipsae", None)
            if not isinstance(v, (int, float)):
                v = getattr(p, "ipsae", None)
            return float(v) if isinstance(v, (int, float)) else -1.0

        ranked = sorted(
            (p for p in elite if getattr(p, "sequence", None)),
            key=_ipsae,
            reverse=True,
        )[:top_n]

        out: list[dict[str, Any]] = []
        for p in ranked:
            seq = str(p.sequence).strip().upper()
            report = self.fold_compare(
                binder=seq,
                on_target=on_target,
                isoforms=isoforms,
                off_targets=off_targets,
                engine=engine,
                binder_id=str(getattr(p, "peptide_id", None) or seq[:12]),
                organism=organism,
                species=species,
                wait=wait,
                poll_interval=poll_interval,
                timeout=timeout,
            )
            out.append({"binder": p, "fold_compare": report})
        return out

    # ------------------------------------------------------------------
    # v0.2.0 surface — paid-only /api/v1/peptides/*
    # ------------------------------------------------------------------

    def by_gene(
        self,
        genes: list[str] | None = None,
        min_ipsae: float | None = None,
        program_id: int | None = None,
        project_id: int | None = None,
        since: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[GeneSummary]:
        """``GET /api/v1/peptides/by-gene`` — gene-level peptide aggregation.

        Aggregates peptide stats per gene across **all of the caller's
        sessions and programs**. Use this to answer "what binders do I have
        for gene X?" — one row per gene with folded counts, best scores,
        program/session coverage. Follow up with :meth:`list` for the
        actual peptide rows.

        **Auth (changed v0.5.3):** Open to ALL tiers including free. Free
        users see aggregate counts only — sequences and structures from
        downstream :meth:`list` / :meth:`get` are masked (first 4 AA +
        ``********``) and PDBs are returned as polyalanine.

        Args:
            genes: Optional whitelist (case-insensitive). When omitted, all
                genes the caller has folds for are returned.
            min_ipsae: Filter aggregation to folds with iPSAE ≥ this threshold.
                ``foldedCount`` reflects only folds meeting the bar.
            program_id: Restrict to one program (Layer-4 program DB id).
            project_id: Restrict to one project.
            since: Only count folds at or after this timestamp.
            limit: Page size (max 200; server caps).
            offset: Pagination offset.

        Returns:
            List of :class:`~ligandai.types.GeneSummary` rows, sorted by
            ``last_activity_at`` descending. To compute total pages, divide
            the server's ``total`` (in the raw response, not exposed here)
            by your ``limit``.
        """
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if genes:
            params["genes"] = ",".join(g.upper() for g in genes if g)
        if min_ipsae is not None:
            params["minIpsae"] = min_ipsae
        if program_id is not None:
            params["programId"] = program_id
        if project_id is not None:
            params["projectId"] = project_id
        if since is not None:
            params["since"] = since.isoformat()
        payload = self._transport.request("GET", "/api/v1/peptides/by-gene", params=params) or {}
        rows = payload.get("rows", []) if isinstance(payload, dict) else []
        return [GeneSummary.model_validate(r) for r in rows]

    def by_pdb(
        self,
        pdb: str | list[str],
        min_ipsae: float | None = None,
        since: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """``GET /api/v1/peptides/by-pdb`` — peptide aggregation pivoted by target PDB code.

        Mirror of :meth:`by_gene` for users whose generation requests
        targeted a specific PDB ID instead of a gene symbol (common for
        custom-uploaded structures or multi-chain complexes — e.g.
        ``"9MIR"`` for the BMPR1A–RGMB heteromer). Returns one row per
        ``(pdb_code, gene)`` combination with session count + total
        peptides generated + activity timestamps.

        **Auth:** Open to ALL tiers (free included). Aggregate counts only;
        sequences and structures still tier-gated through
        :meth:`list` / :meth:`get` / :meth:`download_pdb`.

        Args:
            pdb: One PDB code or a list of codes (case-insensitive).
            min_ipsae: Server-side iPSAE threshold (currently informational
                only — the per-row endpoints carry the real iPSAE filter).
            since: Only sessions newer than this timestamp.
            limit: Page size (max 200).
            offset: Pagination offset.

        Returns:
            List of dicts with ``pdbCode``, ``gene``, ``sessions``,
            ``peptidesGenerated``, ``firstActivityAt``, ``lastActivityAt``.

        Example:
            >>> rows = client.peptides.by_pdb("9MIR")
            >>> for r in rows:
            ...     print(r["pdbCode"], r["gene"], r["sessions"], r["peptidesGenerated"])
            9MIR BMPR1A 3 150
        """
        codes = [pdb] if isinstance(pdb, str) else list(pdb)
        params: dict[str, Any] = {
            "pdb": ",".join(c.upper() for c in codes if c),
            "limit": limit,
            "offset": offset,
        }
        if min_ipsae is not None:
            params["min_ipsae"] = min_ipsae
        if since is not None:
            params["since"] = since.isoformat()
        payload = self._transport.request("GET", "/api/v1/peptides/by-pdb", params=params) or {}
        return payload.get("rows", []) if isinstance(payload, dict) else []

    def list(
        self,
        gene_or_program_id: str | int | None = None,
        *,
        gene: str | None = None,
        program_id: int | None = None,
        min_ipsae: float | None = None,
        min_iptm: float | None = None,
        max_kd: float | None = None,
        include_unfolded: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Peptide]:
        """List peptide rows. v0.5.0: now supports both ``gene`` and ``program_id``.

        Backwards-compatible:
          * ``client.peptides.list("EGFR")`` — gene mode (legacy)
          * ``client.peptides.list(gene="EGFR")`` — explicit gene (legacy)
          * ``client.peptides.list(42)`` — program_id mode (NEW)
          * ``client.peptides.list(program_id=42, min_ipsae=0.8)`` — explicit (NEW)
          * ``client.peptides.list(program_id=42, gene="EGFR")`` — both (NEW)

        a user's #1 bug: ``client.peptides.list(program_id)`` raised
        ``TypeError`` because the signature only took ``gene``. v0.5.0 accepts
        either by detecting the positional arg's type (str → gene, int →
        program_id) and exposes both as keyword args.

        Args:
            gene_or_program_id: Positional shortcut. ``str`` = gene symbol;
                ``int`` = program DB id. Pass ``None`` (the default) to scope
                across all programs (paid tier only — free keys must scope to
                a program or gene to limit response size).
            gene: Gene symbol (case-insensitive; upper-cased server-side).
            program_id: Restrict to one program (Layer-4 program DB id).
            min_ipsae: Filter to folds with iPSAE ≥ this threshold.
            min_iptm: Filter to folds with ipTM ≥ this threshold.
            max_kd: Filter to folds with predicted Kd ≤ this value (M).
            include_unfolded: Reserved for legacy compatibility. Has no effect
                on the new ``/v1/peptides/list`` endpoint (folded only).
            limit: Page size (max 200).
            offset: Pagination offset.

        Returns:
            List of :class:`~ligandai.types.Peptide` rows. When the caller is
            on the free tier, ``sequence`` is truncated to the first 10 amino
            acids + ``********``; check the response's ``_tier_redacted``
            metadata (or :class:`~ligandai.errors.LigandAIUpgradeRequired`).
        """
        # ⛔ `include_unfolded` is ACCEPTED and does nothing — it never reaches
        # `params` and the endpoint returns folded rows only. A docstring
        # saying "has no effect" is not a runtime signal: a caller who passes
        # include_unfolded=True gets a folded-only list that looks exactly
        # like a complete one, and will read the missing unfolded designs as
        # "there are none". Say so out loud, naming the parameter.
        # [bd-dre-rp11j.3]
        if include_unfolded:
            warnings.warn(
                "include_unfolded=True was accepted but has NO EFFECT: the "
                "/v1/peptides/list endpoint returns FOLDED peptides only, and "
                "this parameter is never sent. The list you get back excludes "
                "unfolded designs - do not read its length as a total. To "
                "count generated-but-unfolded designs, read the generation "
                "job's own result instead. Pass include_unfolded=False (the "
                "default) to silence this.",
                LigandAIDiagnosticWarning,
                stacklevel=2,
            )

        # Reconcile positional arg with explicit keywords
        if gene_or_program_id is not None:
            if isinstance(gene_or_program_id, str):
                if gene is not None and gene != gene_or_program_id:
                    raise ValueError("conflicting gene values: pass either positionally or by keyword, not both")
                gene = gene_or_program_id
            elif isinstance(gene_or_program_id, int):
                if program_id is not None and program_id != gene_or_program_id:
                    raise ValueError("conflicting program_id values: pass either positionally or by keyword, not both")
                program_id = gene_or_program_id
            else:
                raise TypeError(
                    f"list() positional arg must be a gene symbol (str) or program_id (int); "
                    f"got {type(gene_or_program_id).__name__}"
                )

        # New /v1/peptides/list endpoint when program_id, min_iptm, or max_kd is supplied
        # OR when gene is supplied (always preferred — returns the richer schema)
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if program_id is not None:
            params["program_id"] = program_id
        if gene is not None:
            if not gene.strip():
                raise ValueError("gene must be a non-empty string")
            params["gene"] = gene.upper()
        if min_ipsae is not None:
            params["min_ipsae"] = min_ipsae
        if min_iptm is not None:
            params["min_iptm"] = min_iptm
        if max_kd is not None:
            params["max_kd"] = max_kd

        payload = self._transport.request(
            "GET", "/api/v1/peptides/list", params=params
        ) or {}
        items = payload.get("peptides", []) if isinstance(payload, dict) else (payload or [])
        return [Peptide.model_validate(p) for p in items]

    def list_by_program(
        self,
        program_id: int,
        *,
        min_ipsae: float | None = None,
        min_iptm: float | None = None,
        max_kd: float | None = None,
        gene: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Peptide]:
        """``GET /api/v1/peptides/list?program_id=X`` — list peptides in a program.

        Convenience method for the common a common query: "give me
        all peptides in program 42, optionally filtered by score thresholds."

        Args:
            program_id: Program DB id.
            min_ipsae: Filter to folds with iPSAE ≥ this threshold.
            min_iptm: Filter to folds with ipTM ≥ this threshold.
            max_kd: Filter to folds with predicted Kd ≤ this value (M).
            gene: Optional gene filter within the program.
            limit: Page size (max 200).
            offset: Pagination offset.
        """
        return self.list(
            program_id=program_id,
            # ⛔ `conversation_id=conversation_id` stood here and made BOTH
            # twins of this public method raise NameError on every call —
            # the name is not a parameter of list_by_program, is never bound
            # in this module's scope, and is not a parameter of `list()`
            # either, so even a bound value would have been a TypeError one
            # line later. The method could never have run. [bd-dre-rp11j.3]
            gene=gene,
            min_ipsae=min_ipsae,
            min_iptm=min_iptm,
            max_kd=max_kd,
            limit=limit,
            offset=offset,
        )

    def get(
        self,
        peptide_id: int | str,
        include: list[_IncludeField] | None = None,
    ) -> PeptideDetail:
        """``GET /api/v1/peptides/:id`` — single-peptide detail.

        Default response is "thin" — sequence + scores + metadata, no heavy
        fields. Heavy fields are gated behind ``include=`` to keep typical
        reads fast.

        **Auth:** Paid tiers only. Free keys raise
        :class:`~ligandai.errors.LigandAIPaidTierRequired`.

        Args:
            peptide_id: ``ptf_fold_results.id`` (positive integer). Strings
                are accepted and parsed.
            include: Optional list of heavy fields to fetch:

                - ``"pocket_features"`` adds ``pocket_features_48_dim`` and
                  ``pocket_features_metadata``.
                - ``"interface"`` adds ``peptide_per_receptor`` and
                  ``disulfide_analysis``.
                - ``"pdb"`` adds ``pdb_content`` (5-50KB).

                Unknown values raise ``ValueError`` client-side; the server
                also rejects them with HTTP 400.
        """
        if self._client is not None:
            self._client._require_paid_tier()
        try:
            id_int = int(peptide_id)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"peptide_id must be a positive integer (got {peptide_id!r})"
            ) from exc
        if id_int <= 0:
            raise ValueError(f"peptide_id must be > 0 (got {id_int})")

        params: dict[str, Any] = {}
        if include:
            unknown = [v for v in include if v not in _ALLOWED_INCLUDE]
            if unknown:
                raise ValueError(
                    f"Unknown include value(s): {unknown}. "
                    f"Allowed: {sorted(_ALLOWED_INCLUDE)}"
                )
            params["include"] = ",".join(include)

        payload = self._transport.request(
            "GET", f"/api/v1/peptides/{id_int}", params=params
        ) or {}
        return PeptideDetail.model_validate(payload)


    def estimate_cost(
        self,
        *,
        num_peptides: int,
        auto_fold: bool = True,
        fold_top_n: int | None = None,
        fold_trajectories: int = 4,
    ) -> CostEstimate:
        """``GET /api/billing/estimate`` — estimate the credit cost of a generation + folding job.

        Args:
            num_peptides: Number of peptides to generate.
            auto_fold: Whether folding will run automatically (default True).
            fold_top_n: Cap on peptides folded; when None the server uses its default.
            fold_trajectories: Diffusion samples per fold (default 4, matches Boltz-2 default).

        Returns:
            :class:`~ligandai.types.CostEstimate` with ``credits`` (int),
            ``cost_usd`` (float), and ``breakdown`` dict by phase
            (generation, folding, scoring).
        """
        params: dict[str, Any] = {
            "num_peptides": num_peptides,
            "auto_fold": auto_fold,
            "fold_trajectories": fold_trajectories,
        }
        if fold_top_n is not None:
            params["top_n"] = fold_top_n
        payload = (
            self._transport.request("GET", "/api/billing/estimate", params=params) or {}
        )
        return CostEstimate.model_validate(payload)

    def download_pdb(
        self,
        peptide_id: int | str,
        save_to: str | None = None,
    ) -> bytes:
        """``GET /api/v1/structures/{id}/pdb`` — download raw PDB content.

        Convenience helper that uses the ``pdb_url`` returned on every peptide
        in :meth:`list`, :meth:`search`, and :meth:`get`. Resolves to the same
        endpoint as ``client.structures.get_pdb(peptide_id)``.

        Args:
            peptide_id: ``ptf_fold_results.id`` (positive integer). Strings
                are accepted and parsed.
            save_to: Optional file path; when provided, writes the PDB bytes
                to disk and returns them. Parent directory must already
                exist.

        Returns:
            Raw PDB content as bytes.

        Tier behavior: free-tier keys receive a side-chain-scrambled PDB
        with a ``LIGANDAI FREE TIER — SIDE CHAIN IDENTITY REDACTED`` REMARK
        header. Paid tiers receive the original. The peptide row's
        ``_pdb_masked: True`` flag indicates when the response will be
        scrambled.
        """
        try:
            id_int = int(peptide_id)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"peptide_id must be a positive integer (got {peptide_id!r})"
            ) from exc
        path = f"/api/v1/structures/{id_int}/pdb"
        # Transport returns the raw bytes when the response is non-JSON.
        body = self._transport.request("GET", path)
        if isinstance(body, dict) and "pdb_content" in body:
            content = body.get("pdb_content") or ""
            data = content.encode("utf-8") if isinstance(content, str) else content
        elif isinstance(body, (bytes, bytearray)):
            data = bytes(body)
        elif isinstance(body, str):
            data = body.encode("utf-8")
        else:
            raise RuntimeError(f"Unexpected /v1/structures/{id_int}/pdb response shape: {type(body).__name__}")
        if save_to:
            import os
            os.makedirs(os.path.dirname(os.path.abspath(save_to)) or ".", exist_ok=True)
            with open(save_to, "wb") as f:
                f.write(data)
        return data


# -- Async resource ---------------------------------------------------------


class AsyncPeptides(AsyncResource):
    async def generate(
        self,
        gene: str,
        num_peptides: int | None = None,
        length_range: tuple[int, int] = (20, 70),
        # PDB residue numbers as numbered in the receptor structure — not indices
        # into any internal array. `chain` is honoured.
        target_residues: list[ResidueRange] | None = None,
        # Receptor regions to keep clear, same PDB numbering. Removed from the
        # featurization along with an 8 A CA-CA packing shell, in EVERY targeting
        # mode (no target_residues required). Measured NOT to change generation
        # or keep designs off the surface — establish avoidance on the fold.
        # See Peptides.generate docs.
        avoid_residues: list[ResidueRange] | None = None,
        # Flat list of PDB residue numbers, or {"chain","residue"} dicts — see
        # Peptides.generate docs. [bd-LIGANDAI_ALPHA_V2-x4vt8]
        hotspot_residues: list[int] | None = None,
        # Ca-Ca packing-shell radius (Angstrom) around avoid_residues. None =
        # server default (8.0 A); 0 disables. See Peptides.generate docs.
        avoid_shell_angstrom: float | None = None,
        target_chains: list[str] | None = None,
        fold_partners: _FoldPartnerMode | list[str] | None = None,
        # Run label only — not read by the generation backend. When
        # target_residues are supplied the SDK auto-switches this to
        # "pocket_targeted" unless you override here.
        targeting_strategy: _TargetingStrategy | None = None,
        # Receptor topology FACE override (bd-LIGANDAI_ALPHA_V2-q3z1b). For a
        # membrane receptor target, restrict the design pocket to a specific
        # topological face instead of the whole folded surface:
        #   "extracellular" → NC (non-cytoplasmic / extracellular) ranges only
        #   "ec_tm"         → NC + TM (the explicit form of the receptor default)
        #   "transmembrane" → TM ranges only
        #   "intracellular" → CY (cytoplasmic / intracellular) ranges only
        #   "full"          → no face restriction (whole surface)
        #   None (default)  → backend applies its receptor default
        #                     (extracellular + TM, excluding the intracellular face)
        # An extracellular peptide cannot reach the intracellular face in vivo,
        # so the default excludes IC; pass "intracellular" to override that for
        # cytoplasmic-delivery designs. The server resolves an explicit face to
        # the matching membrane-topology residue ranges and uses them as the
        # explicit-pocket target_residues.
        target_face: _TargetFace | None = None,
        # Hotspot pocket expansion radius in Angstroms. When target_residues
        # are provided, the server includes every residue within this radius
        # of any listed hotspot atom in the design pocket. This is the right
        # default for "design against residue X" — the peptide needs the
        # surrounding shell to make contacts, not just X itself. Set to 0 to
        # use the literal residues only (no expansion into the orthosteric site).
        # 0.0 is a valid, meaningful value — the server honors it as
        # "hotspots-only" regardless of how many residues surround the target.
        pocket_expansion_radius_a: float | None = 6.0,
        # Receptor flexible residues — 1-based receptor residue indices the
        # backend may relax (side-chain / limited backbone flexibility) during
        # generation/docking. None (default) keeps the receptor rigid.
        receptor_flexible_residues: Sequence[int] | None = None,
        auto_fold: bool = True,
        top_n_fold: int | None = None,
        ec_domain_trimming: bool = True,
        deimmunize_mode: bool = False,
        variant_id: int | None = None,
        user_pdb_id: int | None = None,
        gen_gpus: int = 1,
        fold_gpus: int = 1,
        program_id: int | str | None = None,
        conversation_id: str | None = None,
        cysteine_mode: _CysteineMode = "disulfide_only",
        # Tri-state switch for the cysteine kernel itself, independent of the
        # policy in ``cysteine_mode`` (bd-LIGANDAI_ALPHA_V2-b4f34). See the Args
        # section for why this exists and what it costs.
        cysteine_enabled: bool | None = None,
        quality_guided: bool = True,
        quality_guidance_scale: float = 1.0,
        # Generator model — see :meth:`Peptides.generate` for the per-model scope
        # statements. Sent as ``generatorModelId``; None (default) sends no key
        # so the server applies the platform default. All models currently
        # known to this SDK build support the four guidance heads; a future
        # model without guidance support raises LigandAIInvalidConfig
        # client-side rather than dropping the guidance silently.
        model: _GeneratorModel | str | None = None,
        immunogenicity: bool = False,
        immuno_strength: float = 2.0,
        immuno_modules: dict[str, bool] | None = None,
        serum_stability: bool = False,
        stability_strength: float = 2.0,
        stability_mode: _StabilityMode = "resist",
        stability_modules: dict[str, bool] | None = None,
        # Residue & terminal composition guidance — see :meth:`Peptides.generate`
        # for the full statement. Renamed from ``halflife`` in
        # bd-LIGANDAI_ALPHA_V2-b4f34; both spellings accepted, the legacy one
        # warns, the new one wins on conflict.
        residue_composition: _ResidueCompositionTarget | None = None,
        residue_composition_strength: float | None = None,
        halflife: _ResidueCompositionTarget | None = None,
        halflife_strength: float | None = None,
        # See :meth:`Peptides.generate` for the cellfree guidance docs, including
        # the 1.5-vs-2.0 cross-layer default mismatch.
        cellfree: bool = False,
        cellfree_strength: float = 1.5,
        charge_mode: _ChargeMode | None = None,
        charge_value: float | None = None,
        charge_min: float | None = None,
        charge_max: float | None = None,
        min_solubility: float | None = None,
        cyclic_mode: _CyclicMode | None = None,
        cyclic_strength: float = 2.0,
        strict_recombinant: bool = True,
        dual_fold_viz: bool = False,
        folding_mode: str | None = None,
        # Default fold strategy: rank candidates by composite (LigandIQ × predicted
        # iPTM) before folding. The top peptides by this score get fold GPUs first
        # so credits go to the most promising designs. Override to ``"distributed"``
        # for round-robin across targets, ``"consolidated"`` for sequential
        # single-target folding, or pass ``fold_strategy=None`` to defer to server.
        fold_strategy: str | None = "quality_ranked",
        folding_conformations: str | list[str] | None = None,
        max_folds_per_target: int | None = None,
        enable_expansion: bool | None = None,
        auto_conformation_expansion: bool | None = None,
        clash_resolution_enabled: bool | None = None,
        md_relaxation_enabled: bool | None = None,
        num_trajectories: int | None = None,
        sampling_steps: int | None = None,
        glycosylation_enabled: bool | None = None,
        segments: list[PeptideSegment] | list[dict] | None = None,
        segment_config: SegmentConfig | dict | None = None,
        pdc_config: PdcConfig | dict | None = None,
        ec_trimming_config: EcTrimmingConfig | dict | None = None,
        # Ensemble auto-fold (v0.6.7) — see :meth:`Peptides.generate` for docs.
        # Default None = legacy single-Boltz-2 auto-fold (unchanged). camelCase
        # aliases accepted; snake_case wins. Refs: bd-LIGANDAI_ALPHA_V2-7y1uh
        fold_engines: list[str] | None = None,
        fold_per_engine: dict[str, Any] | None = None,
        fold_shared_msa: str | dict[str, Any] | None = None,
        # Cascade auto-fold (v0.6.7) — see :meth:`Peptides.generate` for docs.
        # Refs: bd-LIGANDAI_ALPHA_V2-7y1uh
        fold_cascade: bool = False,
        cascade_gate_metric: str = "ipsae",
        cascade_gate_threshold: float | None = 0.67,
        cascade_gate_top_n: int | None = None,
        # ── Local (on-prem) GPU routing — SUPERADMIN ONLY (bd-d7b23) ────────
        local: bool = False,
        # ── Contrastive guidance + avoidance (bd-dre-if562) ─────────────────
        contrastive: "ContrastiveGuidance | None" = None,
        avoidance: "AvoidancePolicy | None" = None,
        allow_unforwarded_contrastive: bool = False,
        # Name the off-target genes yourself — the customer-reachable surface
        # for paralog-conditioned generation (bd-dre-siz9d). Sends
        # cfg_reference_genes (and cfgReferenceGenes) on the wire, restricting
        # the server-side paralog reference producer to these genes instead of
        # requiring the raw (entitlement-gated) pocket-feature width that
        # contrastive=ContrastiveGuidance(...) needs. Requires a CFG-capable
        # model (model='v6.9.21' or 'v6.9.22') — raises LigandAIValidationError
        # naming them otherwise. Mutually exclusive with contrastive=: they are
        # two ways of naming one panel, and preferring one silently is how a
        # caller ends up steered away from something they did not choose.
        # None (default): with a CFG-capable model, the platform still derives
        # and applies its own real paralog references — you are just not
        # choosing which ones.
        avoid_paralogs: _StrSequence[str] | None = None,
        foldEngines: list[str] | None = None,
        foldPerEngine: dict[str, Any] | None = None,
        foldSharedMsa: str | dict[str, Any] | None = None,
        foldCascade: bool | None = None,
        cascadeGateMetric: str | None = None,
        cascadeGateThreshold: float | None = None,
        cascadeGateTopN: int | None = None,
        **extra: Any,
    ) -> AsyncJob[GenerationResult]:
        """Async variant of :meth:`Peptides.generate`. See that method for full docs."""
        # Resolve camelCase aliases — snake_case wins when both are supplied.
        fold_engines = fold_engines if fold_engines is not None else foldEngines
        fold_per_engine = fold_per_engine if fold_per_engine is not None else foldPerEngine
        fold_shared_msa = fold_shared_msa if fold_shared_msa is not None else foldSharedMsa
        # Cascade aliases — snake wins (mirrors the sync path).
        if not fold_cascade and foldCascade is not None:
            fold_cascade = bool(foldCascade)
        if cascade_gate_metric == "ipsae" and cascadeGateMetric is not None:
            cascade_gate_metric = cascadeGateMetric
        if cascade_gate_threshold == 0.67 and cascadeGateThreshold is not None:
            cascade_gate_threshold = cascadeGateThreshold
        if cascade_gate_top_n is None and cascadeGateTopN is not None:
            cascade_gate_top_n = cascadeGateTopN
        # Resolve the residue-composition knob from its canonical and legacy
        # names BEFORE the tier gate, so a caller using the deprecated spelling
        # is gated identically to one using the new one.
        (
            residue_composition,
            resolved_rc_strength,
            rc_alias_used,
        ) = _resolve_residue_composition(
            residue_composition=residue_composition,
            residue_composition_strength=residue_composition_strength,
            halflife=halflife,
            halflife_strength=halflife_strength,
        )
        # Each warning is gated on its OWN head being enabled. immuno_strength
        # defaults to 2.0 against a 1.5 ceiling, so an ungated check would warn
        # on every call — including calls with immuno guidance switched off.
        _warn_strength_out_of_band(
            "immuno_strength", immuno_strength, head_enabled=immunogenicity
        )
        _warn_strength_out_of_band(
            "stability_strength", stability_strength, head_enabled=serum_stability
        )
        _warn_strength_out_of_band(
            "cellfree_strength", cellfree_strength, head_enabled=cellfree
        )
        if self._client is not None:
            self._client._require_feature("generate_peptides")
            if _requests_advanced_guidance(
                immunogenicity=immunogenicity,
                immuno_modules=immuno_modules,
                serum_stability=serum_stability,
                stability_modules=stability_modules,
                residue_composition=residue_composition,
                cellfree=cellfree,
                cyclic_mode=cyclic_mode,
                extra=extra,
            ):
                self._client._require_feature("advanced_guidance")
        # v0.2.0: cys/cyclic controls passed via extra={...} are deprecated;
        # use the typed kwargs above. Hard-rejected in v0.3.0.
        _warn_deprecated_cys_extra(extra)
        # ⛔ P0 REUSE GUARD (bd-dre-1u72b). Popped BEFORE the body is built so it
        # never reaches the wire — it is a client-side policy switch, not a
        # server field. See _guard_reused_session() for what it protects against.
        _allow_reused_session = bool(extra.pop("allow_reused_session", False))
        _reuse_guidance_requested = _guidance_was_requested({
            "immunogenicity": immunogenicity,
            "serum_stability": serum_stability,
            "cellfree": cellfree,
            "residue_composition": residue_composition,
            "halflife": halflife,
            "cysteine_enabled": cysteine_enabled,
        })
        effective_segment_config = _resolve_segment_config(segments, segment_config)
        # An AvoidancePolicy is a convenience wrapper over the two avoidance
        # kwargs plus the off-target/hard-negative bookkeeping the campaign loop
        # needs. Explicit kwargs WIN — a caller who passed both meant the
        # explicit one, and silently preferring the policy would be the kind of
        # invisible override this SDK refuses to ship.
        avoid_residues, avoid_shell_angstrom = _merge_avoidance(
            avoidance, avoid_residues, avoid_shell_angstrom
        )
        body = _generation_body(
            gene=gene,
            num_peptides=num_peptides,
            length_range=length_range,
            target_residues=target_residues,
            avoid_residues=avoid_residues,
            hotspot_residues=hotspot_residues,
            avoid_shell_angstrom=avoid_shell_angstrom,
            target_chains=target_chains,
            fold_partners=fold_partners,
            targeting_strategy=targeting_strategy,
            target_face=target_face,
            pocket_expansion_radius_a=pocket_expansion_radius_a,
            receptor_flexible_residues=receptor_flexible_residues,
            auto_fold=auto_fold,
            top_n_fold=top_n_fold,
            ec_domain_trimming=ec_domain_trimming,
            deimmunize_mode=deimmunize_mode,
            variant_id=variant_id,
            user_pdb_id=user_pdb_id,
            gen_gpus=gen_gpus,
            fold_gpus=fold_gpus,
            program_id=program_id,
            conversation_id=conversation_id,
            cysteine_mode=cysteine_mode,
            cysteine_enabled=cysteine_enabled,
            quality_guided=quality_guided,
            quality_guidance_scale=quality_guidance_scale,
            model=model,
            immunogenicity=immunogenicity,
            immuno_strength=immuno_strength,
            immuno_modules=immuno_modules,
            serum_stability=serum_stability,
            stability_strength=stability_strength,
            stability_mode=stability_mode,
            stability_modules=stability_modules,
            residue_composition=residue_composition,
            residue_composition_strength=resolved_rc_strength,
            residue_composition_alias_used=rc_alias_used,
            cellfree=cellfree,
            cellfree_strength=cellfree_strength,
            charge_mode=charge_mode,
            charge_value=charge_value,
            charge_min=charge_min,
            charge_max=charge_max,
            min_solubility=min_solubility,
            cyclic_mode=cyclic_mode,
            cyclic_strength=cyclic_strength,
            strict_recombinant=strict_recombinant,
            dual_fold_viz=dual_fold_viz,
            folding_mode=folding_mode,
            fold_strategy=fold_strategy,
            folding_conformations=folding_conformations,
            max_folds_per_target=max_folds_per_target,
            enable_expansion=enable_expansion,
            auto_conformation_expansion=auto_conformation_expansion,
            clash_resolution_enabled=clash_resolution_enabled,
            md_relaxation_enabled=md_relaxation_enabled,
            num_trajectories=num_trajectories,
            sampling_steps=sampling_steps,
            glycosylation_enabled=glycosylation_enabled,
            segment_config=effective_segment_config,
            pdc_config=pdc_config,
            ec_trimming_config=ec_trimming_config,
            fold_engines=fold_engines,
            fold_per_engine=fold_per_engine,
            fold_shared_msa=fold_shared_msa,
            fold_cascade=fold_cascade,
            cascade_gate_metric=cascade_gate_metric,
            cascade_gate_threshold=cascade_gate_threshold,
            cascade_gate_top_n=cascade_gate_top_n,
            contrastive=contrastive,
            allow_unforwarded_contrastive=allow_unforwarded_contrastive,
            avoid_paralogs=avoid_paralogs,
            local=local,
            extra=extra,
        )
        payload = await self._transport.request("POST", "/api/ptf/parallel/generate", json=body) or {}
        _guard_reused_session(
            payload,
            requested_model=model,
            guidance_requested=_reuse_guidance_requested,
            allow_reused_session=_allow_reused_session,
        )
        job_id = payload.get("sessionId") or payload.get("jobId") or payload.get("session_id") or ""
        if not job_id:
            raise LigandAIError("Server did not return a session_id/jobId", response=payload)
        return AsyncJob(
            self._transport,
            job_id,
            job_type="generation",
            parser=_parse_generation,
            status_path="/api/ptf/parallel/{job_id}/status",
            cancel_path="/api/ptf/parallel/{job_id}/cancel",
            sse_path="/api/ptf/parallel/{job_id}/stream",
            initial={"id": job_id, "type": "generation", "status": "queued", **payload},
            result_loader=lambda info: _aload_generation_result(
                self._transport,
                info,
                fallback_session_id=job_id,
                fallback_gene=gene,
            ),
        )

    async def generate_multi(
        self,
        targets: Iterable[_MultiTargetSpec],
        num_peptides: int | None = None,
        *,
        avoid_paralogs: _StrSequence[str] | None = None,
        **kwargs: Any,
    ) -> AsyncJob[GenerationResult]:
        """Generate against **N receptors in one concurrent run**.

        One request, one session, one bill, N targets generated concurrently by
        the platform orchestrator. Every non-targeting setting — generator
        ``model``, guidance heads, ``length_range``, ``num_peptides``, fold
        settings — is SESSION-level and applies to every target, because that is
        how ``POST /api/ptf/parallel/generate`` reads them (it destructures
        ``generatorModelId`` and the guidance flags once, off the request root,
        never off an individual target). Per-target you may vary only the
        TARGETING: see :data:`_PER_TARGET_KEYS`.

        Args:
            targets: Gene symbols and/or per-target mappings. A bare string is a
                whole-surface target; a mapping may carry ``gene``,
                ``target_residues``, ``avoid_residues``, ``hotspot_residues``,
                ``target_chains``, ``targeting_strategy``, ``variant_id``,
                ``user_pdb_id``, ``uniprot_id``, ``pdb_id``, ``pdb_data``.
            num_peptides: Peptides **per target** (not the total). The bill and
                the volume discount are computed on ``len(targets) *
                num_peptides`` — see :func:`estimate_multi_cost`.
            avoid_paralogs: Name the off-target genes to steer every target's
                generation away from, session-wide — see :meth:`generate` for
                the full contract (CFG-capable model required, mutually
                exclusive with ``contrastive=``).
            **kwargs: Every other :meth:`generate` keyword, applied session-wide.

        Returns:
            The same waitable :class:`~ligandai.jobs.AsyncJob` :meth:`generate`
            returns. ``job.wait()`` resolves once EVERY target is done;
            ``job.target_status()`` reports per-target progress while it runs and
            ``job.peptides_by_target()`` splits the finished peptides by gene.

        Raises:
            LigandAIError: empty target list, a blank or duplicated gene, or a
                session-level setting passed inside a per-target mapping.
            LigandAIReusedSessionError: the platform answered with a PRE-EXISTING
                session rather than running this request (see that class — its
                reuse key ignores the generator model and every guidance flag).

        Example::

            job = await client.peptides.generate_multi(
                targets=["CD274", "EGFR", {"gene": "ERBB2", "hotspot_residues": [310]}],
                num_peptides=100,
                model="v6.9.22",
                immunogenicity=True,
            )
            for gene, state in (await job.target_status()).items():
                print(gene, state["status"], state["peptidesGenerated"])
            await job.wait()
            by_gene = await job.peptides_by_target()

        ⛔ TIER CAPS ARE THE SERVER'S: free 3 simultaneous targets, basic /
        academia / pro 5, enterprise / discovery_partner 25 — enforced in the
        handler, which 400s with ``TIER_TARGET_LIMIT_EXCEEDED``. This method does
        not keep a copy of that table, so a tier change reaches you without an
        SDK upgrade.
        """
        wire_targets = _normalize_multi_targets(targets)
        # The first gene seeds generate()'s single-target scaffolding (session
        # fallback gene, targeting auto-strategy). `targets=` then overrides the
        # one-element array _generation_body built, because **extra is merged
        # into the body LAST. Nothing about the single-target path changes.
        first_gene = str(wire_targets[0]["gene"])
        kwargs.pop("gene", None)
        job = await self.generate(
            gene=first_gene,
            num_peptides=num_peptides,
            targets=wire_targets,
            avoid_paralogs=avoid_paralogs,
            **kwargs,
        )
        _attach_multi_target_helpers(job, [str(t["gene"]) for t in wire_targets])
        return job


    def reattach(self, job_id: str) -> AsyncJob[GenerationResult]:
        """Async sibling of :meth:`Peptides.reattach`.

        Returns a waitable :class:`~ligandai.jobs.AsyncJob` bound to the
        parallel-generation status path so a disconnected caller holding only
        ``job_id`` can ``await client.peptides.reattach(job_id).wait()``.
        """
        if not job_id:
            raise LigandAIError("reattach(job_id) requires a non-empty job_id")
        return AsyncJob(
            self._transport,
            job_id,
            job_type="generation",
            parser=_parse_generation,
            status_path="/api/ptf/parallel/{job_id}/status",
            cancel_path="/api/ptf/parallel/{job_id}/cancel",
            sse_path="/api/ptf/parallel/{job_id}/stream",
            initial={"id": job_id, "type": "generation", "status": "queued"},
            result_loader=lambda info: _aload_generation_result(
                self._transport,
                info,
                fallback_session_id=job_id,
                fallback_gene=None,
            ),
        )

    async def fold(
        self,
        sequences: list[Sequence | str | dict[str, Any]],
        target_gene: str | None = None,
        auto_score: bool = True,
        template_mode: bool = False,
        msa_enabled: bool | None = None,
        glycosylation: bool | None = None,
        pegylation: bool | None = None,
        gpu_count: int = 1,
        diffusion_samples: int = 1,
        sampling_steps: int | None = None,
        recycling_steps: int | None = None,
        num_trajectories: int | None = None,
        step_scale: float | None = None,
        contribute_to_receptordb: bool | None = None,
        n_parallel_gpus: int | None = None,
        # Hardening kwargs
        gpu: str | None = None,
        force_resubmit: bool = False,
        # ESMFold2 + approach selection
        fold_approach: str | None = None,
        num_seeds: int | None = None,
        num_recycles: int | None = None,
        return_pdb: bool | None = None,
        use_potentials: bool | None = True,
    ) -> AsyncJob[FoldResult]:
        """Async sibling of :meth:`Peptides.fold`. See :meth:`Peptides.fold` for
        ``n_parallel_gpus`` semantics and the GPU / dedupe / credit pre-flight
        hardening (gpu='b200_plus' only, 24h local dedupe).

        ⛔ FLAG YOUR DESIGNED CHAINS. A ``protein`` chain with no peptide flag is
        folded as a RECEPTOR, and a receptor is entitled to a multiple-sequence
        alignment. A LigandForge design — or any de novo binder — has no
        homologs, so that MSA search can only come back degenerate, and when the
        MSA service cannot answer the fold is REFUSED for an alignment that
        cannot exist. Mark every designed chain with ``is_peptide=True`` (or
        ``use_msa=False``, or ``role="peptide"`` — any one is enough); the
        receptor chain keeps its MSA::

            job = client.peptides.fold(
                sequences=[
                    {"chainId": "A", "sequence": BRD4_ET_DOMAIN, "geneName": "BRD4"},
                    {"chainId": "B", "sequence": DESIGNED_BINDER, "is_peptide": True},
                ],
                target_gene="BRD4",
                diffusion_samples=4,
            )

        Chains may be plain strings, dicts as above, or
        :class:`~ligandai.types.Sequence` objects
        (``Sequence(sequence=..., is_peptide=True)``). For the common two-chain
        case ``client.fold(receptor_seq, binder_seq)`` flags chain B for you.
        """
        if self._client is not None:
            self._client._require_feature("predict_structure")
        # Hardening pre-flight
        from ligandai._hardening import (
            attach_job_id,
            build_fold_params_for_hash,
            dedupe_lookup_cached,
            enforce_concurrency,
            estimate_single_fold_credits,
            mark_failed,
            preflight_credits,
            receptor_seq_for_hash,
            record_submission,
            validate_gpu,
        )
        from ligandai._dedupe import compute_submission_hash

        canonical_gpu = validate_gpu(gpu)
        submitted_set = self._client.submitted_set if self._client is not None else None
        credit_ledger = self._client.credit_ledger if self._client is not None else None
        api_key_hash = self._client.api_key_hash if self._client is not None else ""

        normalized_seqs = [_norm_seq(s) for s in sequences]
        seq_strings = [str(s.get("sequence", "")) for s in normalized_seqs]
        if len(seq_strings) >= 2:
            rec_for_hash = receptor_seq_for_hash(
                target_gene=target_gene,
                receptor_sequence=seq_strings[0],
                receptor_pdb=None,
            )
            peptide_for_hash: str | list[str] = "|".join(seq_strings[1:])
        else:
            rec_for_hash = receptor_seq_for_hash(
                target_gene=target_gene,
                receptor_sequence=seq_strings[0] if seq_strings else None,
                receptor_pdb=None,
            )
            peptide_for_hash = ""

        sub_hash = compute_submission_hash(
            peptide_seq=peptide_for_hash,
            receptor_seq=rec_for_hash,
            gpu=canonical_gpu,
            params=build_fold_params_for_hash(
                target_gene=target_gene,
                diffusion_samples=(
                    num_trajectories if num_trajectories is not None
                    else diffusion_samples
                ),
                sampling_steps=sampling_steps,
                recycling_steps=recycling_steps,
                step_scale=step_scale,
                msa_enabled=msa_enabled,
                glycosylation=glycosylation,
                template_mode=template_mode,
                extra={
                    "kind": "fold",
                    "pegylation": bool(pegylation) if pegylation is not None else None,
                    # Only include the potentials key when ON, so a default (off)
                    # fold hashes identically to pre-feature submissions (dedupe-stable).
                    **({"use_potentials": bool(use_potentials)} if use_potentials is not None else {}),
                },
            ),
        )

        enforce_concurrency(self._client, submitted_set)

        cached = dedupe_lookup_cached(
            submitted_set,
            submission_hash=sub_hash,
            api_key_hash=api_key_hash,
            force_resubmit=force_resubmit,
        )
        if cached and cached.get("job_id"):
            cached_job_id = str(cached["job_id"])
            return AsyncJob(
                self._transport,
                cached_job_id,
                job_type="folding",
                parser=_parse_fold,
                status_path="/api/folding/jobs/{job_id}",
                cancel_path="/api/folding/jobs/{job_id}",
                sse_path="/api/folding/jobs/{job_id}/logs/stream",
                initial={"id": cached_job_id, "type": "folding", "status": "cached"},
            )

        effective_trajectories = int(
            num_trajectories if num_trajectories is not None else diffusion_samples
        )
        estimated = estimate_single_fold_credits(
            trajectories=effective_trajectories,
            sampling_steps=sampling_steps,
        )
        balance_before, _ = preflight_credits(
            self._client, estimated=estimated, kind="fold",
        )

        body = _fold_body(
            sequences,
            auto_score=auto_score,
            template_mode=template_mode,
            msa_enabled=msa_enabled,
            target_gene=target_gene,
            glycosylation=glycosylation,
            pegylation=pegylation,
            gpu_count=gpu_count,
            diffusion_samples=diffusion_samples,
            sampling_steps=sampling_steps,
            recycling_steps=recycling_steps,
            num_trajectories=num_trajectories,
            step_scale=step_scale,
            contribute_to_receptordb=contribute_to_receptordb,
            n_parallel_gpus=n_parallel_gpus,
            fold_approach=fold_approach,
            num_seeds=num_seeds,
            num_recycles=num_recycles,
            return_pdb=return_pdb,
            use_potentials=use_potentials,
        )

        record_submission(
            submitted_set,
            credit_ledger,
            submission_hash=sub_hash,
            api_key_hash=api_key_hash,
            kind="fold",
            gpu=canonical_gpu,
            estimated_credits=estimated,
            balance_before=balance_before,
            meta={"target_gene": target_gene, "chains": len(seq_strings)},
        )

        try:
            payload = await self._transport.request("POST", "/api/folding/predict", json=body) or {}
        except Exception as exc:
            mark_failed(
                submitted_set, submission_hash=sub_hash,
                api_key_hash=api_key_hash, reason=type(exc).__name__,
            )
            raise

        job_id = payload.get("jobId") or payload.get("id") or ""
        if not job_id:
            mark_failed(
                submitted_set, submission_hash=sub_hash,
                api_key_hash=api_key_hash, reason="server_no_job_id",
            )
            raise LigandAIError("Server did not return a jobId for fold", response=payload)
        attach_job_id(
            submitted_set, submission_hash=sub_hash,
            api_key_hash=api_key_hash, job_id=job_id,
        )
        return AsyncJob(
            self._transport,
            job_id,
            job_type="folding",
            parser=_parse_fold,
            status_path="/api/folding/jobs/{job_id}",
            cancel_path="/api/folding/jobs/{job_id}",
            sse_path="/api/folding/jobs/{job_id}/logs/stream",
            initial={"id": job_id, "type": "folding", "status": "queued", **payload},
        )

    async def cofold(
        self,
        chains: list[Sequence | str | dict[str, Any]],
        engines: list[str] = ["boltz2"],
        *,
        per_engine: dict[str, Any] | None = None,
        shared_msa: str | dict[str, Any] | None = None,
        run_deltaforge: bool = True,
        length_bin: str | None = None,
        gene: str | None = None,
        include_structures: bool = False,
        audit_id: str | None = None,
        # camelCase aliases (accepted; snake_case wins on conflict)
        perEngine: dict[str, Any] | None = None,
        sharedMsa: str | dict[str, Any] | None = None,
        runDeltaforge: bool | None = None,
        lengthBin: str | None = None,
        includeStructures: bool | None = None,
        auditId: str | None = None,
        cofactors: list[Any] | None = None,
        allow_cofactor_absent: bool = False,
    ) -> "AsyncJob[dict[str, Any]]":
        """Async ensemble co-fold — see :meth:`Peptides.cofold`.

        Free-pays-credits (any tier with a positive balance; server reserves the
        per-engine estimate and 402s a zero-balance caller). MSA is a single
        shared control (``shared_msa``); per-engine MSA keys are rejected
        client-side. Refs: bd-LIGANDAI_ALPHA_V2-7wfnp
        """
        per_engine = per_engine if per_engine is not None else perEngine
        shared_msa = shared_msa if shared_msa is not None else sharedMsa
        if runDeltaforge is not None and run_deltaforge is True:
            run_deltaforge = runDeltaforge
        length_bin = length_bin if length_bin is not None else lengthBin
        if includeStructures is not None and include_structures is False:
            include_structures = includeStructures
        audit_id = audit_id if audit_id is not None else auditId

        # Folding is free-pays-credits — server (gpuSubmitGuard) enforces the
        # credit balance; no client-side tier gate. Refs: bd-LIGANDAI_ALPHA_V2-7wfnp

        norm_engines = _cofold_normalize_engines(engines)
        norm_chains = _cofold_chains(chains)
        clean_per_engine = _cofold_sanitize_per_engine(per_engine)

        if shared_msa is None:
            shared_msa_spec: dict[str, Any] = {"mode": "auto"}
        elif isinstance(shared_msa, str):
            mode = shared_msa.strip().lower()
            if mode not in ("auto", "none"):
                raise ValueError("shared_msa must be 'auto' or 'none'")
            shared_msa_spec = {"mode": mode}
        elif isinstance(shared_msa, dict):
            mode = str(shared_msa.get("mode", "auto")).strip().lower()
            if mode not in ("auto", "none"):
                raise ValueError("shared_msa.mode must be 'auto' or 'none'")
            shared_msa_spec = {"mode": mode}
        else:
            raise TypeError("shared_msa must be a str ('auto'|'none') or a dict")

        body: dict[str, Any] = {
            "chains": norm_chains,
            "engines": norm_engines,
            "shared_msa": shared_msa_spec,
            "run_deltaforge": bool(run_deltaforge),
            "include_structures": bool(include_structures),
        }
        if clean_per_engine:
            body["per_engine"] = clean_per_engine
        if length_bin is not None:
            body["length_bin"] = length_bin
        if gene is not None:
            body["gene"] = gene
        if audit_id is not None:
            body["audit_id"] = audit_id
        if cofactors:
            body["cofactors"] = _cofold_cofactors(cofactors)
        if allow_cofactor_absent:
            body["allow_cofactor_absent"] = True

        payload = await self._transport.request("POST", "/api/v1/cofold/start", json=body) or {}
        job_id = payload.get("job_id") or payload.get("jobId") or payload.get("id") or ""
        if not job_id:
            raise LigandAIError(
                "Server did not return a job_id for cofold", response=payload
            )
        return AsyncJob(
            self._transport,
            str(job_id),
            job_type="cofold",
            parser=_parse_cofold,
            status_path="/api/v1/cofold/{job_id}/status",
            cancel_path="/api/v1/cofold/{job_id}/cancel",
            sse_path="/api/v1/cofold/{job_id}/stream",
            initial={"id": str(job_id), "type": "cofold", "status": "queued", **payload},
        )

    async def fold_batch(
        self,
        peptides: list[str] | None = None,
        *,
        groups: list[dict[str, Any]] | None = None,
        target_gene: str | None = None,
        receptor_pdb: str | None = None,
        receptor_sequence: str | None = None,
        receptor_name: str | None = None,
        # bd-dre-h0cuo.1 — arbitrary-seq MSA:
        #   msa_source_gene / parent_gene: derive a receptor_sequence's MSA from
        #     a homolog-rich parent gene's cached MSA (column-subset on substring
        #     match, align otherwise) instead of a fresh fragment search.
        #   gene_range: with target_gene=, fold a 1-based inclusive residue range
        #     (a domain) of the gene using the gene's MSA sliced to those columns.
        msa_source_gene: str | None = None,
        parent_gene: str | None = None,
        gene_range: tuple[int, int] | None = None,
        diffusion_samples: int = 1,
        sampling_steps: int = 50,
        recycling_steps: int | None = None,
        step_scale: float | None = None,
        msa_enabled: bool | None = None,
        glycosylation: bool | None = None,
        template_mode: bool = False,
        n_parallel_gpus: int | None = None,
        session_id: str | None = None,
        contribute_to_receptordb: bool | None = None,
        num_trajectories: int | None = None,
        msa_depth: int | None = None,
        use_potentials: bool | None = True,
        # [bd-LIGANDAI_ALPHA_V2-pybsy] Boltz-2 --seed. `boltz predict --seed N` is a
        # GLOBAL CLI option: one invocation per seed, never a per-row field. Omit for
        # the previous (unseeded) behaviour. To replicate, submit the SAME batch once
        # per seed and treat each seed's median over its diffusion samples as ONE
        # observation — never pool samples across seeds as independent replicates.
        seed: int | None = None,
        on_credit_exhausted: Callable[[LigandAICreditError, dict[str, Any]], bool] | None = None,
        # ─── hardening kwargs ────────────────────
        gpu: str | None = None,
        force_resubmit: bool = False,
    ) -> AsyncBatchFoldJob:
        """Async variant of :meth:`Peptides.fold_batch`.

        Supports both the flat ``peptides=`` + receptor form and the grouped
        ``groups=`` ``target{partners}`` form with per-entity MSA control. See
        :meth:`Peptides.fold_batch` for the full contract (receptor modes, FASTA
        support, the grouped API, billing, the ``on_credit_exhausted`` callback,
        and the submission hardening).
        """
        if self._client is not None:
            self._client._require_feature("predict_structure")
        # Form selection: flat (peptides=) XOR grouped (groups=).
        if (peptides is None) == (groups is None):
            raise ValueError(
                "Pass exactly one of peptides=[...] (flat: N peptides vs one "
                "receptor) or groups=[...] (grouped: target{partners} complexes "
                "with per-entity MSA control)."
            )
        if groups is not None:
            return await self._fold_batch_grouped(
                groups=groups,
                diffusion_samples=diffusion_samples,
                sampling_steps=sampling_steps,
                recycling_steps=recycling_steps,
                step_scale=step_scale,
                glycosylation=glycosylation,
                template_mode=template_mode,
                n_parallel_gpus=n_parallel_gpus,
                session_id=session_id,
                contribute_to_receptordb=contribute_to_receptordb,
                num_trajectories=num_trajectories,
                msa_depth=msa_depth,
                use_potentials=use_potentials,
                seed=seed,
                on_credit_exhausted=on_credit_exhausted,
                gpu=gpu,
                force_resubmit=force_resubmit,
            )
        # Hardening pre-flight
        from ligandai._hardening import (
            attach_job_id,
            build_fold_params_for_hash,
            dedupe_lookup_cached,
            enforce_concurrency,
            estimate_fold_batch_credits,
            mark_failed,
            preflight_credits,
            receptor_seq_for_hash,
            record_submission,
            validate_gpu,
        )
        from ligandai._dedupe import compute_submission_hash

        canonical_gpu = validate_gpu(gpu)
        submitted_set = self._client.submitted_set if self._client is not None else None
        credit_ledger = self._client.credit_ledger if self._client is not None else None
        api_key_hash = self._client.api_key_hash if self._client is not None else ""

        rec_for_hash = receptor_seq_for_hash(
            target_gene=target_gene,
            receptor_sequence=receptor_sequence,
            receptor_pdb=receptor_pdb,
        )
        sub_hash = compute_submission_hash(
            peptide_seq=peptides,
            receptor_seq=rec_for_hash,
            gpu=canonical_gpu,
            params=build_fold_params_for_hash(
                target_gene=target_gene,
                diffusion_samples=diffusion_samples,
                sampling_steps=sampling_steps,
                recycling_steps=recycling_steps,
                step_scale=step_scale,
                msa_enabled=msa_enabled,
                glycosylation=glycosylation,
                template_mode=template_mode,
                extra={
                    "kind": "fold_batch",
                    "receptor_name": receptor_name,
                    # Only present when ON so default batches hash unchanged.
                    **({"use_potentials": bool(use_potentials)} if use_potentials is not None else {}),
                    **({"msa_depth": int(msa_depth)} if msa_depth is not None else {}),
                },
            ),
        )

        enforce_concurrency(self._client, submitted_set)

        cached = dedupe_lookup_cached(
            submitted_set,
            submission_hash=sub_hash,
            api_key_hash=api_key_hash,
            force_resubmit=force_resubmit,
        )
        if cached and cached.get("job_id"):
            cached_batch_id = str(cached["job_id"])
            return AsyncBatchFoldJob(
                self._transport,
                batch_id=cached_batch_id,
                jobs=[],
                total_cost_credits=int(cached.get("estimated_credits") or 0),
                peptide_count=len(peptides),
                trajectories_per_peptide=int(diffusion_samples),
                receptor={"source": "cached"},
                sampling_steps=int(sampling_steps),
            )

        estimated = estimate_fold_batch_credits(
            peptide_count=len(peptides),
            trajectories=int(diffusion_samples),
            sampling_steps=int(sampling_steps),
        )
        balance_before, _ = preflight_credits(
            self._client, estimated=estimated, kind="fold_batch",
        )

        # parent_gene is an alias for msa_source_gene; reject conflicting values.
        _effective_msa_gene = msa_source_gene if msa_source_gene is not None else parent_gene
        if msa_source_gene is not None and parent_gene is not None and msa_source_gene != parent_gene:
            raise ValueError("Pass only one of msa_source_gene= or parent_gene= (they are aliases)")

        body = _build_batch_fold_body(
            peptides=peptides,
            target_gene=target_gene,
            receptor_pdb=receptor_pdb,
            receptor_sequence=receptor_sequence,
            receptor_name=receptor_name,
            diffusion_samples=diffusion_samples,
            sampling_steps=sampling_steps,
            recycling_steps=recycling_steps,
            step_scale=step_scale,
            msa_enabled=msa_enabled,
            glycosylation=glycosylation,
            template_mode=template_mode,
            n_parallel_gpus=n_parallel_gpus,
            session_id=session_id,
            contribute_to_receptordb=contribute_to_receptordb,
            num_trajectories=num_trajectories,
            msa_depth=msa_depth,
            use_potentials=use_potentials,
            msa_source_gene=_effective_msa_gene,
            gene_range=gene_range,
            seed=seed,
        )

        record_submission(
            submitted_set,
            credit_ledger,
            submission_hash=sub_hash,
            api_key_hash=api_key_hash,
            kind="fold_batch",
            gpu=canonical_gpu,
            estimated_credits=estimated,
            balance_before=balance_before,
            meta={
                "peptide_count": len(peptides),
                "target_gene": target_gene,
                "receptor_name": receptor_name,
                "trajectories": int(diffusion_samples),
                "sampling_steps": int(sampling_steps),
            },
        )

        # mirror the sync graceful-credit retry.
        try:
            payload = await self._transport.request("POST", "/api/v1/folding/predict-batch", json=body) or {}
        except LigandAICreditError as cred_err:
            if on_credit_exhausted is None:
                mark_failed(
                    submitted_set, submission_hash=sub_hash,
                    api_key_hash=api_key_hash, reason="credit_exhausted",
                )
                raise
            try:
                should_retry = bool(on_credit_exhausted(cred_err, body))
            except Exception:
                mark_failed(
                    submitted_set, submission_hash=sub_hash,
                    api_key_hash=api_key_hash, reason="credit_exhausted_callback_error",
                )
                raise cred_err from None
            if not should_retry:
                mark_failed(
                    submitted_set, submission_hash=sub_hash,
                    api_key_hash=api_key_hash, reason="credit_exhausted_no_retry",
                )
                raise
            payload = await self._transport.request("POST", "/api/v1/folding/predict-batch", json=body) or {}
        except Exception as exc:
            mark_failed(
                submitted_set, submission_hash=sub_hash,
                api_key_hash=api_key_hash, reason=type(exc).__name__,
            )
            raise
        batch_id = payload.get("batch_id") or payload.get("batchId") or ""
        if not batch_id:
            mark_failed(
                submitted_set, submission_hash=sub_hash,
                api_key_hash=api_key_hash, reason="server_no_batch_id",
            )
            raise LigandAIError("Server did not return a batch_id for fold_batch", response=payload)
        attach_job_id(
            submitted_set, submission_hash=sub_hash,
            api_key_hash=api_key_hash, job_id=batch_id,
        )
        return AsyncBatchFoldJob(
            self._transport,
            batch_id=batch_id,
            jobs=payload.get("jobs") or [],
            total_cost_credits=int(payload.get("total_cost_credits") or 0),
            peptide_count=int(payload.get("peptide_count") or 0),
            trajectories_per_peptide=int(payload.get("trajectories_per_peptide") or diffusion_samples),
            receptor=payload.get("receptor"),
            sampling_steps=int(payload.get("sampling_steps") or sampling_steps),
        )

    async def _fold_batch_grouped(
        self,
        *,
        groups: list[dict[str, Any]],
        diffusion_samples: int,
        sampling_steps: int,
        recycling_steps: int | None,
        step_scale: float | None,
        glycosylation: bool | None,
        template_mode: bool,
        n_parallel_gpus: int | None,
        session_id: str | None,
        contribute_to_receptordb: bool | None,
        num_trajectories: int | None,
        msa_depth: int | None,
        use_potentials: bool | None,
        on_credit_exhausted: Callable[[LigandAICreditError, dict[str, Any]], bool] | None,
        gpu: str | None,
        force_resubmit: bool,
    # [bd-LIGANDAI_ALPHA_V2-pybsy] Boltz-2 --seed (grouped form).
    seed: int | None = None,
    ) -> AsyncBatchFoldJob:
        """Async grouped fold_batch submission. See :meth:`Peptides._fold_batch_grouped`."""
        from ligandai._hardening import (
            attach_job_id,
            build_fold_params_for_hash,
            dedupe_lookup_cached,
            enforce_concurrency,
            estimate_fold_batch_credits,
            mark_failed,
            preflight_credits,
            record_submission,
            validate_gpu,
        )
        from ligandai._dedupe import compute_submission_hash

        canonical_gpu = validate_gpu(gpu)
        submitted_set = self._client.submitted_set if self._client is not None else None
        credit_ledger = self._client.credit_ledger if self._client is not None else None
        api_key_hash = self._client.api_key_hash if self._client is not None else ""

        body = _build_grouped_batch_fold_body(
            groups=groups,
            diffusion_samples=diffusion_samples,
            sampling_steps=sampling_steps,
            recycling_steps=recycling_steps,
            step_scale=step_scale,
            glycosylation=glycosylation,
            template_mode=template_mode,
            n_parallel_gpus=n_parallel_gpus,
            session_id=session_id,
            contribute_to_receptordb=contribute_to_receptordb,
            num_trajectories=num_trajectories,
            msa_depth=msa_depth,
            use_potentials=use_potentials,
            seed=seed,
        )
        normalized_groups = body["groups"]
        group_count = len(normalized_groups)

        import json as _json
        group_signature = _json.dumps(normalized_groups, sort_keys=True)
        sub_hash = compute_submission_hash(
            peptide_seq=[group_signature],
            receptor_seq="grouped_batch_fold",
            gpu=canonical_gpu,
            params=build_fold_params_for_hash(
                target_gene=None,
                diffusion_samples=diffusion_samples,
                sampling_steps=sampling_steps,
                recycling_steps=recycling_steps,
                step_scale=step_scale,
                msa_enabled=None,
                glycosylation=glycosylation,
                template_mode=template_mode,
                extra={
                    "kind": "fold_batch_grouped",
                    "group_count": group_count,
                    **({"use_potentials": bool(use_potentials)} if use_potentials is not None else {}),
                    **({"msa_depth": int(msa_depth)} if msa_depth is not None else {}),
                },
            ),
        )

        enforce_concurrency(self._client, submitted_set)

        cached = dedupe_lookup_cached(
            submitted_set,
            submission_hash=sub_hash,
            api_key_hash=api_key_hash,
            force_resubmit=force_resubmit,
        )
        if cached and cached.get("job_id"):
            return AsyncBatchFoldJob(
                self._transport,
                batch_id=str(cached["job_id"]),
                jobs=[],
                total_cost_credits=int(cached.get("estimated_credits") or 0),
                peptide_count=group_count,
                trajectories_per_peptide=int(diffusion_samples),
                receptor={"source": "cached"},
                sampling_steps=int(sampling_steps),
            )

        estimated = estimate_fold_batch_credits(
            peptide_count=group_count,
            trajectories=int(num_trajectories or diffusion_samples),
            sampling_steps=int(sampling_steps),
        )
        balance_before, _ = preflight_credits(
            self._client, estimated=estimated, kind="fold_batch",
        )

        record_submission(
            submitted_set,
            credit_ledger,
            submission_hash=sub_hash,
            api_key_hash=api_key_hash,
            kind="fold_batch_grouped",
            gpu=canonical_gpu,
            estimated_credits=estimated,
            balance_before=balance_before,
            meta={
                "group_count": group_count,
                "trajectories": int(num_trajectories or diffusion_samples),
                "sampling_steps": int(sampling_steps),
            },
        )

        try:
            payload = await self._transport.request("POST", "/api/v1/folding/predict-batch", json=body) or {}
        except LigandAICreditError as cred_err:
            if on_credit_exhausted is None:
                mark_failed(
                    submitted_set, submission_hash=sub_hash,
                    api_key_hash=api_key_hash, reason="credit_exhausted",
                )
                raise
            try:
                should_retry = bool(on_credit_exhausted(cred_err, body))
            except Exception:
                mark_failed(
                    submitted_set, submission_hash=sub_hash,
                    api_key_hash=api_key_hash, reason="credit_exhausted_callback_error",
                )
                raise cred_err from None
            if not should_retry:
                mark_failed(
                    submitted_set, submission_hash=sub_hash,
                    api_key_hash=api_key_hash, reason="credit_exhausted_no_retry",
                )
                raise
            payload = await self._transport.request("POST", "/api/v1/folding/predict-batch", json=body) or {}
        except Exception as exc:
            mark_failed(
                submitted_set, submission_hash=sub_hash,
                api_key_hash=api_key_hash, reason=type(exc).__name__,
            )
            raise
        batch_id = payload.get("batch_id") or payload.get("batchId") or ""
        if not batch_id:
            mark_failed(
                submitted_set, submission_hash=sub_hash,
                api_key_hash=api_key_hash, reason="server_no_batch_id",
            )
            raise LigandAIError("Server did not return a batch_id for fold_batch", response=payload)
        attach_job_id(
            submitted_set, submission_hash=sub_hash,
            api_key_hash=api_key_hash, job_id=batch_id,
        )
        return AsyncBatchFoldJob(
            self._transport,
            batch_id=batch_id,
            jobs=payload.get("jobs") or [],
            total_cost_credits=int(payload.get("total_cost_credits") or 0),
            peptide_count=int(payload.get("peptide_count") or group_count),
            trajectories_per_peptide=int(payload.get("trajectories_per_peptide") or diffusion_samples),
            receptor=payload.get("receptor"),
            sampling_steps=int(payload.get("sampling_steps") or sampling_steps),
        )

    async def fold_custom_mutation(
        self,
        gene: str,
        mutations: list[str],
        alias: str | None = None,
    ) -> AsyncJob[FoldResult]:
        if self._client is not None:
            self._client._require_feature("predict_structure")
        body: dict[str, Any] = {"gene": gene, "mutations": mutations}
        if alias is not None:
            body["alias"] = alias
        payload = await self._transport.request("POST", "/api/ptf/fold-custom-mutation", json=body) or {}
        job_id = payload.get("jobId") or payload.get("id") or ""
        if not job_id:
            raise LigandAIError("Server did not return a jobId", response=payload)
        return AsyncJob(
            self._transport,
            job_id,
            job_type="folding",
            parser=_parse_fold,
            status_path="/api/folding/jobs/{job_id}",
            cancel_path="/api/folding/jobs/{job_id}",
            initial={"id": job_id, "type": "folding", "status": "queued", **payload},
        )

    async def continue_folding(
        self,
        session_id: str | None = None,
        gene: str | None = None,
        top_n: int = 25,
        gpu_count: int = 5,
        template_mode: bool = False,
    ) -> AsyncJob[GenerationResult]:
        if not session_id and not gene:
            raise ValueError("Pass session_id= or gene=")
        if not session_id:
            assert gene is not None
            from_session = await self._transport.request("GET", f"/api/ptf/sessions/by-gene/{gene}") or {}
            session_id = from_session.get("id")
            if not session_id:
                raise LigandAIError(f"No active session for gene {gene!r}")
        body = {
            "topN": top_n,
            "gpuCount": gpu_count,
            "templateMode": template_mode,
        }
        payload = (
            await self._transport.request("POST", f"/api/ptf/parallel/{session_id}/continue", json=body) or {}
        )
        job_id = payload.get("jobId") or session_id
        return AsyncJob(
            self._transport,
            job_id,
            job_type="generation",
            parser=_parse_generation,
            status_path="/api/ptf/parallel/{job_id}/status",
            cancel_path="/api/ptf/parallel/{job_id}/cancel",
            sse_path="/api/ptf/parallel/{job_id}/stream",
            initial={"id": job_id, "type": "generation", "status": "running", **payload},
            result_loader=lambda info: _aload_generation_result(
                self._transport,
                info,
                fallback_session_id=session_id,
                fallback_gene=gene,
            ),
        )

    async def score_complex(
        self,
        binder_sequence: str,
        target_sequence: str,
        binder_name: str = "binder",
        target_name: str = "target",
        scorer: _DeltaForgeScorer = "auto",
    ) -> AsyncJob[DeltaForgeScore]:
        body = {
            "binderSequence": binder_sequence,
            "targetSequence": target_sequence,
            "binderName": binder_name,
            "targetName": target_name,
            "scorer": scorer,
        }
        payload = await self._transport.request("POST", "/api/binder-scoring/fold-and-score", json=body) or {}
        job_id = payload.get("jobId") or payload.get("id") or ""
        if not job_id:
            raise LigandAIError("Server did not return a jobId", response=payload)

        def parse(data: dict[str, Any]) -> DeltaForgeScore:
            return _parse_deltaforge_score(data)

        return AsyncJob(
            self._transport,
            job_id,
            job_type="scoring",
            parser=parse,
            status_path=f"/api/binder-scoring/job/{{job_id}}?scorer={scorer}",
            initial={"id": job_id, "type": "scoring", "status": "submitted"},
        )

    async def score_pdb(
        self,
        *,
        pdb_content: str | None = None,
        pdb_file: str | Path | None = None,
        receptor_chains: list[str] | None = None,
        peptide_chain: str | None = None,
        chain_a: str | None = None,
        chain_b: str | None = None,
        scorer: _DeltaForgeScorer = "auto",
        aggregate_method: _DeltaForgeAggregateMethod = "boltzmann_parallel",
        include_features: bool = False,
        include_pae: bool = False,
        fold_ipsae: float | None = None,
        fold_iptm: float | None = None,
        fold_ptm: float | None = None,
        fold_plddt_mean: float | None = None,
        fold_complex_plddt: float | None = None,
        fold_complex_iplddt: float | None = None,
    ) -> DeltaForgeScore:
        if not pdb_content and not pdb_file:
            raise ValueError("Pass pdb_content= or pdb_file=")
        if pdb_content and pdb_file:
            raise ValueError("Pass only one of pdb_content= or pdb_file=")
        content = pdb_content if pdb_content is not None else Path(pdb_file).read_text()
        receptors = receptor_chains or ([chain_a] if chain_a else None)
        peptide = peptide_chain or chain_b
        if not receptors or not peptide:
            raise ValueError("Pass receptor_chains= and peptide_chain=, or chain_a= and chain_b=")

        payload = await self._transport.request(
            "POST",
            "/api/v1/deltaforge/score-pdb",
            json={
                "pdbContent": content,
                "receptorChains": receptors,
                "peptideChain": peptide,
                "scorer": scorer,
                "aggregateMethod": aggregate_method,
                "includeFeatures": include_features,
                "includePae": include_pae,
                "foldIpsae": fold_ipsae,
                "foldIptm": fold_iptm,
                "foldPtm": fold_ptm,
                "foldPlddtMean": fold_plddt_mean,
                "foldComplexPlddt": fold_complex_plddt,
                "foldComplexIplddt": fold_complex_iplddt,
            },
        ) or {}
        return _parse_deltaforge_score(payload)

    async def score_with_ligandiq(
        self,
        session_id: str | None = None,
        gene: str | None = None,
        top_n: int = 20,
    ) -> list[LigandIQScore]:
        if not session_id and not gene:
            raise ValueError("Pass session_id= or gene=")
        if not session_id:
            from_session = await self._transport.request("GET", f"/api/ptf/sessions/by-gene/{gene}") or {}
            session_id = from_session.get("id")
            if not session_id:
                raise LigandAIError(f"No active session for gene {gene!r}")
        body = {"topN": top_n}
        payload = (
            await self._transport.request(
                "POST", f"/api/ptf/parallel/{session_id}/ligandiq-score", json=body
            )
            or {}
        )
        items = payload.get("scores") or payload.get("results") or []
        return [LigandIQScore.model_validate(s) for s in items]

    async def analyze_solubility(
        self,
        peptides: list[PeptideInput | dict[str, Any] | str],
        gravy_threshold: float = 0.0,
        flag_multi_cys: bool = True,
    ) -> list[SolubilityResult]:
        normalized = [
            (p.model_dump(by_alias=True) if isinstance(p, PeptideInput) else
             {"sequence": p} if isinstance(p, str) else p)
            for p in peptides
        ]
        body = {
            "peptides": normalized,
            "gravyThreshold": gravy_threshold,
            "flagMultiCys": flag_multi_cys,
        }
        payload = (
            await self._transport.request("POST", "/api/peptide-features/solubility", json=body)
            or {}
        )
        items = payload.get("results") or payload.get("solubility") or []
        return [SolubilityResult.model_validate(s) for s in items]

    async def score_developability(
        self,
        sequences: list[str] | None = None,
        *,
        peptide_ids: list[int] | None = None,
        fold_ids: list[str | int] | None = None,
        modules: list[str] | None = None,
        stability: bool | None = None,
        immuno: bool | None = None,
        halflife: bool | None = None,
    ) -> list[DevelopabilityResult]:
        """Async ``POST /api/v1/peptides/score-developability``.

        See :meth:`Peptides.score_developability` for full semantics: standalone
        developability rescore (stability / immuno / half-life) of existing
        peptides — no regen, no refold — by sequence, peptide_id, or fold_id.
        """
        body = _build_developability_body(
            sequences=sequences,
            peptide_ids=peptide_ids,
            fold_ids=fold_ids,
            modules=modules,
            stability=stability,
            immuno=immuno,
            halflife=halflife,
        )
        payload = (
            await self._transport.request("POST", "/api/v1/peptides/score-developability", json=body)
            or {}
        )
        items = payload.get("results") or []
        return [DevelopabilityResult.model_validate(s) for s in items]

    async def search(
        self,
        gene: str | None = None,
        classification: str | None = None,
        ipsae_min: float | None = None,
        iptm_min: float | None = None,
        plddt_min: float | None = None,
        kd_max: float | None = None,
        dg_max: float | None = None,
        pde_max: float | None = None,
        pde_engine: str | None = None,
        binder_pct_min: float | None = None,
        length_min: int | None = None,
        length_max: int | None = None,
        is_elite: bool | None = None,
        super_elite: bool | None = None,
        super_elite_affinity: bool | None = None,
        super_elite_thermo: bool | None = None,
        acceptance_criterion: Literal[
            "great_plus", "elite", "super_elite", "super_elite_affinity"
        ] | None = None,
        require_linker_leg: bool | None = None,
        require_ensemble: bool | None = None,
        hotspot_residues: list[str] | None = None,
        pocket_residues: list[str] | None = None,
        hotspot_hit: bool | None = None,
        pocket_hit: bool | None = None,
        contact_distance_a: float | None = None,
        stability_grade: list[str] | None = None,
        immuno_grade: list[str] | None = None,
        conformation: str | None = None,
        program_id: int | None = None,
        session_id: str | None = None,
        pdb_id: str | None = None,
        sort: str = "ipsae",
        order: str = "desc",
        min_ipsae: float | None = None,  # legacy alias
        limit: int = 20,
        offset: int = 0,
    ) -> list[Peptide]:
        """Async variant of :meth:`Peptides.search`.

        Mirrors the full sync signature — every score/coverage/scope filter
        the workspace UI exposes, including ``acceptance_criterion`` /
        ``require_linker_leg`` / ``require_ensemble``. See :meth:`Peptides.search`
        for full argument documentation — in particular the unit traps
        (pLDDT 0-100, predicted_kd nanomolar, ``peptide_ipsae`` never
        whole-complex ``ipsae``) and the ``require_ensemble`` honesty note
        (co-validation is not persistable on the platform today;
        bd-LIGANDAI_ALPHA_V2-xv5ih). All criteria AND-combine.
        """
        if min_ipsae is not None and ipsae_min is None:
            ipsae_min = min_ipsae
        if super_elite_thermo is not None:
            warnings.warn(
                "super_elite_thermo is deprecated; use super_elite_affinity instead.",
                DeprecationWarning,
                stacklevel=2,
            )
            if super_elite_affinity is None:
                super_elite_affinity = super_elite_thermo
        params: dict[str, Any] = {"limit": limit, "offset": offset, "sort": sort, "order": order}
        if gene is not None: params["gene"] = gene.upper()
        if classification is not None: params["classification"] = classification
        if ipsae_min is not None: params["ipsae_min"] = ipsae_min
        if iptm_min is not None: params["iptm_min"] = iptm_min
        if plddt_min is not None: params["plddt_min"] = plddt_min
        if kd_max is not None: params["kd_max"] = kd_max
        if dg_max is not None: params["dg_max"] = dg_max
        # PDE (migration 170). LOWER IS BETTER, so this is a MAX threshold.
        # Without pde_engine the server filters pde_rankable — the DB-generated
        # cross-engine-safe column — so proxy and unverified-scale folds are
        # excluded rather than silently mixed in. Supplying pde_engine pins the
        # query to one engine and filters the raw interface_pde_min instead.
        if pde_max is not None: params["pde_max"] = pde_max
        if pde_engine is not None: params["pde_engine"] = pde_engine
        if binder_pct_min is not None: params["binder_pct_min"] = binder_pct_min
        if length_min is not None: params["length_min"] = length_min
        if length_max is not None: params["length_max"] = length_max
        if is_elite is not None: params["is_elite"] = "true" if is_elite else "false"
        if super_elite is not None: params["super_elite"] = "true" if super_elite else "false"
        if super_elite_affinity is not None: params["super_elite_affinity"] = "true" if super_elite_affinity else "false"
        # Acceptance-gate ladder [bd-LIGANDAI_ALPHA_V2-dl6pj]. See the sync
        # search() docstring for the full unit-trap and ensemble notes.
        if acceptance_criterion is not None: params["acceptance_criterion"] = acceptance_criterion
        if require_linker_leg is not None: params["require_linker_leg"] = "true" if require_linker_leg else "false"
        if require_ensemble is not None: params["require_ensemble"] = "true" if require_ensemble else "false"
        if hotspot_hit is not None: params["hotspot_hit"] = "true" if hotspot_hit else "false"
        if pocket_hit is not None: params["pocket_hit"] = "true" if pocket_hit else "false"
        if hotspot_residues: params["hotspot_residues"] = ",".join(hotspot_residues)
        if pocket_residues: params["pocket_residues"] = ",".join(pocket_residues)
        if contact_distance_a is not None: params["contact_distance_a"] = contact_distance_a
        if stability_grade: params["stability_grade"] = ",".join(stability_grade)
        if immuno_grade: params["immuno_grade"] = ",".join(immuno_grade)
        if conformation is not None: params["conformation"] = conformation
        if program_id is not None: params["program_id"] = program_id
        if session_id is not None: params["session_id"] = session_id
        if pdb_id is not None: params["pdb_id"] = pdb_id.upper()

        payload = await self._transport.request(
            "GET", "/api/v1/peptides/search", params=params
        ) or {}
        items = payload.get("peptides", []) if isinstance(payload, dict) else (payload or [])
        return [Peptide.model_validate(p) for p in items]

    async def search_by_pocket(
        self,
        gene: str,
        chain: str | None = None,
        start_residue: int | None = None,
        end_residue: int | None = None,
        targeted_only: bool = True,
    ) -> list[Peptide]:
        params: dict[str, Any] = {"gene": gene, "targeted_only": targeted_only}
        if chain is not None:
            params["chain"] = chain
        if start_residue is not None:
            params["start_residue"] = start_residue
        if end_residue is not None:
            params["end_residue"] = end_residue
        payload = await self._transport.request(
            "GET", "/api/ptf/peptides/by-pocket", params=params
        )
        items = unwrap_rows(
            payload,
            _BY_POCKET_KEYS,
            what=f"GET /api/ptf/peptides/by-pocket for {gene!r}",
        )
        return [Peptide.model_validate(p) for p in items]

    async def get_elite(
        self,
        session_id: str | None = None,
        gene: str | None = None,
    ) -> list[Peptide]:
        if not session_id and not gene:
            raise ValueError("Pass session_id= or gene=")
        if not session_id:
            from_session = await self._transport.request(
                "GET", f"/api/ptf/sessions/by-gene/{gene}"
            )
            session_id = _session_id_for_gene(from_session, gene)
            if not session_id:
                raise LigandAIError(f"No active session for gene {gene!r}")
        payload = await self._transport.request(
            "GET", f"/api/ptf/parallel/{session_id}/elite"
        )
        items = unwrap_rows(
            payload,
            _ELITE_KEYS,
            what=f"GET /api/ptf/parallel/{session_id}/elite",
        )
        return [Peptide.model_validate(p) for p in items]

    # --- Selectivity / negative-selection validation (async) ---

    async def fold_compare(
        self,
        binder: str,
        on_target: str,
        *,
        isoforms: str | Sequence[str] | None = None,
        off_targets: Sequence[str] | None = None,
        engine: str = "boltz2",
        binder_id: str = "binder",
        on_target_sequence: str | None = None,
        organism: str | None = None,
        species: str | None = None,
        wait: bool = True,
        poll_interval: float = 10.0,
        timeout: float = 1800.0,
    ) -> dict[str, Any]:
        """Async variant of :meth:`Peptides.fold_compare`.

        Fold-and-score a designed binder against a target's isoforms + off-targets
        for negative-selection / selectivity validation. Species is resolved
        fail-closed (non-entitled ``mouse`` -> ``human``); ``organism`` wins over
        ``species``. See the sync method for full argument semantics.
        """
        if not binder or not isinstance(binder, str):
            raise LigandAIValidationError("binder must be an amino-acid sequence")
        if not on_target and not on_target_sequence:
            raise LigandAIValidationError(
                "on_target (gene / UniProt) or on_target_sequence is required",
            )
        body: dict[str, Any] = {
            "binder": {"id": binder_id, "sequence": binder.strip().upper()},
            "engine": engine,
        }
        effective = self._resolve_organism(organism, species)
        body["organism"] = effective
        body["species"] = effective
        if on_target_sequence:
            body["on_target"] = {
                "gene": on_target,
                "sequence": on_target_sequence.strip().upper(),
            }
        else:
            body["on_target"] = {"gene": on_target}
        if isoforms is not None:
            body["isoforms"] = isoforms if isoforms == "all" else list(isoforms)
        if off_targets:
            body["off_targets"] = list(off_targets)

        submit = await self._transport.request(
            "POST", "/api/v1/fold-compare/start", json=body
        ) or {}
        if not wait:
            return submit
        job_id = submit.get("job_id") or submit.get("jobId")
        if not job_id:
            raise LigandAIValidationError(
                f"fold-compare start returned no job_id: {submit}"
            )
        return await self._poll_fold_compare(
            job_id, poll_interval=poll_interval, timeout=timeout
        )

    def _resolve_organism(self, organism: str | None, species: str | None) -> str:
        """Fail-closed effective species for a fold-compare call (async client)."""
        requested = organism if organism is not None else species
        if self._client is not None:
            return str(self._client._resolve_species(requested))
        from ligandai.species import normalize_species

        return str(normalize_species(requested)) if requested is not None else "human"

    async def _poll_fold_compare(
        self,
        job_id: str,
        *,
        poll_interval: float = 10.0,
        timeout: float = 1800.0,
    ) -> dict[str, Any]:
        import asyncio
        import time

        start = time.time()
        terminal = {"completed", "done", "succeeded", "failed", "cancelled", "error"}
        while True:
            status = await self._transport.request(
                "GET", f"/api/v1/fold-compare/{job_id}/status"
            ) or {}
            state = str(status.get("status") or status.get("state") or "").lower()
            if state in terminal:
                return status
            if time.time() - start > timeout:
                raise LigandAIWaitTimeout(f"fold-compare job timed out: {job_id}")
            await asyncio.sleep(poll_interval)

    async def validate_selectivity(
        self,
        session_id: str,
        on_target: str,
        *,
        isoforms: str | Sequence[str] | None = "all",
        off_targets: Sequence[str] | None = None,
        top_n: int = 10,
        engine: str = "boltz2",
        organism: str | None = None,
        species: str | None = None,
        wait: bool = True,
        poll_interval: float = 10.0,
        timeout: float = 1800.0,
    ) -> list[dict[str, Any]]:
        """Async variant of :meth:`Peptides.validate_selectivity`.

        Pulls the top ``top_n`` elite candidates from ``session_id`` (by iPSAE)
        and runs :meth:`fold_compare` on each against the on-target isoforms +
        off-targets. Species resolved fail-closed. See the sync method for full
        argument semantics.
        """
        if top_n < 1:
            raise LigandAIValidationError("top_n must be at least 1")
        elite = await self.get_elite(session_id=session_id)

        def _ipsae(p: Peptide) -> float:
            # ⛔ peptide_ipsae is THE binder metric — the peptide<->receptor
            # interface only. Whole-complex `ipsae` is inflated by the receptor's
            # confidence in itself on any multimeric target, so ranking on it
            # sends the wrong candidates into the selectivity comparison. Fall
            # back to `ipsae` ONLY when the binder metric was never computed, and
            # sort a never-computed row last rather than treating it as a zero.
            v = getattr(p, "peptide_ipsae", None)
            if not isinstance(v, (int, float)):
                v = getattr(p, "ipsae", None)
            return float(v) if isinstance(v, (int, float)) else -1.0

        ranked = sorted(
            (p for p in elite if getattr(p, "sequence", None)),
            key=_ipsae,
            reverse=True,
        )[:top_n]

        out: list[dict[str, Any]] = []
        for p in ranked:
            seq = str(p.sequence).strip().upper()
            report = await self.fold_compare(
                binder=seq,
                on_target=on_target,
                isoforms=isoforms,
                off_targets=off_targets,
                engine=engine,
                binder_id=str(getattr(p, "peptide_id", None) or seq[:12]),
                organism=organism,
                species=species,
                wait=wait,
                poll_interval=poll_interval,
                timeout=timeout,
            )
            out.append({"binder": p, "fold_compare": report})
        return out

    # --- v0.2.0 paid-only surface (async) ---

    async def by_gene(
        self,
        genes: list[str] | None = None,
        min_ipsae: float | None = None,
        program_id: int | None = None,
        project_id: int | None = None,
        since: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[GeneSummary]:
        """Async variant of :meth:`Peptides.by_gene`."""
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if genes:
            params["genes"] = ",".join(g.upper() for g in genes if g)
        if min_ipsae is not None:
            params["minIpsae"] = min_ipsae
        if program_id is not None:
            params["programId"] = program_id
        if project_id is not None:
            params["projectId"] = project_id
        if since is not None:
            params["since"] = since.isoformat()
        payload = await self._transport.request(
            "GET", "/api/v1/peptides/by-gene", params=params
        ) or {}
        rows = payload.get("rows", []) if isinstance(payload, dict) else []
        return [GeneSummary.model_validate(r) for r in rows]

    async def by_pdb(
        self,
        pdb: str | list[str],
        min_ipsae: float | None = None,
        since: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Async variant of :meth:`Peptides.by_pdb`."""
        codes = [pdb] if isinstance(pdb, str) else list(pdb)
        params: dict[str, Any] = {
            "pdb": ",".join(c.upper() for c in codes if c),
            "limit": limit, "offset": offset,
        }
        if min_ipsae is not None:
            params["min_ipsae"] = min_ipsae
        if since is not None:
            params["since"] = since.isoformat()
        payload = await self._transport.request(
            "GET", "/api/v1/peptides/by-pdb", params=params
        ) or {}
        return payload.get("rows", []) if isinstance(payload, dict) else []

    async def list(
        self,
        gene_or_program_id: str | int | None = None,
        *,
        gene: str | None = None,
        program_id: int | None = None,
        min_ipsae: float | None = None,
        min_iptm: float | None = None,
        max_kd: float | None = None,
        include_unfolded: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Peptide]:
        """Async variant of :meth:`Peptides.list`."""
        if gene_or_program_id is not None:
            if isinstance(gene_or_program_id, str):
                if gene is not None and gene != gene_or_program_id:
                    raise ValueError("conflicting gene values: pass either positionally or by keyword, not both")
                gene = gene_or_program_id
            elif isinstance(gene_or_program_id, int):
                if program_id is not None and program_id != gene_or_program_id:
                    raise ValueError("conflicting program_id values: pass either positionally or by keyword, not both")
                program_id = gene_or_program_id
            else:
                raise TypeError(
                    f"list() positional arg must be a gene symbol (str) or program_id (int); "
                    f"got {type(gene_or_program_id).__name__}"
                )

        # ⛔ `include_unfolded` is ACCEPTED and does nothing — it never reaches
        # `params` and the endpoint returns folded rows only. A docstring
        # saying "has no effect" is not a runtime signal: a caller who passes
        # include_unfolded=True gets a folded-only list that looks exactly
        # like a complete one, and will read the missing unfolded designs as
        # "there are none". Say so out loud, naming the parameter.
        # [bd-dre-rp11j.3]
        if include_unfolded:
            warnings.warn(
                "include_unfolded=True was accepted but has NO EFFECT: the "
                "/v1/peptides/list endpoint returns FOLDED peptides only, and "
                "this parameter is never sent. The list you get back excludes "
                "unfolded designs - do not read its length as a total. To "
                "count generated-but-unfolded designs, read the generation "
                "job's own result instead. Pass include_unfolded=False (the "
                "default) to silence this.",
                LigandAIDiagnosticWarning,
                stacklevel=2,
            )

        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if program_id is not None:
            params["program_id"] = program_id
        if gene is not None:
            if not gene.strip():
                raise ValueError("gene must be a non-empty string")
            params["gene"] = gene.upper()
        if min_ipsae is not None:
            params["min_ipsae"] = min_ipsae
        if min_iptm is not None:
            params["min_iptm"] = min_iptm
        if max_kd is not None:
            params["max_kd"] = max_kd

        payload = await self._transport.request(
            "GET", "/api/v1/peptides/list", params=params
        ) or {}
        items = payload.get("peptides", []) if isinstance(payload, dict) else (payload or [])
        return [Peptide.model_validate(p) for p in items]

    async def list_by_program(
        self,
        program_id: int,
        *,
        min_ipsae: float | None = None,
        min_iptm: float | None = None,
        max_kd: float | None = None,
        gene: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Peptide]:
        """Async variant of :meth:`Peptides.list_by_program`."""
        return await self.list(
            program_id=program_id,
            # ⛔ `conversation_id=conversation_id` stood here and made BOTH
            # twins of this public method raise NameError on every call —
            # the name is not a parameter of list_by_program, is never bound
            # in this module's scope, and is not a parameter of `list()`
            # either, so even a bound value would have been a TypeError one
            # line later. The method could never have run. [bd-dre-rp11j.3]
            gene=gene,
            min_ipsae=min_ipsae,
            min_iptm=min_iptm,
            max_kd=max_kd,
            limit=limit,
            offset=offset,
        )

    async def get(
        self,
        peptide_id: int | str,
        include: list[_IncludeField] | None = None,
    ) -> PeptideDetail:
        """Async variant of :meth:`Peptides.get`."""
        if self._client is not None:
            self._client._require_paid_tier()
        try:
            id_int = int(peptide_id)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"peptide_id must be a positive integer (got {peptide_id!r})"
            ) from exc
        if id_int <= 0:
            raise ValueError(f"peptide_id must be > 0 (got {id_int})")
        params: dict[str, Any] = {}
        if include:
            unknown = [v for v in include if v not in _ALLOWED_INCLUDE]
            if unknown:
                raise ValueError(
                    f"Unknown include value(s): {unknown}. "
                    f"Allowed: {sorted(_ALLOWED_INCLUDE)}"
                )
            params["include"] = ",".join(include)
        payload = await self._transport.request(
            "GET", f"/api/v1/peptides/{id_int}", params=params
        ) or {}
        return PeptideDetail.model_validate(payload)

    async def estimate_cost(
        self,
        *,
        num_peptides: int,
        auto_fold: bool = True,
        fold_top_n: int | None = None,
        fold_trajectories: int = 4,
    ) -> CostEstimate:
        """Async variant of :meth:`Peptides.estimate_cost`."""
        params: dict[str, Any] = {
            "num_peptides": num_peptides,
            "auto_fold": auto_fold,
            "fold_trajectories": fold_trajectories,
        }
        if fold_top_n is not None:
            params["top_n"] = fold_top_n
        payload = (
            await self._transport.request("GET", "/api/billing/estimate", params=params)
            or {}
        )
        return CostEstimate.model_validate(payload)
