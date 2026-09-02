# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Poses in, one record per design out.

Two rules govern every aggregation here.

RECEPTOR IDENTITY IS A HASH.  Poses are grouped by ``(design, receptor_sha256)``
— content — and never by arm, directory or file name.  A pose whose receptor
cannot be resolved is counted in a named bucket, not dropped silently and not
folded into the on-target numbers.

A RATE IS COUNTED IN INDEPENDENT SEED UNITS.  The failure rate that feeds the
Wilson interval counts one observation per (design, seed unit), taking the
MEDIAN over that unit's diffusion samples first.  Counting the samples inside a
single seed as independent runs is what produced an apparent 44% unreliability
that was winner's curse.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from typing import Any, Iterable, Mapping, Sequence

from .identity import sha256_seq
from .platform import PoseResult
from .record import (AffinityBlock, CofactorControlBlock, ConstructComparisonBlock,
                     DesignRecord, ImmunogenicityBlock, InterfaceBlock,
                     LinkerSiteBlock, ReliabilityBlock, SelectivityBlock,
                     StabilityBlock)
from .spec import CampaignSpec
from .values import Measured, Status


def _med(vals: Sequence[float]) -> float | None:
    return statistics.median(vals) if vals else None


def _m(vals: Sequence[float], denom: int, method: str,
       units: str | None = None, what: str = "") -> Measured:
    if not vals:
        return Measured.absent(what or "no measured pose", denominator=denom)
    return Measured(statistics.median(vals), Status.MEASURED, n=len(vals),
                    denominator=denom, units=units, method=method)


def _units(poses: Sequence[PoseResult]) -> dict[str, list[PoseResult]]:
    by: dict[str, list[PoseResult]] = defaultdict(list)
    for p in poses:
        by[p.seed_unit].append(p)
    return dict(by)


def _unit_medians(poses: Sequence[PoseResult], attr: str = "peptide_ipsae") -> list[float]:
    """One number per independent seed unit: the median over its samples."""
    out: list[float] = []
    for _u, ps in sorted(_units(poses).items()):
        vals = [float(getattr(p, attr).value) for p in ps
                if getattr(p, attr).is_measured]
        if vals:
            out.append(statistics.median(vals))
    return out


class AggregationReport:
    """What the aggregation saw, including what it refused to pool."""

    def __init__(self) -> None:
        self.n_poses_in = 0
        self.n_unresolved_receptor = 0
        self.n_off_construct = 0
        self.receptor_counts: dict[str, int] = defaultdict(int)
        self.n_designs = 0
        self.warnings: list[str] = []

    def as_dict(self) -> dict[str, Any]:
        return {"n_poses_in": self.n_poses_in,
                "n_unresolved_receptor": self.n_unresolved_receptor,
                "n_off_construct": self.n_off_construct,
                "receptor_counts": dict(self.receptor_counts),
                "n_designs": self.n_designs,
                "warnings": list(self.warnings)}


