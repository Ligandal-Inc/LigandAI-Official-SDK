# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""The campaign spec: one declarative file that fixes every decision.

Everything the Avammune/ENPP3 campaign settled ad hoc over twelve hours — which
construct, which cofactors, which paralogs, which peptide classes and lengths,
which gates at which thresholds, what "done" means, and what it may spend — is a
field here.  Two people running the same spec run the same campaign.

The spec VALIDATES itself against the defects that made that campaign expensive.
``CampaignSpec.validate()`` refuses, before a single credit is spent:

  * a stop metric with no numeric target (you cannot stop on a vibe)
  * a certification bar that no achievable run count can reach
  * a paralog named in the stop metrics but with no construct declared
  * a cofactor-free control demanded on a target that declares no cofactors
  * a ranking that does not name the population it orders
  * a learned scorer declared without a clustered split
  * a budget with no cap, or a no-progress rule that can never fire
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, asdict
from typing import Any, Iterable, Mapping, Sequence

from .gates import GateSpec
from .values import runs_needed_for_upper_bound

SPEC_VERSION = 1

_AA_RE = re.compile(r"^[ACDEFGHIKLMNPQRSTVWYXUO]+$")


class SpecError(ValueError):
    """The spec is not runnable as written."""


def _req(d: Mapping[str, Any], key: str, where: str) -> Any:
    if key not in d or d[key] is None:
        raise SpecError(f"{where}: missing required field {key!r}")
    return d[key]


# ---------------------------------------------------------------------------

@dataclass
class Construct:
    """A specific receptor sequence.  The sequence — not the name — is identity."""

    name: str
    sequence: str
    gene: str | None = None
    uniprot: str | None = None
    pdb: str | None = None
    chain: str | None = None
    residue_range: tuple[int, int] | None = None
    numbering_frame: str = ""
    conformation: str | None = None
    role: str = "design"          # 'design' | 'comparison' | 'off_target'

    @classmethod
    def from_dict(cls, d: Mapping[str, Any], base_dir: str = ".") -> "Construct":
        name = _req(d, "name", "construct")
        seq = d.get("sequence")
        if not seq and d.get("sequence_file"):
            p = d["sequence_file"]
            p = p if os.path.isabs(p) else os.path.join(base_dir, p)
            with open(p, encoding="utf-8") as fh:
                seq = "".join(l.strip() for l in fh if not l.startswith(">"))
        if not seq:
            raise SpecError(f"construct {name!r}: needs 'sequence' or 'sequence_file'")
        seq = "".join(seq.split()).upper()
        if not _AA_RE.match(seq):
            bad = sorted(set(seq) - set("ACDEFGHIKLMNPQRSTVWYXUO"))
            raise SpecError(f"construct {name!r}: non-amino-acid characters {bad}")
        rr = d.get("residue_range")
        return cls(
            name=name, sequence=seq, gene=d.get("gene"), uniprot=d.get("uniprot"),
            pdb=d.get("pdb"), chain=d.get("chain"),
            residue_range=(int(rr[0]), int(rr[1])) if rr else None,
            numbering_frame=d.get("numbering_frame", ""),
            conformation=d.get("conformation"), role=d.get("role", "design"),
        )

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["residue_range"] = list(self.residue_range) if self.residue_range else None
        d["sequence_length"] = len(self.sequence)
        return d


