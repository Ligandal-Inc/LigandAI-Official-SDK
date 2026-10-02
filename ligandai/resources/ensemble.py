# Copyright © 2025 Ligandal, Inc. All rights reserved.
"""Ensemble co-fold — many peptides x many engines, with a consensus answer.

``client.peptides.cofold()`` folds ONE complex on 1-6 engines. This resource is
the layer a binder campaign actually needs: a receptor plus N candidate peptides
across M engines, a projected cost BEFORE anything runs, a spend ceiling, and a
returned CONSENSUS rather than M separate leaderboards.

WHY CONSENSUS IS THE RETURNED OBJECT. A customer choosing 10 of 25 designs to
synthesise does not need six rankings. They need, per peptide: how many engines
put it above the bar, what each engine actually said, and how much the ordering
moved between engines. A candidate every engine likes is a different proposition
from one only the default engine likes, and only the second of those needs a
caveat in the report.

⛔ COFACTORS ARE PART OF THIS INTERFACE, NOT AN IMPLEMENTATION DETAIL.
Some targets do not have a meaningful interface without their cofactor. ENPP3
carries two catalytic zincs, and all 25 shortlisted cyclic binders lose their
binding interface when the zincs are removed (median ipSAE drop 0.302, none
clearing the 0.67 bar zinc-free). A zinc-free fold of that target is a fold of a
different molecule.

Engines split into those that take structured ENTITIES and can carry a ligand,
and those that take chains or a bare sequence and cannot. That split is exposed
by :meth:`Ensemble.cofactor_support` and is enforced: asking for a cofactor an
arm cannot carry RAISES rather than quietly folding without it and pooling the
result with arms that carried it. See :data:`COFACTOR_SUPPORT`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

from ..errors import LigandAIInvalidConfig
from ._base import AsyncResource, Resource

#: Engines whose Modal worker can carry a ligand/cofactor entity. MEASURED
#: against each worker's own entity parser on 2026-09-02 — read from the source
#: and, where marked, confirmed by a fold that actually carried the ligand:
#:   boltz2    boltz2.py::_ligand_spec_from_entity reads ccd/ccdCode/ccd_code and
#:             smiles BY NAME and RAISES on an empty spec. CONFIRMED BY FOLD:
#:             2x {"type":"ligand","ccd":"ZN"} on ENPP3 gave ipSAE 0.786 with the
#:             zincs vs 0.369 without, same peptide, same settings.
#:   protenix  fold_complex takes the nested entity list including
#:             {"ligand": {"id": .., "ccd": ..}}. CONFIRMED BY FOLD.
#:   esmfold2  fast_single.py::_entities_to_spi maps a ligand entity onto
#:             LigandInput(id, ccd=[...]) (or smiles). CONFIRMED BY FOLD: ipTM
#:             0.554 with Zn vs 0.241 without, same peptide.
#:   openfold3 openfold.py::_normalize_chains accepts `ccd_codes` and `smiles`
#:             per chain. CONFIRMED BY FOLD: 355aa receptor + 12mer + 2x ZN
#:             returned a 369x369 PAE — 355 + 12 + 2, i.e. both zincs were
#:             tokenised and carried. (That fold is otherwise unusable on this
#:             deployment: its only MSA route is the public ColabFold server,
#:             which errors, and single-sequence it returns mean pLDDT 33.9 —
#:             the receptor does not fold. Cofactor capability and usability are
#:             separate facts and are reported separately.)
#:   opendde   fold() takes `chains` with kind in {receptor, peptide} and NOTHING
#:             else (opendde_fold_worker.py:402). It cannot express a cofactor.
#:   promera   NOT VERIFIED. Declared unsupported rather than assumed either way
#:             — an unchecked True here silently drops a cofactor, which is the
#:             exact failure this table exists to prevent.
COFACTOR_SUPPORT: dict[str, bool] = {
    "boltz2": True,
    "protenix": True,
    "esmfold2": True,
    "openfold3": True,
    "opendde": False,
    "promera": False,
}

#: Whether the hosted ensemble route (``POST /api/v1/cofold/start``) can EXPRESS a
#: cofactor. It can, since server commit 2e9ed2d1a2 (bd-dre-3jqok): the request
#: carries a top-level ``cofactors`` list (``ccd`` | ``smiles``, ``copies``, ``id``;
#: exactly one of ccd/smiles), equivalent to ``chains`` entries with
#: ``kind="cofactor"``, and the per-engine converters emit each engine's native
#: ligand entity for boltz2, protenix, esmfold2 and openfold3. ``opendde`` and
#: ``promera`` cannot carry one and the server REFUSES the request unless
#: ``allow_cofactor_absent`` is set. This SDK sends both fields (see
#: ``Peptides.cofold``).
#:
#: ⛔ This flag says the route CAN express a cofactor. It is never evidence that a
#: particular fold CARRIED one: an older deployment would ignore the unknown
#: ``cofactors`` field and fold the complex without it. So ``run()`` marks an arm
#: ``cofactor_honoured`` only when the server's own record for that engine says
#: ``cofactor.honoured`` — the SDK's expectation alone never counts.
COFACTORS_REACHABLE_VIA_HOSTED_COFOLD = True

DEFAULT_BAR = 0.67


@dataclass
class CofactorSpec:
    """One cofactor to co-fold with the complex.

    ``ccd`` is a PDB Chemical Component Dictionary code (``"ZN"``, ``"MG"``,
    ``"HEM"``); ``smiles`` is the alternative for anything without one. Exactly
    one must be given — an empty spec is the defect that silently produced
    ``ligand: {id: Y, ccd: ''}`` and killed every cofactor fold for months.
    """

    ccd: Optional[str] = None
    smiles: Optional[str] = None
    id: Optional[str] = None
    copies: int = 1

    def __post_init__(self) -> None:
        if bool(self.ccd) == bool(self.smiles):
            raise LigandAIInvalidConfig(
                "CofactorSpec needs exactly one of ccd= or smiles= "
                f"(got ccd={self.ccd!r}, smiles={self.smiles!r})"
            )
        if self.copies < 1:
            raise LigandAIInvalidConfig("CofactorSpec.copies must be >= 1")

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"copies": self.copies}
        if self.id:
            d["id"] = self.id
        if self.ccd:
            d["ccd"] = self.ccd
        else:
            d["smiles"] = self.smiles
        return d


def _credits_text(per_complex: Any, n: int) -> str:
    """``per_complex * n`` as display text (no ``.0`` on whole credits)."""
    if isinstance(per_complex, bool) or not isinstance(per_complex, (int, float)):
        return str(per_complex)
    total = per_complex * n
    return str(int(total)) if float(total).is_integer() else str(round(total, 2))


@dataclass
class EnsembleEstimate:
    """Projected spend for a whole ensemble, BEFORE anything runs."""

    engines: list[str]
    n_complexes: int
    credits_total: int
    credits_per_complex: float
    per_engine: list[dict[str, Any]]
    discount_percent: float
    usd_total: float
    breakdown: str
    cofactor_arms: dict[str, bool] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        lines = [
            f"ensemble co-fold: {self.n_complexes} complexes x "
            f"{len(self.engines)} engines = {self.n_complexes * len(self.engines)} folds",
            f"  projected {self.credits_total} credits "
            f"(~${self.usd_total:.2f}), {self.discount_percent:.0f}% bundle discount",
        ]
        for e in self.per_engine:
            # The server quotes ONE complex (see Ensemble.estimate), so the
            # per-engine ``cost`` it returns is per complex; this line says
            # "x N", so it must show the N-complex figure, not the per-complex one.
            lines.append(
                f"    {e.get('engine'):<10} {e.get('trajectories')} traj "
                f"x {self.n_complexes} = "
                f"{_credits_text(e.get('cost'), self.n_complexes)} cr"
            )
        for w in self.warnings:
            lines.append(f"  ! {w}")
        return "\n".join(lines)


@dataclass
class ConsensusRow:
    """One peptide's answer across the ensemble."""

    sequence: str
    per_engine: dict[str, Optional[float]]
    n_engines_scored: int
    n_engines_above_bar: int
    engines_above_bar: list[str]
    cofactor_honoured: dict[str, bool]
    unanimous: bool
    contested: bool


