# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""The canonical per-design record.

One row per design, carrying every field the Avammune/ENPP3 campaign produced —
each of which was added only after somebody asked for it.  Every scalar is a
:class:`~.values.Measured`, so ABSENT, FAILED and NOT_APPLICABLE are all
distinguishable from a measured 0.0, everywhere, without exception.

Nothing here fills a default in.  A field that was never run is
``Measured.absent(...)`` with a denominator saying how many observations it
would have taken, and every block reports its own coverage.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Any, Iterable, Mapping, Sequence

from .values import Measured, Status, WilsonInterval, wilson, runs_needed_for_upper_bound
from .identity import PoseIdentity


def _m(x: Any) -> dict[str, Any] | None:
    return x.as_dict() if isinstance(x, Measured) else x


# ---------------------------------------------------------------------------
# Blocks
# ---------------------------------------------------------------------------

@dataclass
class InterfaceBlock:
    """Boltz-2 interface metrics, aggregated ACROSS independent runs.

    ``peptide_ipsae_median`` is the reportable statistic (never a single fold —
    on this platform 53 of 80 peptides straddled the 0.67 gate WITHIN their own
    four diffusion samples).  best/worst travel with it so the spread is visible.
    """

    peptide_ipsae_median: Measured
    peptide_ipsae_best: Measured
    peptide_ipsae_worst: Measured
    iptm_median: Measured
    complex_plddt_median: Measured
    peptide_plddt_median: Measured
    peptide_interface_plddt_median: Measured
    n_poses: int = 0
    n_independent_seed_units: int = 0
    receptor_sha256: str | None = None
    pose_spread: Measured | None = None

    def as_dict(self) -> dict[str, Any]:
        d = {k: _m(v) for k, v in asdict(self).items()}
        return d


@dataclass
class ReliabilityBlock:
    """Reproducibility as a RATE with an interval — never a label.

    ``fold_failure_rate`` is the fraction of independent runs whose peptide
    ipSAE fell below the interface threshold.  ``certified`` is true only when
    the Wilson 95% UPPER bound is at or below the campaign's bar.  With zero
    failures, 8 runs bound the rate no tighter than 0.324 and 22 runs at 0.149:
    an 8-run read can EXCLUDE but cannot CERTIFY, and this block says which.
    """

    n_runs: int
    n_failures: int
    threshold_metric: str
    threshold_value: float
    interval: WilsonInterval | None
    certification_bar: float
    certified: bool | None            # None = not enough runs to decide
    runs_needed_to_certify: int | None
    seed_units: int = 0
    note: str = ""

    @classmethod
    def build(cls, *, n_runs: int, n_failures: int, threshold_metric: str,
              threshold_value: float, certification_bar: float,
              seed_units: int = 0, confidence: float = 0.95) -> "ReliabilityBlock":
        iv = wilson(n_failures, n_runs, confidence)
        if iv is None:
            return cls(n_runs, n_failures, threshold_metric, threshold_value, None,
                       certification_bar, None, None, seed_units,
                       "no runs — reliability is unmeasured, not zero")
        certified = iv.upper <= certification_bar
        need = None
        if not certified:
            need = runs_needed_for_upper_bound(certification_bar, iv.point, confidence)
        note = ""
        if not certified and need is None:
            note = (f"at the observed rate {iv.point:.3f} the Wilson upper bound never "
                    f"reaches {certification_bar:.3f} — this molecule cannot certify at "
                    f"this bar without the rate itself falling")
        elif not certified:
            note = (f"bound {iv.upper:.4f} > bar {certification_bar:.3f}; "
                    f"~{need} runs at the observed rate would reach it")
        return cls(n_runs, n_failures, threshold_metric, threshold_value, iv,
                   certification_bar, certified, need, seed_units, note)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["interval"] = self.interval.as_dict() if self.interval else None
        return d


@dataclass
class SelectivityBlock:
    """Per-off-target margin, measured on the SAME construct as the on-target leg.

    ``margin`` is on_target_leg - off_target_leg.  A margin computed from a
    partial panel inflates the worst case, so ``panel_complete`` records whether
    every design in the cohort got every comparator.
    """

    off_target: str
    off_target_receptor_sha256: str | None
    on_target_leg: Measured
    off_target_leg: Measured
    margin: Measured
    n_runs_on: int = 0
    n_runs_off: int = 0
    panel_complete: bool = False
    statistic: str = "median"

    def as_dict(self) -> dict[str, Any]:
        return {k: _m(v) for k, v in asdict(self).items()}