@dataclass
class Cofactor:
    name: str                 # HETATM resname, e.g. 'ZN'
    count: int = 1
    role: str = "catalytic"

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Cofactor":
        return cls(str(_req(d, "name", "cofactor")).upper(),
                   int(d.get("count", 1)), d.get("role", "catalytic"))

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Target:
    gene: str
    construct: Construct
    cofactors: tuple[Cofactor, ...] = ()
    pocket_residues: tuple[int, ...] = ()
    pocket_source: str = ""
    comparison_constructs: tuple[Construct, ...] = ()

    @classmethod
    def from_dict(cls, d: Mapping[str, Any], base_dir: str = ".") -> "Target":
        gene = _req(d, "gene", "target")
        con = Construct.from_dict(_req(d, "construct", "target"), base_dir)
        con.gene = con.gene or gene
        comps = tuple(Construct.from_dict({**c, "role": "comparison"}, base_dir)
                      for c in d.get("comparison_constructs", []))
        return cls(gene, con,
                   tuple(Cofactor.from_dict(c) for c in d.get("cofactors", [])),
                   tuple(int(r) for r in d.get("pocket_residues", [])),
                   d.get("pocket_source", ""), comps)

    def as_dict(self) -> dict[str, Any]:
        return {"gene": self.gene, "construct": self.construct.as_dict(),
                "cofactors": [c.as_dict() for c in self.cofactors],
                "pocket_residues": list(self.pocket_residues),
                "pocket_source": self.pocket_source,
                "comparison_constructs": [c.as_dict() for c in self.comparison_constructs]}


@dataclass
class OffTarget:
    gene: str
    construct: Construct
    min_margin: float = 0.20
    cofactors: tuple[Cofactor, ...] = ()

    @classmethod
    def from_dict(cls, d: Mapping[str, Any], base_dir: str = ".") -> "OffTarget":
        gene = _req(d, "gene", "off_target")
        con = Construct.from_dict({**_req(d, "construct", f"off_target {gene}"),
                                   "role": "off_target"}, base_dir)
        con.gene = con.gene or gene
        return cls(gene, con, float(d.get("min_margin", 0.20)),
                   tuple(Cofactor.from_dict(c) for c in d.get("cofactors", [])))

    def as_dict(self) -> dict[str, Any]:
        return {"gene": self.gene, "construct": self.construct.as_dict(),
                "min_margin": self.min_margin,
                "cofactors": [c.as_dict() for c in self.cofactors]}


@dataclass
class PeptideClass:
    name: str
    topology: str = "linear"          # linear | disulfide | cyclic | scaffolded
    length_range: tuple[int, int] = (12, 24)
    n_per_round: int = 200
    molecule_class: str | None = None   # defaults to topology; feeds Scorer.applies_to
    generator_overrides: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "PeptideClass":
        name = _req(d, "name", "peptide class")
        lr = d.get("length_range", [12, 24])
        if len(lr) != 2 or int(lr[0]) > int(lr[1]):
            raise SpecError(f"peptide class {name!r}: length_range must be [min, max]")
        if int(lr[0]) == int(lr[1]):
            raise SpecError(
                f"peptide class {name!r}: length_range [{lr[0]}, {lr[1]}] is a single "
                f"length. Length is sampled PER BATCH on this platform — a collapsed "
                f"range has silently produced 1,300 designs at one length before."
            )
        return cls(name, d.get("topology", "linear"), (int(lr[0]), int(lr[1])),
                   int(d.get("n_per_round", 200)),
                   d.get("molecule_class") or d.get("topology", "linear"),
                   dict(d.get("generator_overrides", {})))

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["length_range"] = list(self.length_range)
        return d