@dataclass
class EnsembleResult:
    """Per-engine folds plus the consensus, with the arms kept separable.

    ⛔ ``rows`` never pools a cofactor-bearing arm with a cofactor-absent one.
    ``ConsensusRow.cofactor_honoured`` says, per engine, whether the cofactor
    the caller asked for actually reached that fold.
    """

    bar: float
    engines: list[str]
    metric: str
    rows: list[ConsensusRow]
    failures: list[dict[str, Any]]
    credits_spent: Optional[int] = None
    wall_seconds: Optional[float] = None
    stopped_reason: Optional[str] = None
    cofactors: list[dict[str, Any]] = field(default_factory=list)

    def consensus_histogram(self) -> dict[int, int]:
        """How many peptides had k engines above the bar, for each k."""
        h: dict[int, int] = {}
        for r in self.rows:
            h[r.n_engines_above_bar] = h.get(r.n_engines_above_bar, 0) + 1
        return dict(sorted(h.items()))

    def unanimous(self) -> list[ConsensusRow]:
        """Peptides every scored engine put above the bar."""
        return [r for r in self.rows if r.unanimous]

    def rank_agreement(self) -> list[dict[str, Any]]:
        """Spearman rho between every pair of engines, with n stated.

        n is the number of peptides BOTH engines scored — an engine that failed
        on half the set is compared on the half it returned, and that n is
        reported rather than hidden.
        """
        out = []
        engs = list(self.engines)
        for i in range(len(engs)):
            for j in range(i + 1, len(engs)):
                a, b = engs[i], engs[j]
                pairs = [
                    (r.per_engine.get(a), r.per_engine.get(b))
                    for r in self.rows
                    if r.per_engine.get(a) is not None
                    and r.per_engine.get(b) is not None
                ]
                if len(pairs) < 3:
                    out.append({"a": a, "b": b, "n": len(pairs), "spearman": None,
                                "note": "fewer than 3 shared peptides"})
                    continue
                rho = _spearman([p[0] for p in pairs], [p[1] for p in pairs])
                out.append({"a": a, "b": b, "n": len(pairs),
                            "spearman": round(rho, 4) if rho is not None else None})
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "bar": self.bar, "engines": self.engines, "metric": self.metric,
            "cofactors": self.cofactors,
            "credits_spent": self.credits_spent,
            "wall_seconds": self.wall_seconds,
            "stopped_reason": self.stopped_reason,
            "consensus_histogram": self.consensus_histogram(),
            "rank_agreement": self.rank_agreement(),
            "failures": self.failures,
            "designs": [
                {"sequence": r.sequence, "per_engine": r.per_engine,
                 "n_engines_scored": r.n_engines_scored,
                 "n_engines_above_bar": r.n_engines_above_bar,
                 "engines_above_bar": r.engines_above_bar,
                 "cofactor_honoured": r.cofactor_honoured,
                 "unanimous": r.unanimous, "contested": r.contested}
                for r in self.rows
            ],
        }