@dataclass
class CofactorControlBlock:
    """Paired refold with the catalytic ions removed.

    Paired PER PEPTIDE: the same sequence, the same receptor construct, cofactors
    present vs absent.  ``delta`` is (with - without).
    """

    cofactors: tuple[str, ...]
    with_cofactor: Measured
    without_cofactor: Measured
    delta: Measured
    n_runs_with: int = 0
    n_runs_without: int = 0
    paired: bool = False

    def as_dict(self) -> dict[str, Any]:
        d = {k: _m(v) for k, v in asdict(self).items()}
        d["cofactors"] = list(self.cofactors)
        return d


@dataclass
class ImmunogenicityBlock:
    grade: Measured
    tap_score: Measured
    bcr_epitope_count: Measured
    tcr_epitope_count: Measured
    mhc_i_count: Measured
    mhc_ii_count: Measured
    population_coverage: Measured
    alleles_tested: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {k: _m(v) for k, v in asdict(self).items()}


@dataclass
class StabilityBlock:
    proteolytic_stability: Measured
    cleavage_risk_per_bond: Measured           # list-valued Measured or absent
    n_bonds_scored: Measured
    half_life: Measured
    half_life_matrix: str | None = None        # 'human_serum' etc
    half_life_direction_validated: bool = False  # D4: false => do not rank on it

    def as_dict(self) -> dict[str, Any]:
        return {k: _m(v) for k, v in asdict(self).items()}


@dataclass
class AffinityBlock:
    """DeltaForge post-fold ranking.  Only ever computed on the gate-PASSING set."""

    predicted_kd_nm: Measured
    model_version: str | None = None
    scored_population: str | None = None       # the Population name it ran on
    population_coverage: float | None = None   # fraction of that population scored

    def as_dict(self) -> dict[str, Any]:
        return {k: _m(v) for k, v in asdict(self).items()}


@dataclass
class LinkerSiteBlock:
    """Attachment sites from contact frequency + solvent exposure + trajectory spread."""

    recommended_positions: tuple[int, ...] = ()
    contact_frequency: Measured | None = None
    solvent_exposure: Measured | None = None
    trajectory_spread: Measured | None = None
    numbering_frame: str | None = None
    n_poses_used: int = 0

    def as_dict(self) -> dict[str, Any]:
        d = {k: _m(v) for k, v in asdict(self).items()}
        d["recommended_positions"] = list(self.recommended_positions)
        return d


@dataclass
class ConstructComparisonBlock:
    """Fragment designed against vs the customer's full-length construct."""

    design_construct: str
    comparison_construct: str
    design_construct_sha256: str | None
    comparison_construct_sha256: str | None
    leg_on_design_construct: Measured
    leg_on_comparison_construct: Measured
    delta: Measured
    contact_jaccard: Measured
    numbering_frame_map: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {k: _m(v) for k, v in asdict(self).items()}


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------