@dataclass
class GenerationSettings:
    model: str = "v6.9.21"
    guidance_scale: float = 1.0
    heads: dict[str, Any] = field(default_factory=dict)
    avoid_residues: tuple[int, ...] = ()
    target_residues: tuple[int, ...] = ()
    composition_floor: dict[str, float] = field(
        default_factory=lambda: {"hydrophobic": 0.25, "aromatic": 0.02, "k_plus_r": 0.02})
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "GenerationSettings":
        heads = dict(d.get("heads", {}))
        # Measured on this platform: stacking these four collapses composition to
        # 13 of 20 amino acids, aromatic fraction 0.001, 0 of 243 passing.
        stacked = [h for h in ("immuno", "cellfree", "proteolytic", "halflife")
                   if heads.get(h)]
        if len(stacked) >= 4:
            raise SpecError(
                f"generation.heads stacks all of {stacked}. Measured result: 13 of 20 "
                f"amino acids used, aromatic fraction 0.001, 0/243 passing the "
                f"composition floor. Enable at most three, and keep immuno <= 0.5."
            )
        if isinstance(heads.get("immuno"), (int, float)) and float(heads["immuno"]) > 0.5:
            raise SpecError("generation.heads.immuno must be <= 0.5 (measured composition collapse above it)")
        return cls(d.get("model", "v6.9.21"), float(d.get("guidance_scale", 1.0)), heads,
                   tuple(int(r) for r in d.get("avoid_residues", [])),
                   tuple(int(r) for r in d.get("target_residues", [])),
                   dict(d.get("composition_floor", {"hydrophobic": 0.25, "aromatic": 0.02, "k_plus_r": 0.02})),
                   dict(d.get("extra", {})))

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["avoid_residues"] = list(self.avoid_residues)
        d["target_residues"] = list(self.target_residues)
        return d


@dataclass
class FoldSettings:
    engine: str = "boltz2"
    diffusion_samples: int = 4
    sampling_steps: int = 50
    recycling_steps: int = 3
    use_potentials: bool = True
    msa_receptor: bool = True
    msa_peptide: bool = False
    seeds_per_unit: int = 1
    # ⛔ These are MEASURED MEDIANS from the live credit ledger, not a price list.
    # Derived over a 365-day window (server/campaign-economics.ts,
    # bd-LIGANDAI_ALPHA_V2-pxmj8): fold 100.00 median over 8,626 rows,
    # generation 100.00 median over 229 rows / 47,027 peptides, scoring 25.00 over
    # 165 rows — 225 credits all-in per design.
    # Use the MEDIAN, never the mean: the mean fold rate is 504.73 because <1% of
    # rows are hand-written bulk reconciliations bundling generation and folding
    # into one line. A campaign quoted at 500/fold when the real charge is 100 is
    # told it can afford a FIFTH of the work it can actually afford.
    # Re-derive with: npx tsx scripts/campaign_runner/verify_credit_rates.ts
    credits_per_fold: int = 100
    credits_per_generated_peptide: int = 100
    credits_per_scored_design: int = 25

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "FoldSettings":
        ds = int(d.get("diffusion_samples", 4))
        if ds < 1:
            raise SpecError("fold.diffusion_samples must be >= 1")
        msa = d.get("msa", {})
        if isinstance(msa, bool):
            msa = {"receptor": msa, "peptide": False}
        if not msa.get("receptor", True):
            raise SpecError(
                "fold.msa.receptor is false. Folding a receptor without an MSA "
                "produced 0 of 75 elite designs on this platform. MSA on the "
                "receptor is not optional."
            )
        return cls(d.get("engine", "boltz2"), ds, int(d.get("sampling_steps", 50)),
                   int(d.get("recycling_steps", 3)), bool(d.get("use_potentials", True)),
                   bool(msa.get("receptor", True)), bool(msa.get("peptide", False)),
                   int(d.get("seeds_per_unit", 1)),
                   int(d.get("credits_per_fold", 100)),
                   int(d.get("credits_per_generated_peptide", 100)),
                   int(d.get("credits_per_scored_design", 25)))

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReliabilitySettings:
    """How reproducibility is measured and what counts as certified."""

    failure_metric: str = "peptide_ipsae"
    failure_threshold: float = 0.67
    certification_bar: float = 0.15
    confidence: float = 0.95
    # ⛔ Independent SEED UNITS, never diffusion samples inside one seed.  Cutting
    # on within-seed samples produced an apparent 44% unreliability that was
    # winner's curse (cyclic 4.7% vs linear 5.2%, p=0.88, within one arm).
    count_unit: str = "independent_seed_unit"
    min_runs_before_verdict: int = 8

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "ReliabilitySettings":
        cu = d.get("count_unit", "independent_seed_unit")
        if cu not in ("independent_seed_unit",):
            raise SpecError(
                f"reliability.count_unit={cu!r} is not allowed. Reliability must be "
                f"counted in INDEPENDENT SEED UNITS. Counting diffusion samples "
                f"within one seed as independent runs is the winner's-curse defect."
            )
        return cls(d.get("failure_metric", "peptide_ipsae"),
                   float(d.get("failure_threshold", 0.67)),
                   float(d.get("certification_bar", 0.15)),
                   float(d.get("confidence", 0.95)), cu,
                   int(d.get("min_runs_before_verdict", 8)))

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StopMetric:
    """A numeric, checkable definition of done.

    ``kind`` is one of:
      certified_designs   -- N designs certified at the reliability bar
      margin_coverage     -- N designs with a measured margin >= min_margin vs EACH off-target
      control_present     -- N designs carrying a named paired control
      block_coverage      -- a fraction of a population carrying a whole record block
      interval_width      -- N designs whose reliability interval is narrower than W
    """

    id: str
    kind: str
    target: float
    of_population: str = "gate_passing"
    off_targets: tuple[str, ...] = ()
    min_margin: float | None = None
    control: str | None = None
    block: str | None = None
    max_interval_width: float | None = None
    description: str = ""

    KINDS = ("certified_designs", "margin_coverage", "control_present",
             "block_coverage", "interval_width")

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "StopMetric":
        sid = _req(d, "id", "stop_metric")
        kind = _req(d, "kind", f"stop_metric {sid}")
        if kind not in cls.KINDS:
            raise SpecError(f"stop_metric {sid}: kind {kind!r} not in {cls.KINDS}")
        if "target" not in d:
            raise SpecError(
                f"stop_metric {sid}: needs a numeric 'target'. A campaign cannot "
                f"stop on a condition with no number in it."
            )
        return cls(sid, kind, float(d["target"]), d.get("of_population", "gate_passing"),
                   tuple(d.get("off_targets", [])),
                   float(d["min_margin"]) if d.get("min_margin") is not None else None,
                   d.get("control"), d.get("block"),
                   float(d["max_interval_width"]) if d.get("max_interval_width") is not None else None,
                   d.get("description", ""))

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["off_targets"] = list(self.off_targets)
        return d