def _spearman(x: Sequence[float], y: Sequence[float]) -> Optional[float]:
    n = len(x)
    if n < 3:
        return None

    def rank(v: Sequence[float]) -> list[float]:
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r

    rx, ry = rank(x), rank(y)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    dy = math.sqrt(sum((b - my) ** 2 for b in ry))
    return num / (dx * dy) if dx and dy else None


def _norm_cofactors(cofactors: Optional[Iterable[Any]]) -> list[CofactorSpec]:
    if not cofactors:
        return []
    out: list[CofactorSpec] = []
    for c in cofactors:
        if isinstance(c, CofactorSpec):
            out.append(c)
        elif isinstance(c, str):
            out.append(CofactorSpec(ccd=c.strip().upper()))
        elif isinstance(c, dict):
            out.append(CofactorSpec(
                ccd=c.get("ccd") or c.get("ccdCode"),
                smiles=c.get("smiles"),
                id=c.get("id"),
                copies=int(c.get("copies", 1)),
            ))
        else:
            raise LigandAIInvalidConfig(
                f"cofactor must be a CCD string, dict, or CofactorSpec; got {type(c).__name__}"
            )
    return out


#: The setting the SERVER reads to count one engine's trajectories — a mirror of
#: ``trajectoryKnob`` in shared/fold-engine-registry.ts (the single source of
#: truth the quote and the reservation both use). ``count`` knobs take an integer;
#: ``seedList`` knobs are counted as ``len(seeds)``. ``promera`` is absent on
#: purpose: it is fixed at one trajectory server-side and has no knob.
_TRAJECTORY_KNOB: dict[str, tuple[str, str]] = {
    "boltz2": ("diffusion_samples", "count"),
    "protenix": ("sample", "count"),
    "opendde": ("sample", "count"),
    "esmfold2": ("seeds", "seedList"),
    "openfold3": ("seeds", "seedList"),
}