@dataclass
class DesignRecord:
    """One design.  Every optional block is None only when it does not apply;
    an unmeasured VALUE inside a present block is ``Measured.absent``."""

    design_id: str
    sequence: str
    length: int
    topology: str                       # 'linear' | 'cyclic' | 'disulfide' | ...
    molecule_class: str                 # feeds Scorer.applies_to (D3)
    arm: str | None                     # METADATA ONLY — never identity (D7)
    target: str
    construct: str
    interface: InterfaceBlock
    reliability: ReliabilityBlock | None = None
    selectivity: tuple[SelectivityBlock, ...] = ()
    cofactor_control: CofactorControlBlock | None = None
    immunogenicity: ImmunogenicityBlock | None = None
    stability: StabilityBlock | None = None
    affinity: AffinityBlock | None = None
    linker_sites: LinkerSiteBlock | None = None
    construct_comparison: ConstructComparisonBlock | None = None
    gate_results: dict[str, str] = field(default_factory=dict)
    identities: tuple[PoseIdentity, ...] = ()
    provenance: dict[str, Any] = field(default_factory=dict)

    # -- coverage -------------------------------------------------------
    BLOCKS = ("reliability", "selectivity", "cofactor_control", "immunogenicity",
              "stability", "affinity", "linker_sites", "construct_comparison")

    def block_coverage(self) -> dict[str, bool]:
        return {b: bool(getattr(self, b)) for b in self.BLOCKS}

    def missing_blocks(self) -> tuple[str, ...]:
        return tuple(b for b, present in self.block_coverage().items() if not present)

    def field_coverage(self) -> dict[str, float]:
        """Fraction of the scalars in each present block that are MEASURED."""
        out: dict[str, float] = {}
        for name in ("interface",) + self.BLOCKS:
            blk = getattr(self, name)
            items = blk if isinstance(blk, tuple) else ([blk] if blk else [])
            total = 0
            got = 0
            for b in items:
                for v in vars(b).values():
                    if isinstance(v, Measured):
                        total += 1
                        got += 1 if v.is_measured else 0
            out[name] = (got / total) if total else 0.0
        return out

    @property
    def certified(self) -> bool | None:
        return self.reliability.certified if self.reliability else None

    def runs_needed_to_certify_hint(self) -> int | None:
        """How many more runs would close this design's interval, if it can close."""
        if not self.reliability:
            return None
        return self.reliability.runs_needed_to_certify

    def margin_against(self, off_target: str) -> Measured:
        for s in self.selectivity:
            if s.off_target == off_target:
                return s.margin
        return Measured.absent(f"no panel leg against {off_target}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "design_id": self.design_id,
            "sequence": self.sequence,
            "length": self.length,
            "topology": self.topology,
            "molecule_class": self.molecule_class,
            "arm": self.arm,
            "arm_is_metadata_only": True,
            "target": self.target,
            "construct": self.construct,
            "interface": self.interface.as_dict(),
            "reliability": self.reliability.as_dict() if self.reliability else None,
            "selectivity": [s.as_dict() for s in self.selectivity],
            "cofactor_control": self.cofactor_control.as_dict() if self.cofactor_control else None,
            "immunogenicity": self.immunogenicity.as_dict() if self.immunogenicity else None,
            "stability": self.stability.as_dict() if self.stability else None,
            "affinity": self.affinity.as_dict() if self.affinity else None,
            "linker_sites": self.linker_sites.as_dict() if self.linker_sites else None,
            "construct_comparison": (self.construct_comparison.as_dict()
                                     if self.construct_comparison else None),
            "gate_results": dict(self.gate_results),
            "certified": self.certified,
            "block_coverage": self.block_coverage(),
            "missing_blocks": list(self.missing_blocks()),
            "field_coverage": self.field_coverage(),
            "n_identities": len(self.identities),
            "receptor_sha256": self.interface.receptor_sha256,
            "provenance": dict(self.provenance),
        }


# ---------------------------------------------------------------------------
# Flat export — every field, with an explicit null and a denominator
# ---------------------------------------------------------------------------

_FLAT_SPEC: tuple[tuple[str, str], ...] = (
    ("interface.peptide_ipsae_median", "peptide_ipsae_median"),
    ("interface.peptide_ipsae_best", "peptide_ipsae_best"),
    ("interface.peptide_ipsae_worst", "peptide_ipsae_worst"),
    ("interface.iptm_median", "iptm_median"),
    ("interface.complex_plddt_median", "complex_plddt_median"),
    ("interface.peptide_plddt_median", "peptide_plddt_median"),
    ("interface.peptide_interface_plddt_median", "peptide_interface_plddt_median"),
    ("immunogenicity.grade", "immuno_grade"),
    ("immunogenicity.tap_score", "immuno_tap"),
    ("immunogenicity.bcr_epitope_count", "immuno_bcr_epitopes"),
    ("immunogenicity.tcr_epitope_count", "immuno_tcr_epitopes"),
    ("immunogenicity.mhc_i_count", "immuno_mhc_i"),
    ("immunogenicity.mhc_ii_count", "immuno_mhc_ii"),
    ("immunogenicity.population_coverage", "immuno_population_coverage"),
    ("stability.proteolytic_stability", "proteolytic_stability"),
    ("stability.half_life", "half_life"),
    ("stability.n_bonds_scored", "cleavage_bonds_scored"),
    ("affinity.predicted_kd_nm", "deltaforge_predicted_kd_nm"),
)