@dataclass
class NoProgressRule:
    patience_iterations: int = 3
    min_absolute_progress: float = 1.0

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "NoProgressRule":
        p = int(d.get("patience_iterations", 3))
        m = float(d.get("min_absolute_progress", 1.0))
        if p < 1:
            raise SpecError("budget.no_progress.patience_iterations must be >= 1")
        if m <= 0:
            raise SpecError(
                "budget.no_progress.min_absolute_progress must be > 0, otherwise the "
                "no-progress guard can never fire and the loop can spin forever."
            )
        return cls(p, m)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Budget:
    max_credits: int
    max_iterations: int = 20
    max_wall_clock_hours: float = 72.0
    per_iteration_credit_cap: int | None = None
    no_progress: NoProgressRule = field(default_factory=NoProgressRule)
    reserve_fraction: float = 0.05      # never spend the last 5%

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Budget":
        if "max_credits" not in d:
            raise SpecError(
                "budget.max_credits is required. An auto-population loop without a "
                "hard credit cap can spend a whole allocation in one night."
            )
        mc = int(d["max_credits"])
        if mc <= 0:
            raise SpecError("budget.max_credits must be positive")
        mi = int(d.get("max_iterations", 20))
        if mi <= 0:
            raise SpecError("budget.max_iterations must be positive")
        return cls(mc, mi, float(d.get("max_wall_clock_hours", 72.0)),
                   int(d["per_iteration_credit_cap"]) if d.get("per_iteration_credit_cap") else None,
                   NoProgressRule.from_dict(d.get("no_progress", {})),
                   float(d.get("reserve_fraction", 0.05)))

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["no_progress"] = self.no_progress.as_dict()
        return d


