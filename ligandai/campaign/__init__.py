# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Reproducible, auto-populating peptide binder campaigns.

A campaign is one declarative spec file plus a loop that runs until the spec's
stop metrics are met, the budget is spent, or progress stalls.  Every score the
campaign produces is emitted, with an explicit null and a coverage denominator
wherever a value is unmeasured.

    from ligandai.campaign import CampaignSpec, CampaignEngine, SDKPlatform

    spec = CampaignSpec.load("my_campaign.yaml")
    engine = CampaignEngine(spec, SDKPlatform())
    result = engine.run()

Or from a shell::

    ligandai campaign run my_campaign.yaml

The eight guards this package enforces, each from a real defect:

  1. a gate is refused if it removes nothing (or almost nothing) — every gate
     reports the metric's observed range
  2. a ranking can only be computed on a typed Population, and must cover it
  3. a scorer term that is constant across the cohort cannot enter a composite;
     an inapplicable term contributes NOT_APPLICABLE, never a constant
  4. a scorer must declare its direction and carry evidence for it
  5. a learned scorer must report a clustered/grouped/temporal split
  6. every ranking ships a length baseline; there is no code path without one
  7. pose identity comes from hashing the receptor chain and parsing the peptide
     chain, never from an arm or file name
  8. every aggregate returns a Verdict carrying its row count; zero rows is
     INDETERMINATE, never PASS
"""

from .values import (Measured, Status, Verdict, Outcome, WilsonInterval, wilson,
                     runs_needed_for_upper_bound, all_of, any_of, count_at_least,
                     fraction_at_least)
from .identity import (PoseIdentity, ChainRecord, identify_pose, parse_chains,
                       sha256_seq, assert_single_receptor, group_by_receptor,
                       CrossReceptorPoolingError)
from .gates import (GateSpec, GateReport, GateStatus, GatePanelResult, Population,
                    apply_gate, apply_gates, assert_population, VacuousGateError,
                    WrongPopulationError)
from .scoring import (Scorer, Direction, DirectionEvidence, SplitEvidence, SplitType,
                      Correlation, spearman, composite_score, rank, RankingResult,
                      LengthBaseline, DirectionInvertedError, ConstantTermError,
                      UnvalidatedScorerError, OptimisticSplitError)
from .record import (DesignRecord, InterfaceBlock, ReliabilityBlock, SelectivityBlock,
                     CofactorControlBlock, ImmunogenicityBlock, StabilityBlock,
                     AffinityBlock, LinkerSiteBlock, ConstructComparisonBlock,
                     flatten, write_csv, write_jsonl)
from .spec import (CampaignSpec, Target, OffTarget, Construct, Cofactor, PeptideClass,
                   GenerationSettings, FoldSettings, ReliabilitySettings, StopMetric,
                   Budget, RankingSettings, SpecError, SPEC_VERSION)
from .stopmetrics import StopMetricResult, StopReport, evaluate_all
from .planner import (Action, ActionKind, BudgetLedger, IterationPlan, StopReason,
                      plan_iteration)
from .platform import (Platform, SDKPlatform, ReplayPlatform, DryRunPlatform,
                       PoseResult, GeneratedDesign)
from .aggregate import build_records, AggregationReport
from .engine import CampaignEngine, CampaignResult, IterationRecord

__all__ = [
    "CampaignSpec", "CampaignEngine", "CampaignResult", "SDKPlatform",
    "ReplayPlatform", "DryRunPlatform", "Platform", "PoseResult", "GeneratedDesign",
    "DesignRecord", "Measured", "Status", "Verdict", "Outcome", "WilsonInterval",
    "wilson", "runs_needed_for_upper_bound", "all_of", "any_of", "count_at_least",
    "fraction_at_least", "PoseIdentity", "ChainRecord", "identify_pose",
    "parse_chains", "sha256_seq", "assert_single_receptor", "group_by_receptor",
    "CrossReceptorPoolingError", "GateSpec", "GateReport", "GateStatus",
    "GatePanelResult", "Population", "apply_gate", "apply_gates", "assert_population",
    "VacuousGateError", "WrongPopulationError", "Scorer", "Direction",
    "DirectionEvidence", "SplitEvidence", "SplitType", "Correlation", "spearman",
    "composite_score", "rank", "RankingResult", "LengthBaseline",
    "DirectionInvertedError", "ConstantTermError", "UnvalidatedScorerError",
    "OptimisticSplitError", "InterfaceBlock", "ReliabilityBlock", "SelectivityBlock",
    "CofactorControlBlock", "ImmunogenicityBlock", "StabilityBlock", "AffinityBlock",
    "LinkerSiteBlock", "ConstructComparisonBlock", "flatten", "write_csv",
    "write_jsonl", "Target", "OffTarget", "Construct", "Cofactor", "PeptideClass",
    "GenerationSettings", "FoldSettings", "ReliabilitySettings", "StopMetric",
    "Budget", "RankingSettings", "SpecError", "SPEC_VERSION", "StopMetricResult",
    "StopReport", "evaluate_all", "Action", "ActionKind", "BudgetLedger",
    "IterationPlan", "StopReason", "plan_iteration", "build_records",
    "AggregationReport", "IterationRecord",
]