def _per_engine_body(
    per_engine: Optional[dict[str, Any]],
    per_engine_trajectories: Optional[dict[str, int]],
    per_engine_settings: Optional[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Build the ``per_engine`` map ``POST /api/v1/cofold/estimate`` reads.

    ⛔ The server reads ONE key, ``per_engine`` (``{engine: {setting: value}}``),
    and derives the trajectory count from the engine's own knob. This SDK used to
    send ``perEngineTrajectories`` / ``perEngineSettings`` — keys the handler never
    reads — so a caller who asked for 8 Boltz-2 trajectories was quoted the
    default and charged for 8. The same ``per_engine`` dict that goes to
    ``/start`` goes here, so the quote and the run cannot parse it differently.

    ``per_engine_trajectories={"boltz2": 8}`` is a shortcut for the engine's
    knob; ``per_engine_settings`` merges cost-relevant settings
    (``use_potentials``, ``sampling_steps``). Naming the same knob in both
    ``per_engine`` and the shortcut raises rather than letting one silently win.
    """
    out: dict[str, dict[str, Any]] = {}
    for eng, st in (per_engine or {}).items():
        out[str(eng).strip().lower()] = dict(st) if isinstance(st, dict) else {}
    for eng, n in (per_engine_trajectories or {}).items():
        el = str(eng).strip().lower()
        if el == "promera":
            continue  # fixed at 1 trajectory server-side; there is nothing to set
        knob = _TRAJECTORY_KNOB.get(el)
        if knob is None:
            raise LigandAIInvalidConfig(
                f"per_engine_trajectories names unknown engine {eng!r}; "
                f"known: {sorted(_TRAJECTORY_KNOB) + ['promera']}"
            )
        if isinstance(n, bool) or not isinstance(n, int) or n < 1:
            raise LigandAIInvalidConfig(
                f"per_engine_trajectories[{eng!r}] must be an integer >= 1 (got {n!r})"
            )
        key, kind = knob
        slot = out.setdefault(el, {})
        if key in slot:
            raise LigandAIInvalidConfig(
                f"{eng!r}: {key!r} is set in both per_engine and "
                "per_engine_trajectories — pass it once"
            )
        slot[key] = n if kind == "count" else list(range(1, n + 1))
    for eng, st in (per_engine_settings or {}).items():
        el = str(eng).strip().lower()
        if not isinstance(st, dict):
            raise LigandAIInvalidConfig(
                f"per_engine_settings[{eng!r}] must be a dict (got {type(st).__name__})"
            )
        out.setdefault(el, {}).update(st)
    return out


class _EnsembleCommon:
    """Shared, transport-free logic so the sync and async twins cannot drift."""

    @staticmethod
    def cofactor_support(engines: Optional[list[str]] = None) -> dict[str, dict[str, Any]]:
        """Per-engine cofactor capability — what the engine can do, and what is
        reachable through the hosted co-fold route today.

        ``engine_can_carry`` is a measured property of the engine's own entity
        parser. ``reachable_today`` is whether ``POST /api/v1/cofold/start`` can
        express it (``cofactors`` / ``ChainSpec kind="cofactor"``) AND the engine
        can carry it. The two are reported separately so a caller is never told
        "unsupported" when the real answer is "supported by the model, not
        plumbed".
        """
        engs = engines or list(COFACTOR_SUPPORT)
        out = {}
        for e in engs:
            el = str(e).strip().lower()
            can = COFACTOR_SUPPORT.get(el)
            out[el] = {
                "engine_can_carry": can,
                "reachable_today": bool(can) and COFACTORS_REACHABLE_VIA_HOSTED_COFOLD,
                "reason": (
                    "engine takes chains/sequences only — no ligand entity"
                    if can is False else
                    ("engine is not in the verified cofactor capability table"
                     if can is None else
                     ("supported" if COFACTORS_REACHABLE_VIA_HOSTED_COFOLD else
                      "the hosted co-fold route cannot express a cofactor; the "
                      "single-engine Boltz-2 batch surface can: "
                      "client.peptides.fold_batch(..., "
                      "partners=[{'type':'ligand','ccd':'ZN','copies':2}])"))
                ),
            }
        return out

    @staticmethod
    def _check_cofactors(engines: list[str], cofs: list[CofactorSpec],
                         allow_cofactor_absent: bool) -> list[str]:
        """Refuse, by default, to run a cofactor request the arms cannot honour.

        Silently dropping the cofactor is the failure mode this whole interface
        exists to prevent: on a metal-dependent target it returns a confident
        number for the wrong molecule.
        """
        if not cofs:
            return []
        sup = _EnsembleCommon.cofactor_support(engines)
        cannot = [e for e in engines if not sup[e]["reachable_today"]]
        if cannot and not allow_cofactor_absent:
            names = ", ".join(sorted(f"{c.ccd or 'SMILES'}" for c in cofs))
            raise LigandAIInvalidConfig(
                f"cofactor(s) {names} requested, but engine(s) {cannot} in "
                f"{engines} cannot receive them through the hosted co-fold route "
                f"({'; '.join(f'{e}: ' + sup[e]['reason'] for e in cannot)}). "
                "These folds would run WITHOUT the cofactor, which on a "
                "cofactor-dependent target scores a different molecule. "
                "Pass allow_cofactor_absent=True to run them anyway; every "
                "affected arm is then labelled cofactor_honoured=False and is "
                "never pooled with an arm that carried it. For a cofactor-bearing "
                "fold TODAY, the single-engine Boltz-2 batch surface carries one: "
                "client.peptides.fold_batch(..., partners=[{'type': 'ligand', "
                "'ccd': 'ZN', 'copies': 2}])."
            )
        return cannot

    @staticmethod
    def _build_result(bar: float, engines: list[str], metric: str,
                      per_peptide: dict[str, dict[str, Optional[float]]],
                      honoured: dict[str, bool],
                      failures: list[dict[str, Any]],
                      cofs: list[CofactorSpec],
                      credits: Optional[int], wall: Optional[float],
                      stopped: Optional[str]) -> EnsembleResult:
        rows = []
        for seq, vals in per_peptide.items():
            scored = {e: v for e, v in vals.items() if v is not None}
            above = sorted(e for e, v in scored.items() if v >= bar)
            rows.append(ConsensusRow(
                sequence=seq, per_engine=vals,
                n_engines_scored=len(scored),
                n_engines_above_bar=len(above),
                engines_above_bar=above,
                cofactor_honoured=dict(honoured),
                unanimous=bool(scored) and len(above) == len(scored),
                contested=0 < len(above) < len(scored),
            ))
        rows.sort(key=lambda r: (-r.n_engines_above_bar, r.sequence))
        return EnsembleResult(
            bar=bar, engines=list(engines), metric=metric, rows=rows,
            failures=failures, credits_spent=credits, wall_seconds=wall,
            stopped_reason=stopped,
            cofactors=[c.as_dict() for c in cofs],
        )


class Ensemble(Resource, _EnsembleCommon):
    """Ensemble co-fold: N peptides x M engines, priced before it runs."""

    def engines(self) -> dict[str, Any]:
        """Engines the server will accept, with their default trajectory counts."""
        return self._transport.request("GET", "/api/v1/cofold/engines")

    def estimate(
        self,
        engines: list[str],
        n_peptides: int = 1,
        *,
        per_engine: dict[str, Any] | None = None,
        per_engine_trajectories: dict[str, int] | None = None,
        per_engine_settings: dict[str, Any] | None = None,
        cofactors: Iterable[Any] | None = None,
        credits_per_usd: float = 100.0,
    ) -> EnsembleEstimate:
        """Projected spend and per-engine breakdown BEFORE submitting anything.

        Wraps ``POST /api/v1/cofold/estimate`` (the server's own pricing, from
        ``CREDIT_PRICING.cofold`` — per-engine rates with the bundle discount)
        and scales it by the peptide count. It is deliberately NOT a second cost
        model: no rate is held or re-derived client-side, so a price change on the
        platform reaches this estimate without an SDK release.

        ONE complex is quoted (``complexCount: 1``) and scaled by ``n_peptides``
        because :meth:`run` submits one co-fold JOB per peptide, and the server's
        minimum charge applies per job.

        Args:
            engines: engines to include in the ensemble.
            n_peptides: how many complexes will be folded.
            per_engine: the SAME ``{engine: {setting: value}}`` map you pass to
                :meth:`run` / ``peptides.cofold`` — sent as ``per_engine``, the
                key the server reads.
            per_engine_trajectories / per_engine_settings: shortcuts merged into
                ``per_engine``; see :func:`_per_engine_body`.
            cofactors: any cofactor the caller intends to pass — declared here
                so the estimate can WARN when arms cannot honour it, before
                money moves.
            credits_per_usd: display conversion only (platform rate: 100 cr = $1).
        """
        if n_peptides < 1:
            raise LigandAIInvalidConfig("n_peptides must be >= 1")
        body: dict[str, Any] = {"engines": list(engines), "complexCount": 1}
        pe = _per_engine_body(per_engine, per_engine_trajectories, per_engine_settings)
        if pe:
            body["per_engine"] = pe
        r = self._transport.request("POST", "/api/v1/cofold/estimate", json=body)
        per_complex = float(r.get("estimatedTotal") or 0)
        cofs = _norm_cofactors(cofactors)
        sup = self.cofactor_support(list(engines))
        warns = []
        if cofs:
            bad = [e for e in engines if not sup[e]["reachable_today"]]
            if bad:
                warns.append(
                    f"cofactor(s) {', '.join(c.ccd or 'SMILES' for c in cofs)} cannot "
                    f"reach {', '.join(bad)} — those arms would fold WITHOUT them"
                )
        return EnsembleEstimate(
            engines=list(engines),
            n_complexes=n_peptides,
            credits_total=int(round(per_complex * n_peptides)),
            credits_per_complex=per_complex,
            per_engine=list(r.get("perEngine") or []),
            discount_percent=float(r.get("discountPercent") or 0),
            usd_total=round(per_complex * n_peptides / credits_per_usd, 2),
            breakdown=str(r.get("breakdown") or ""),
            cofactor_arms={e: sup[e]["reachable_today"] for e in engines},
            warnings=warns,
        )

    def run(
        self,
        receptor: str | dict[str, Any],
        peptides: Sequence[str | dict[str, Any]],
        engines: list[str] = ["boltz2", "esmfold2", "protenix"],
        *,
        cofactors: Iterable[Any] | None = None,
        allow_cofactor_absent: bool = False,
        bar: float = DEFAULT_BAR,
        metric: str = "peptide_ipsae",
        max_credits: int | None = None,
        budget: Any | None = None,
        dry_run: bool = False,
        per_engine: dict[str, Any] | None = None,
        shared_msa: str | dict[str, Any] | None = None,
        gene: str | None = None,
        run_deltaforge: bool = False,
        poll_interval: float = 10.0,
        timeout: float | None = None,
        on_progress: Any | None = None,
    ) -> EnsembleResult | EnsembleEstimate:
        """Fold every peptide against the receptor on every engine, then agree.

        ⛔ COST IS CHECKED BEFORE ANY FOLD IS SUBMITTED. The projected spend
        comes from the server's own pricing endpoint; if it exceeds the ceiling
        the call raises and nothing is submitted. ``dry_run=True`` returns the
        estimate and stops.

        Args:
            receptor: receptor sequence (or a chain dict).
            peptides: candidate binder sequences.
            engines: 1-6 engines. Defaults to the three that return a PAE matrix,
                so ipSAE is computable on every arm.
            cofactors: CCD codes (``["ZN", "ZN"]``), dicts, or
                :class:`CofactorSpec`. Raises unless every requested arm can
                actually carry them — see :meth:`cofactor_support`.
            allow_cofactor_absent: proceed even though arms cannot carry the
                cofactor. Those arms come back ``cofactor_honoured=False`` and
                are never pooled with arms that carried it.
            bar: the shortlist threshold consensus is counted against.
            max_credits: hard ceiling. Also read from ``budget.max_credits``
                when a campaign ``Budget`` is passed, so the SDK campaign spec
                and this entry point share ONE ceiling rather than two.
            budget: optional ``ligandai.campaign.spec.Budget`` (duck-typed:
                ``max_credits``, ``per_iteration_credit_cap``,
                ``no_progress.patience_iterations``).
            dry_run: return the :class:`EnsembleEstimate` and submit nothing.

        Returns:
            :class:`EnsembleResult` — per-peptide per-engine values, the
            consensus histogram, and pairwise rank agreement with n stated.
        """
        import time

        peps = [
            (p if isinstance(p, str) else str(p.get("sequence") or "")).strip().upper()
            for p in peptides
        ]
        peps = [p for p in peps if p]
        if not peps:
            raise LigandAIInvalidConfig("peptides must contain at least one sequence")
        rec = receptor if isinstance(receptor, str) else str(receptor.get("sequence") or "")
        rec = rec.strip().upper()
        if not rec:
            raise LigandAIInvalidConfig("receptor sequence is empty")

        engines = [str(e).strip().lower() for e in engines]
        cofs = _norm_cofactors(cofactors)
        absent_on = self._check_cofactors(engines, cofs, allow_cofactor_absent)
        honoured = {e: (e not in absent_on and bool(cofs)) for e in engines}

        # --- ceiling, resolved from ONE place ---------------------------------
        ceiling = max_credits
        if ceiling is None and budget is not None:
            ceiling = getattr(budget, "max_credits", None)
        est = self.estimate(engines, len(peps), per_engine=per_engine, cofactors=cofs)
        if dry_run:
            return est
        if ceiling is not None and est.credits_total > ceiling:
            raise LigandAIInvalidConfig(
                f"projected {est.credits_total} credits for {len(peps)} peptides x "
                f"{len(engines)} engines exceeds the ceiling of {ceiling}. "
                "Nothing was submitted. Lower n, drop an engine, or raise "
                "max_credits deliberately."
            )

        patience = None
        if budget is not None:
            npr = getattr(budget, "no_progress", None)
            patience = getattr(npr, "patience_iterations", None)

        t0 = time.time()
        per_peptide: dict[str, dict[str, Optional[float]]] = {}
        failures: list[dict[str, Any]] = []
        consecutive_dead = 0
        stopped: Optional[str] = None
        cofold = self._client.peptides.cofold  # the shipping single-complex call

        for idx, pep in enumerate(peps):
            if timeout is not None and time.time() - t0 > timeout:
                stopped = "timeout"
                break
            chains = [{"id": "A", "sequence": rec, "kind": "receptor"},
                      {"id": "B", "sequence": pep, "kind": "peptide"}]
            vals: dict[str, Optional[float]] = {e: None for e in engines}
            try:
                job = cofold(chains, engines, per_engine=per_engine,
                             shared_msa=shared_msa, gene=gene,
                             run_deltaforge=run_deltaforge,
                             cofactors=[c.as_dict() for c in cofs] or None,
                             allow_cofactor_absent=allow_cofactor_absent)
                res = job.result(poll_interval=poll_interval)
                for eng, rec_e in (res.get("engines") or {}).items():
                    vals[str(eng).lower()] = _pick_metric(rec_e, metric)
                    _reconcile_honoured(honoured, str(eng).lower(), rec_e, cofs)
            except Exception as exc:              # one peptide must not kill the run
                failures.append({"sequence": pep, "stage": "cofold",
                                 "error": f"{type(exc).__name__}: {exc}"[:400]})
            per_peptide[pep] = vals
            if all(v is None for v in vals.values()):
                consecutive_dead += 1
            else:
                consecutive_dead = 0
            # ⛔ no-progress stop. Same rule shape as the campaign Budget's
            # NoProgressRule: if nothing at all is coming back, stop spending.
            if patience and consecutive_dead >= patience:
                stopped = "no_progress"
                break
            if on_progress:
                on_progress(idx + 1, len(peps), per_peptide)

        return self._build_result(
            bar, engines, metric, per_peptide, honoured, failures, cofs,
            credits=None, wall=round(time.time() - t0, 1), stopped=stopped,
        )


#: Where the hosted co-fold route puts each metric. Each engine's record is
#: ``{engine, ok, n_traj, trajectories: [TRAJ], best: TRAJ | None, settings, ...}``
#: (fastapi_backend/services/cofold_engine_adapters.py, "CANONICAL RETURN"), and
#: ``best`` is the trajectory with the highest ipTM among the ok ones. The TRAJ
#: keys below are the server's spelling; ``iPSAE`` / ``ipTM`` / ``iPAE`` are
#: restricted host-side to the PEPTIDE<->receptor interface, so ``iPSAE`` IS the
#: binder metric (``peptide_ipsae``). ``pLDDT`` is normalised to 0-1 server-side.
#: Nothing here maps a metric onto a different quantity: ``ipTM_complex`` (the
#: engine-native whole-complex value, contaminated by receptor<->receptor
#: contacts) is reachable ONLY by asking for ``iptm_complex``.
_BEST_TRAJ_KEYS: dict[str, tuple[str, ...]] = {
    "peptide_ipsae": ("iPSAE",),
    "iptm": ("ipTM",),
    "iptm_complex": ("ipTM_complex",),
    "ptm": ("pTM",),
    "plddt": ("pLDDT",),
}

#: Aliases that are THE SAME QUANTITY under a different worker's spelling, looked
#: up FLAT on the engine record (the shape single-engine workers return). The
#: hosted route does not use them — see :data:`_BEST_TRAJ_KEYS`. Nothing here maps
#: a metric onto a different quantity.
_METRIC_ALIASES: dict[str, tuple[str, ...]] = {
    "peptide_ipsae": ("peptide_ipsae", "peptideIpsae", "ipsae_peptide"),
    "ipsae": ("ipsae", "iPSAE", "overall_ipsae"),
    "iptm": ("iptm", "ipTM", "top_iptm", "iptm_complex"),
    "ptm": ("ptm", "pTM", "top_ptm"),
    "plddt": ("plddt", "mean_plddt", "top_plddt", "plddt_mean"),
}


def _as_number(v: Any) -> Optional[float]:
    if isinstance(v, dict):
        v = v.get("best", v.get("value"))
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    return None


def _pick_metric(engine_record: Any, metric: str) -> Optional[float]:
    """Pull ONE named metric out of an engine record.

    Reads the hosted route's shape first — ``record["best"][<TRAJ key>]``, e.g.
    ``best.iPSAE`` for ``peptide_ipsae`` — then falls back to a flat spelling on
    the record. ⛔ Before this read ``best``, it looked only for a top-level
    ``peptide_ipsae`` that the hosted route never emits, so every per-engine
    score came back None live and the consensus counted nothing.

    ``best`` is the engine's best structure by ipTM — the same one DeltaForge
    scores — so this is the engine's own answer, not the maximum over its
    trajectories (taking that would select on the metric being reported).

    ⛔ NEVER DEFAULTS TO 0.0 AND NEVER SUBSTITUTES A DIFFERENT QUANTITY. Both
    have shipped from production workers: a missing ipTM returned as a measured
    0.0 ("no interface"), a missing affinity returned as 0.0 (log10 IC50/uM = a
    1 uM binder), and a batch path that filled a missing ipSAE with
    plddt_interface/100 — a different quantity entirely — and then counted the
    result as elite. Here an absent metric is None, and asking for ipSAE gets
    ipSAE or nothing: the hosted route reports ONE iPSAE (peptide<->receptor), so
    the whole-complex ``ipsae`` metric is None there rather than served the
    peptide-interface value.
    """
    if not isinstance(engine_record, dict):
        return None
    best = engine_record.get("best")
    if isinstance(best, dict):
        for key in _BEST_TRAJ_KEYS.get(metric, ()):
            if key in best:
                v = _as_number(best[key])
                if v is not None:
                    return v
    keys = _METRIC_ALIASES.get(metric, (metric,))
    for key in keys:
        if key not in engine_record:
            continue
        v = _as_number(engine_record[key])
        if v is not None:
            return v
    return None


def _reconcile_honoured(honoured: dict[str, bool], engine: str,
                        engine_record: Any, cofs: list[CofactorSpec]) -> None:
    """Make ``cofactor_honoured`` the SERVER's statement, not the SDK's hope.

    The hosted route reports, per engine, ``cofactor.honoured``. A deployment that
    predates the ``cofactors`` field ignores it and folds the complex WITHOUT the
    cofactor — and then returns no ``cofactor`` block at all. Either way an arm
    the server did not confirm is marked not honoured, so it is never pooled with
    one that carried the cofactor. Only ever flips True -> False.
    """
    if not cofs or engine not in honoured:
        return
    cof = engine_record.get("cofactor") if isinstance(engine_record, dict) else None
    if not (isinstance(cof, dict) and cof.get("honoured") is True):
        honoured[engine] = False


class AsyncEnsemble(AsyncResource, _EnsembleCommon):
    """Async twin of :class:`Ensemble`."""

    async def engines(self) -> dict[str, Any]:
        return await self._transport.request("GET", "/api/v1/cofold/engines")

    async def estimate(
        self,
        engines: list[str],
        n_peptides: int = 1,
        *,
        per_engine: dict[str, Any] | None = None,
        per_engine_trajectories: dict[str, int] | None = None,
        per_engine_settings: dict[str, Any] | None = None,
        cofactors: Iterable[Any] | None = None,
        credits_per_usd: float = 100.0,
    ) -> EnsembleEstimate:
        if n_peptides < 1:
            raise LigandAIInvalidConfig("n_peptides must be >= 1")
        body: dict[str, Any] = {"engines": list(engines), "complexCount": 1}
        pe = _per_engine_body(per_engine, per_engine_trajectories, per_engine_settings)
        if pe:
            body["per_engine"] = pe
        r = await self._transport.request("POST", "/api/v1/cofold/estimate", json=body)
        per_complex = float(r.get("estimatedTotal") or 0)
        cofs = _norm_cofactors(cofactors)
        sup = self.cofactor_support(list(engines))
        warns = []
        if cofs:
            bad = [e for e in engines if not sup[e]["reachable_today"]]
            if bad:
                warns.append(
                    f"cofactor(s) {', '.join(c.ccd or 'SMILES' for c in cofs)} cannot "
                    f"reach {', '.join(bad)} — those arms would fold WITHOUT them"
                )
        return EnsembleEstimate(
            engines=list(engines), n_complexes=n_peptides,
            credits_total=int(round(per_complex * n_peptides)),
            credits_per_complex=per_complex,
            per_engine=list(r.get("perEngine") or []),
            discount_percent=float(r.get("discountPercent") or 0),
            usd_total=round(per_complex * n_peptides / credits_per_usd, 2),
            breakdown=str(r.get("breakdown") or ""),
            cofactor_arms={e: sup[e]["reachable_today"] for e in engines},
            warnings=warns,
        )

    async def run(
        self,
        receptor: str | dict[str, Any],
        peptides: Sequence[str | dict[str, Any]],
        engines: list[str] = ["boltz2", "esmfold2", "protenix"],
        *,
        cofactors: Iterable[Any] | None = None,
        allow_cofactor_absent: bool = False,
        bar: float = DEFAULT_BAR,
        metric: str = "peptide_ipsae",
        max_credits: int | None = None,
        budget: Any | None = None,
        dry_run: bool = False,
        per_engine: dict[str, Any] | None = None,
        shared_msa: str | dict[str, Any] | None = None,
        gene: str | None = None,
        run_deltaforge: bool = False,
        poll_interval: float = 10.0,
        timeout: float | None = None,
        concurrency: int = 4,
    ) -> EnsembleResult | EnsembleEstimate:
        """Async ensemble run. Folds up to ``concurrency`` complexes at a time."""
        import asyncio
        import time

        peps = [
            (p if isinstance(p, str) else str(p.get("sequence") or "")).strip().upper()
            for p in peptides
        ]
        peps = [p for p in peps if p]
        if not peps:
            raise LigandAIInvalidConfig("peptides must contain at least one sequence")
        rec = receptor if isinstance(receptor, str) else str(receptor.get("sequence") or "")
        rec = rec.strip().upper()
        if not rec:
            raise LigandAIInvalidConfig("receptor sequence is empty")

        engines = [str(e).strip().lower() for e in engines]
        cofs = _norm_cofactors(cofactors)
        absent_on = self._check_cofactors(engines, cofs, allow_cofactor_absent)
        honoured = {e: (e not in absent_on and bool(cofs)) for e in engines}

        ceiling = max_credits
        if ceiling is None and budget is not None:
            ceiling = getattr(budget, "max_credits", None)
        est = await self.estimate(engines, len(peps), per_engine=per_engine, cofactors=cofs)
        if dry_run:
            return est
        if ceiling is not None and est.credits_total > ceiling:
            raise LigandAIInvalidConfig(
                f"projected {est.credits_total} credits exceeds the ceiling of "
                f"{ceiling}. Nothing was submitted."
            )

        t0 = time.time()
        per_peptide: dict[str, dict[str, Optional[float]]] = {}
        failures: list[dict[str, Any]] = []
        sem = asyncio.Semaphore(max(1, concurrency))
        cofold = self._client.peptides.cofold

        # ⛔ `timeout` was accepted by this signature and never read, while the
        # sync twin at Ensemble.run enforced it — despite the class comment
        # promising "the sync and async twins cannot drift". A caller passing
        # timeout= got an async run that ignored the deadline AND a result
        # whose `stopped` was hard-coded None, so it could not even report
        # that it had overrun. Wired up here rather than warned away: the
        # capability is real and the sync half already has it.
        # [bd-dre-rp11j.3]
        stopped: Optional[str] = None

        def _deadline_passed() -> bool:
            return timeout is not None and (time.time() - t0) > timeout

        async def one(pep: str) -> None:
            nonlocal stopped
            async with sem:
                # Checked INSIDE the semaphore: a peptide queued behind the
                # concurrency limit must not start after the budget is spent.
                if _deadline_passed():
                    stopped = "timeout"
                    return
                chains = [{"id": "A", "sequence": rec, "kind": "receptor"},
                          {"id": "B", "sequence": pep, "kind": "peptide"}]
                vals: dict[str, Optional[float]] = {e: None for e in engines}
                try:
                    job = await cofold(chains, engines, per_engine=per_engine,
                                       shared_msa=shared_msa, gene=gene,
                                       run_deltaforge=run_deltaforge,
                                       cofactors=[c.as_dict() for c in cofs] or None,
                                       allow_cofactor_absent=allow_cofactor_absent)
                    res = await job.result(poll_interval=poll_interval)
                    for eng, rec_e in (res.get("engines") or {}).items():
                        vals[str(eng).lower()] = _pick_metric(rec_e, metric)
                        _reconcile_honoured(honoured, str(eng).lower(), rec_e, cofs)
                except Exception as exc:
                    failures.append({"sequence": pep, "stage": "cofold",
                                     "error": f"{type(exc).__name__}: {exc}"[:400]})
                per_peptide[pep] = vals

        await asyncio.gather(*(one(p) for p in peps))
        # `stopped` now carries "timeout" when the deadline cut the run short,
        # so a partial ensemble is distinguishable from a complete one. A
        # short result that claims stopped=None is a measurement; a short
        # result that says "timeout" is a truncation. They are not the same
        # and must not print the same.
        return self._build_result(
            bar, engines, metric, per_peptide, honoured, failures, cofs,
            credits=None, wall=round(time.time() - t0, 1), stopped=stopped,
        )