def build_records(
    spec: CampaignSpec,
    poses: Sequence[PoseResult],
    *,
    design_meta: Mapping[str, Mapping[str, Any]] | None = None,
    developability: Mapping[str, Mapping[str, Any]] | None = None,
    affinity: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[list[DesignRecord], AggregationReport]:
    rep = AggregationReport()
    rep.n_poses_in = len(poses)
    meta = dict(design_meta or {})

    on_hash = sha256_seq(spec.target.construct.sequence)
    off_hash = {o.gene: sha256_seq(o.construct.sequence) for o in spec.off_targets}
    comp_hash = {c.name: sha256_seq(c.sequence) for c in spec.target.comparison_constructs}
    known = {on_hash: spec.target.construct.name}
    known.update({h: g for g, h in off_hash.items()})
    known.update({h: n for n, h in comp_hash.items()})

    # ---- bucket every pose by (design, receptor hash, cofactor state) ----
    buckets: dict[tuple[str, str, bool], list[PoseResult]] = defaultdict(list)
    seqs: dict[str, str] = {}
    for p in poses:
        rh = (p.identity.receptor_sha256 if p.identity and p.identity.receptor_sha256
              else sha256_seq(_construct_seq(spec, p.receptor_construct) or ""))
        if not rh:
            rep.n_unresolved_receptor += 1
            continue
        rep.receptor_counts[known.get(rh, f"UNKNOWN:{rh[:12]}")] += 1
        if rh not in known:
            rep.n_off_construct += 1
            continue
        has_cof = bool(p.cofactors_present)
        buckets[(p.design_id, rh, has_cof)].append(p)
        seqs[p.design_id] = p.sequence

    if rep.n_off_construct:
        rep.warnings.append(
            f"{rep.n_off_construct} pose(s) carried a receptor that is not the target, "
            f"an off-target or a comparison construct declared in the spec. They were "
            f"NOT pooled. Receptor census: {dict(rep.receptor_counts)}")
    if rep.n_unresolved_receptor:
        rep.warnings.append(
            f"{rep.n_unresolved_receptor} pose(s) had no resolvable receptor and were "
            f"excluded; they are unmeasured, not failing.")

    cof_names = tuple(c.name for c in spec.target.cofactors)
    want_cof = bool(cof_names)
    rel = spec.reliability
    records: list[DesignRecord] = []

    for design_id, seq in sorted(seqs.items()):
        prod = buckets.get((design_id, on_hash, want_cof), [])
        if not prod and want_cof:
            prod = buckets.get((design_id, on_hash, False), [])
        if not prod:
            continue

        vals = [float(p.peptide_ipsae.value) for p in prod if p.peptide_ipsae.is_measured]
        unit_meds = _unit_medians(prod)
        n_units = len(_units(prod))
        iface = InterfaceBlock(
            peptide_ipsae_median=_m(vals, len(prod), "boltz2:median_over_poses"),
            peptide_ipsae_best=(Measured(max(vals), Status.MEASURED, n=len(vals),
                                         denominator=len(prod), method="boltz2:max")
                                if vals else Measured.absent("no scored pose", len(prod))),
            peptide_ipsae_worst=(Measured(min(vals), Status.MEASURED, n=len(vals),
                                          denominator=len(prod), method="boltz2:min")
                                 if vals else Measured.absent("no scored pose", len(prod))),
            iptm_median=_m([float(p.iptm.value) for p in prod if p.iptm.is_measured],
                           len(prod), "boltz2"),
            complex_plddt_median=_m([float(p.complex_plddt.value) for p in prod
                                     if p.complex_plddt.is_measured], len(prod),
                                    "boltz2", "0-100"),
            peptide_plddt_median=_m([float(p.peptide_plddt.value) for p in prod
                                     if p.peptide_plddt.is_measured], len(prod),
                                    "boltz2", "0-100"),
            peptide_interface_plddt_median=_m(
                [float(p.peptide_interface_plddt.value) for p in prod
                 if p.peptide_interface_plddt.is_measured], len(prod), "boltz2", "0-100"),
            n_poses=len(prod), n_independent_seed_units=n_units,
            receptor_sha256=on_hash,
            pose_spread=(Measured(max(vals) - min(vals), Status.MEASURED, n=len(vals),
                                  denominator=len(prod), method="range")
                         if len(vals) > 1 else Measured.absent("fewer than 2 scored poses")),
        )

        reliability = None
        if unit_meds:
            fails = sum(1 for v in unit_meds if v < rel.failure_threshold)
            reliability = ReliabilityBlock.build(
                n_runs=len(unit_meds), n_failures=fails,
                threshold_metric=rel.failure_metric,
                threshold_value=rel.failure_threshold,
                certification_bar=rel.certification_bar,
                seed_units=n_units, confidence=rel.confidence)

        sel: list[SelectivityBlock] = []
        for o in spec.off_targets:
            oh = off_hash[o.gene]
            off_poses = buckets.get((design_id, oh, want_cof), []) or \
                buckets.get((design_id, oh, False), [])
            on_med = _med(unit_meds)
            off_meds = _unit_medians(off_poses)
            off_med = _med(off_meds)
            if on_med is None or off_med is None:
                margin = Measured.absent(
                    f"no {o.gene} leg for this design — the margin is UNMEASURED, "
                    f"which is not the same as small", denominator=len(prod))
            else:
                margin = Measured(on_med - off_med, Status.MEASURED,
                                  n=min(len(unit_meds), len(off_meds)),
                                  denominator=max(len(unit_meds), len(off_meds)),
                                  method="paired_unit_medians")
            sel.append(SelectivityBlock(
                off_target=o.gene, off_target_receptor_sha256=oh,
                on_target_leg=(Measured(on_med, Status.MEASURED, n=len(unit_meds))
                               if on_med is not None else Measured.absent("no on-target leg")),
                off_target_leg=(Measured(off_med, Status.MEASURED, n=len(off_meds))
                                if off_med is not None else Measured.absent(f"no {o.gene} leg")),
                margin=margin, n_runs_on=len(unit_meds), n_runs_off=len(off_meds),
                panel_complete=bool(off_meds and unit_meds)))

        cof_block = None
        if cof_names:
            withs = _unit_medians(buckets.get((design_id, on_hash, True), []))
            withouts = _unit_medians(buckets.get((design_id, on_hash, False), []))
            wm, om = _med(withs), _med(withouts)
            paired = bool(withs and withouts)
            cof_block = CofactorControlBlock(
                cofactors=cof_names,
                with_cofactor=(Measured(wm, Status.MEASURED, n=len(withs))
                               if wm is not None else Measured.absent("no cofactor-bearing leg")),
                without_cofactor=(Measured(om, Status.MEASURED, n=len(withouts))
                                  if om is not None else
                                  Measured.absent("no cofactor-free leg has been folded")),
                delta=(Measured(wm - om, Status.MEASURED, n=min(len(withs), len(withouts)),
                                method="paired_unit_medians")
                       if paired else Measured.absent(
                           "unpaired — a delta from two arms of different depth is not "
                           "a paired control")),
                n_runs_with=len(withs), n_runs_without=len(withouts), paired=paired)

        cmp_block = None
        for c in spec.target.comparison_constructs:
            ch = comp_hash[c.name]
            cposes = buckets.get((design_id, ch, want_cof), []) or \
                buckets.get((design_id, ch, False), [])
            cmeds = _unit_medians(cposes)
            a, b = _med(unit_meds), _med(cmeds)
            if b is None:
                continue
            cmp_block = ConstructComparisonBlock(
                design_construct=spec.target.construct.name, comparison_construct=c.name,
                design_construct_sha256=on_hash, comparison_construct_sha256=ch,
                leg_on_design_construct=(Measured(a, Status.MEASURED, n=len(unit_meds))
                                         if a is not None else Measured.absent("no leg")),
                leg_on_comparison_construct=Measured(b, Status.MEASURED, n=len(cmeds)),
                delta=(Measured(a - b, Status.MEASURED, n=min(len(unit_meds), len(cmeds)))
                       if a is not None else Measured.absent("no design-construct leg")),
                contact_jaccard=Measured.absent("contact overlap not computed in this pass"),
                numbering_frame_map=(f"{spec.target.construct.numbering_frame} -> "
                                     f"{c.numbering_frame}") or None)
            break

        md = meta.get(design_id, {})
        dev = (developability or {}).get(seq) or (developability or {}).get(design_id)
        immuno, stab = _developability_blocks(dev, md.get("topology", "linear"))
        aff = _affinity_block((affinity or {}).get(design_id))

        records.append(DesignRecord(
            design_id=design_id, sequence=seq, length=len(seq),
            topology=md.get("topology", "linear"),
            molecule_class=md.get("molecule_class", md.get("topology", "linear")),
            arm=md.get("arm"), target=spec.target.gene,
            construct=spec.target.construct.name,
            interface=iface, reliability=reliability, selectivity=tuple(sel),
            cofactor_control=cof_block, immunogenicity=immuno, stability=stab,
            affinity=aff, linker_sites=None, construct_comparison=cmp_block,
            provenance={"controls": md.get("controls", []),
                        "peptide_class": md.get("peptide_class"),
                        "spec_fingerprint": spec.fingerprint()[:16]}))

    rep.n_designs = len(records)
    return records, rep


def _construct_seq(spec: CampaignSpec, name: str | None) -> str | None:
    if not name:
        return None
    if name == spec.target.construct.name:
        return spec.target.construct.sequence
    for o in spec.off_targets:
        if o.construct.name == name or o.gene == name:
            return o.construct.sequence
    for c in spec.target.comparison_constructs:
        if c.name == name:
            return c.sequence
    return None


def _pick(d: Mapping[str, Any] | None, *keys: str, units: str | None = None,
          what: str = "") -> Measured:
    if not d:
        return Measured.absent(what or "developability was never scored")
    for k in keys:
        if d.get(k) is not None:
            v = d[k]
            if isinstance(v, (list, tuple)):
                return Measured(len(v), Status.MEASURED, n=len(v), units="count",
                                method=f"developability:{k}")
            if isinstance(v, str):
                return Measured(v, Status.MEASURED, n=1, method=f"developability:{k}")
            try:
                return Measured(float(v), Status.MEASURED, n=1, units=units,
                                method=f"developability:{k}")
            except (TypeError, ValueError):
                continue
    return Measured.absent(f"none of {keys} present in the developability record")


def _developability_blocks(dev: Mapping[str, Any] | None, topology: str
                           ) -> tuple[ImmunogenicityBlock | None, StabilityBlock | None]:
    if not dev or dev.get("__error__"):
        return None, None
    immuno = ImmunogenicityBlock(
        grade=_pick(dev, "immuno_grade", "immunogenicity_grade"),
        tap_score=_pick(dev, "tap_transport_score", "tap_score"),
        bcr_epitope_count=_pick(dev, "bcr_epitope_score", "bcr_epitope_count"),
        tcr_epitope_count=_pick(dev, "tcr_contact_score", "tcr_epitope_count"),
        mhc_i_count=_pick(dev, "mhc_i_epitope_count", "mhc_i_epitopes"),
        mhc_ii_count=_pick(dev, "mhc_ii_epitope_count", "mhc_ii_epitopes"),
        population_coverage=_pick(dev, "population_coverage_pct", "population_coverage",
                                  units="%"),
        alleles_tested=int(dev.get("alleles_tested") or 0))
    cyclic = topology in ("cyclic", "disulfide", "macrocycle")
    hl = _pick(dev, "predicted_halflife_hours", units="h")
    if cyclic and not hl.is_measured:
        hl = Measured.not_applicable(
            "half-life is not defensible for a macrocycle here: the N-end-rule terms "
            "do not apply and the trained estimator's corpus is linear")
    stab = StabilityBlock(
        proteolytic_stability=_pick(dev, "stability_composite", "proteolytic_grade",
                                    "stability_grade"),
        cleavage_risk_per_bond=_pick(dev, "cleavage_risk_per_bond"),
        n_bonds_scored=_pick(dev, "cleavage_site_density", "n_bonds_scored"),
        half_life=hl, half_life_matrix=dev.get("halflife_matrix"),
        # ⛔ D4: the half-life score ranked NEGATIVELY against measured data on every
        # stratum. Until a DirectionEvidence says otherwise it is reportable but
        # never rankable.
        half_life_direction_validated=False)
    return immuno, stab


def _affinity_block(aff: Mapping[str, Any] | None) -> AffinityBlock | None:
    if not aff or aff.get("__error__"):
        return None
    kd = _pick(aff, "kd_nm", "kdNm", "predicted_kd_nm", "predicted_kd", units="nM")
    return AffinityBlock(predicted_kd_nm=kd,
                         model_version=aff.get("scorerVersion") or aff.get("version"))