def _dig(rec: DesignRecord, path: str) -> Measured | None:
    obj: Any = rec
    for part in path.split("."):
        obj = getattr(obj, part, None)
        if obj is None:
            return None
    return obj if isinstance(obj, Measured) else None


def flatten(rec: DesignRecord) -> dict[str, Any]:
    """One flat row.  Each metric emits VALUE, STATUS and N columns.

    ``<m>`` is None when unmeasured, ``<m>_status`` says which kind of
    unmeasured, and ``<m>_n`` is the denominator.  A consumer that reads only
    the value column still cannot confuse absent with zero, because absent is
    an empty cell and zero is ``0.0``.
    """
    row: dict[str, Any] = {
        "design_id": rec.design_id,
        "sequence": rec.sequence,
        "length": rec.length,
        "topology": rec.topology,
        "molecule_class": rec.molecule_class,
        "target": rec.target,
        "construct": rec.construct,
        "receptor_sha256": rec.interface.receptor_sha256,
        "arm_metadata_only": rec.arm,
        "n_poses": rec.interface.n_poses,
        "n_seed_units": rec.interface.n_independent_seed_units,
    }
    for path, col in _FLAT_SPEC:
        m = _dig(rec, path)
        if m is None:
            row[col] = None
            row[f"{col}_status"] = "block_absent"
            row[f"{col}_n"] = 0
        else:
            row[col] = m.value if m.is_measured else None
            row[f"{col}_status"] = m.status.value
            row[f"{col}_n"] = m.n if m.n is not None else 0
    r = rec.reliability
    row["reliability_n_runs"] = r.n_runs if r else 0
    row["reliability_n_failures"] = r.n_failures if r else None
    row["fold_failure_rate"] = r.interval.point if (r and r.interval) else None
    row["fold_failure_ci_low"] = r.interval.lower if (r and r.interval) else None
    row["fold_failure_ci_high"] = r.interval.upper if (r and r.interval) else None
    row["certification_bar"] = r.certification_bar if r else None
    row["certified"] = r.certified if r else None
    row["certified_status"] = ("certified" if (r and r.certified)
                               else "not_certified" if (r and r.certified is False)
                               else "unmeasured")
    row["runs_needed_to_certify"] = r.runs_needed_to_certify if r else None
    for s in rec.selectivity:
        p = f"margin_vs_{s.off_target}"
        row[p] = s.margin.value if s.margin.is_measured else None
        row[f"{p}_status"] = s.margin.status.value
        row[f"{p}_n_on"] = s.n_runs_on
        row[f"{p}_n_off"] = s.n_runs_off
        row[f"{p}_panel_complete"] = s.panel_complete
    c = rec.cofactor_control
    row["cofactor_control_present"] = c is not None
    row["cofactor_delta"] = c.delta.value if (c and c.delta.is_measured) else None
    row["cofactor_delta_status"] = c.delta.status.value if c else "block_absent"
    row["cofactor_paired"] = c.paired if c else None
    row["linker_sites"] = (",".join(str(i) for i in rec.linker_sites.recommended_positions)
                           if rec.linker_sites else None)
    row["missing_blocks"] = ";".join(rec.missing_blocks())
    for k, v in rec.gate_results.items():
        row[f"gate__{k}"] = v
    return row


def flat_columns(records: Sequence[DesignRecord]) -> list[str]:
    cols: list[str] = []
    seen: set[str] = set()
    for r in records:
        for k in flatten(r):
            if k not in seen:
                seen.add(k)
                cols.append(k)
    return cols


def write_csv(records: Sequence[DesignRecord], path: str) -> int:
    """Write every record with a stable union of columns.  Returns rows written."""
    import csv

    cols = flat_columns(records)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in records:
            w.writerow(flatten(r))
    return len(records)


def write_jsonl(records: Sequence[DesignRecord], path: str) -> int:
    with open(path, "w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r.as_dict(), default=str) + "\n")
    return len(records)