@dataclass
class RankingSettings:
    score: str = "deltaforge_predicted_kd_nm"
    higher_is_better: bool = False
    population: str = "gate_passing"
    length_baseline: str = "required"
    length_confound_threshold: float = 0.20
    require_full_coverage: bool = True

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "RankingSettings":
        pop = d.get("population")
        if not pop:
            raise SpecError(
                "ranking.population must name the population the ordering exists to "
                "order. Leaving it implicit is how a ranker came to run on 679 "
                "gate-REJECTED designs while skipping 415 that passed."
            )
        if d.get("length_baseline", "required") != "required":
            raise SpecError(
                "ranking.length_baseline must be 'required'. Interface score "
                "correlates with peptide length at rho -0.37 on this platform; "
                "every ranking ships its length baseline."
            )
        return cls(d.get("score", "deltaforge_predicted_kd_nm"),
                   bool(d.get("higher_is_better", False)), pop,
                   "required", float(d.get("length_confound_threshold", 0.20)),
                   bool(d.get("require_full_coverage", True)))

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------

@dataclass
class CampaignSpec:
    name: str
    target: Target
    peptide_classes: tuple[PeptideClass, ...]
    gates: tuple[GateSpec, ...]
    stop_metrics: tuple[StopMetric, ...]
    budget: Budget
    off_targets: tuple[OffTarget, ...] = ()
    generation: GenerationSettings = field(default_factory=GenerationSettings)
    fold: FoldSettings = field(default_factory=FoldSettings)
    reliability: ReliabilitySettings = field(default_factory=ReliabilitySettings)
    ranking: RankingSettings = field(default_factory=RankingSettings)
    controls: tuple[str, ...] = ("scramble", "length_matched_random")
    output_dir: str = "./campaign_out"
    customer: dict[str, Any] = field(default_factory=dict)
    spec_version: int = SPEC_VERSION
    source_path: str | None = None
    notes: str = ""

    # ---- loading ------------------------------------------------------
    @classmethod
    def from_dict(cls, d: Mapping[str, Any], base_dir: str = ".",
                  source_path: str | None = None) -> "CampaignSpec":
        v = int(d.get("campaign_spec_version", SPEC_VERSION))
        if v != SPEC_VERSION:
            raise SpecError(f"campaign_spec_version {v} != supported {SPEC_VERSION}")
        gates_in = d.get("gates", [])
        if not gates_in:
            raise SpecError("spec needs at least one gate")
        gates = tuple(GateSpec(
            metric=_req(g, "metric", "gate"), op=_req(g, "op", "gate"),
            threshold=float(_req(g, "threshold", "gate")), label=g.get("label"),
            min_effect_fraction=float(g.get("min_effect_fraction", 0.01)),
            allow_vacuous=bool(g.get("allow_vacuous", False)),
            allow_vacuous_reason=g.get("allow_vacuous_reason"),
            on_missing=g.get("on_missing", "reject"),
        ) for g in gates_in)
        sms = tuple(StopMetric.from_dict(s) for s in d.get("stop_metrics", []))
        if not sms:
            raise SpecError(
                "spec needs at least one stop_metric. Without one the loop has no "
                "definition of done and will run until the budget is gone."
            )
        pcs = tuple(PeptideClass.from_dict(p) for p in _req(d, "peptide_classes", "spec"))
        spec = cls(
            name=_req(d, "name", "spec"),
            target=Target.from_dict(_req(d, "target", "spec"), base_dir),
            peptide_classes=pcs, gates=gates, stop_metrics=sms,
            budget=Budget.from_dict(_req(d, "budget", "spec")),
            off_targets=tuple(OffTarget.from_dict(o, base_dir) for o in d.get("off_targets", [])),
            generation=GenerationSettings.from_dict(d.get("generation", {})),
            fold=FoldSettings.from_dict(d.get("fold", {})),
            reliability=ReliabilitySettings.from_dict(d.get("reliability", {})),
            ranking=RankingSettings.from_dict(d.get("ranking", {"population": "gate_passing"})),
            controls=tuple(d.get("controls", ["scramble", "length_matched_random"])),
            output_dir=d.get("output_dir", "./campaign_out"),
            customer=dict(d.get("customer", {})), spec_version=v,
            source_path=source_path, notes=d.get("notes", ""),
        )
        spec.validate()
        return spec

    @classmethod
    def load(cls, path: str) -> "CampaignSpec":
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        if path.endswith((".yaml", ".yml")):
            try:
                import yaml  # type: ignore
            except ImportError as exc:  # pragma: no cover
                raise SpecError("PyYAML is required for YAML specs: pip install pyyaml") from exc
            data = yaml.safe_load(text)
        else:
            data = json.loads(text)
        if not isinstance(data, dict):
            raise SpecError(f"{path}: spec must be a mapping at the top level")
        return cls.from_dict(data, os.path.dirname(os.path.abspath(path)),
                             os.path.abspath(path))

    # ---- validation ---------------------------------------------------
    def declared_receptors(self) -> dict[str, str]:
        """name -> sequence, for content-based pose identity (D7)."""
        out = {self.target.construct.name: self.target.construct.sequence}
        for c in self.target.comparison_constructs:
            out[c.name] = c.sequence
        for o in self.off_targets:
            out[o.construct.name] = o.construct.sequence
        return out

    def validate(self) -> list[str]:
        """Raise on anything unrunnable; return non-fatal warnings."""
        warn: list[str] = []
        rel = self.reliability

        if not 0.0 < rel.certification_bar < 1.0:
            raise SpecError("reliability.certification_bar must be in (0, 1)")
        clean = runs_needed_for_upper_bound(rel.certification_bar, 0.0, rel.confidence)
        if clean is None:
            raise SpecError(
                f"reliability.certification_bar={rel.certification_bar} cannot be reached "
                f"even with zero failures at any run count."
            )
        if clean > 500:
            warn.append(
                f"certification_bar {rel.certification_bar} needs >= {clean} clean runs per "
                f"design; at {self.fold.credits_per_fold} credits/fold that is "
                f"{clean * self.fold.credits_per_fold:,} credits for ONE certified design."
            )

        # receptor sequences must be distinct — a paralog that is byte-identical to
        # the target has no margin to measure
        seqs: dict[str, str] = {}
        for name, s in self.declared_receptors().items():
            if s in seqs.values():
                dup = [k for k, v in seqs.items() if v == s][0]
                raise SpecError(
                    f"constructs {name!r} and {dup!r} have IDENTICAL sequences. "
                    f"Identity here is the sequence, so these are one receptor and "
                    f"any margin between them is zero by construction."
                )
            seqs[name] = s

        off_names = {o.gene for o in self.off_targets}
        cof_names = {c.name for c in self.target.cofactors}

        for sm in self.stop_metrics:
            if sm.target <= 0:
                raise SpecError(f"stop_metric {sm.id}: target must be > 0")
            if sm.kind == "margin_coverage":
                if not sm.off_targets:
                    raise SpecError(f"stop_metric {sm.id}: margin_coverage needs off_targets")
                missing = set(sm.off_targets) - off_names
                if missing:
                    raise SpecError(
                        f"stop_metric {sm.id}: names off-target(s) {sorted(missing)} that "
                        f"the spec does not declare a construct for. A margin cannot be "
                        f"measured against a receptor with no sequence."
                    )
                if sm.min_margin is None:
                    raise SpecError(f"stop_metric {sm.id}: margin_coverage needs min_margin")
            if sm.kind == "control_present":
                if not sm.control:
                    raise SpecError(f"stop_metric {sm.id}: control_present needs 'control'")
                if sm.control == "cofactor_free" and not cof_names:
                    raise SpecError(
                        f"stop_metric {sm.id}: demands a cofactor-free control but the "
                        f"target declares no cofactors. Declare target.cofactors, or "
                        f"drop this stop metric."
                    )
            if sm.kind == "block_coverage":
                if not sm.block:
                    raise SpecError(f"stop_metric {sm.id}: block_coverage needs 'block'")
                from .record import DesignRecord
                if sm.block not in DesignRecord.BLOCKS:
                    raise SpecError(f"stop_metric {sm.id}: block {sm.block!r} is not one of "
                                    f"{DesignRecord.BLOCKS}")
                if not 0 < sm.target <= 1.0:
                    raise SpecError(f"stop_metric {sm.id}: block_coverage target is a "
                                    f"FRACTION in (0, 1]")
            if sm.kind == "interval_width" and sm.max_interval_width is None:
                raise SpecError(f"stop_metric {sm.id}: interval_width needs max_interval_width")

        # can the budget buy even one certified design?
        gen1 = self.fold.credits_per_generated_peptide
        per_design = clean * self.fold.credits_per_fold + self.fold.credits_per_scored_design
        floor = gen1 + per_design
        if floor > self.budget.max_credits:
            raise SpecError(
                f"budget.max_credits={self.budget.max_credits:,} cannot buy a single "
                f"certified design: generating one peptide ({gen1:,}) plus {clean} "
                f"clean runs and one scoring pass ({per_design:,}) = {floor:,} credits."
            )
        want = next((sm.target for sm in self.stop_metrics
                     if sm.kind == "certified_designs"), None)
        if want:
            optimistic = int(want) * (gen1 + per_design)
            if optimistic > self.budget.max_credits:
                warn.append(
                    f"stop metric asks for {int(want)} certified designs; the OPTIMISTIC "
                    f"cost (every design certifying with zero failures) is "
                    f"{optimistic:,} credits against a cap of {self.budget.max_credits:,}. "
                    f"The loop will stop on budget before it stops on the metric. "
                    f"(Rates are ledger medians: {self.fold.credits_per_fold}/fold, "
                    f"{gen1}/peptide generated, "
                    f"{self.fold.credits_per_scored_design}/design scored.)"
                )
        if self.budget.per_iteration_credit_cap and \
                self.budget.per_iteration_credit_cap > self.budget.max_credits:
            raise SpecError("budget.per_iteration_credit_cap exceeds budget.max_credits")

        gate_metrics = {g.metric for g in self.gates}
        if rel.failure_metric not in {"peptide_ipsae"} | gate_metrics:
            warn.append(
                f"reliability.failure_metric={rel.failure_metric!r} is not among the "
                f"gate metrics {sorted(gate_metrics)}; a design can then be 'certified' "
                f"against a threshold no gate uses."
            )
        if self.fold.diffusion_samples < 4:
            warn.append(
                f"fold.diffusion_samples={self.fold.diffusion_samples}. On this platform "
                f"53 of 80 peptides straddled the 0.67 gate WITHIN their own four "
                f"samples; ranking on fewer is ranking on noise."
            )
        if not self.controls:
            warn.append("no controls declared. A scramble and a length-matched random "
                        "arm are how you learn whether the metric is informative on "
                        "this receptor at all.")
        return warn

    def as_dict(self) -> dict[str, Any]:
        return {
            "campaign_spec_version": self.spec_version,
            "name": self.name,
            "customer": dict(self.customer),
            "target": self.target.as_dict(),
            "off_targets": [o.as_dict() for o in self.off_targets],
            "peptide_classes": [p.as_dict() for p in self.peptide_classes],
            "generation": self.generation.as_dict(),
            "fold": self.fold.as_dict(),
            "gates": [g.as_dict() for g in self.gates],
            "reliability": self.reliability.as_dict(),
            "stop_metrics": [s.as_dict() for s in self.stop_metrics],
            "budget": self.budget.as_dict(),
            "ranking": self.ranking.as_dict(),
            "controls": list(self.controls),
            "output_dir": self.output_dir,
            "source_path": self.source_path,
            "notes": self.notes,
        }

    def fingerprint(self) -> str:
        import hashlib
        payload = json.dumps(self.as_dict(), sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()
