# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Public pydantic models for SDK request/response payloads.

Models mirror the server-side schemas in ``shared/schema.ts``. They are
intentionally permissive (``ConfigDict(extra="allow")``) so additive server
changes don't break SDK callers — but every documented field is typed.

For internal-only types (HTTP machinery, rate limiter state) see
:mod:`ligandai._internal`.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# -- Base ---------------------------------------------------------------------


class _LGModel(BaseModel):
    """Base for SDK pydantic models.

    Configured to allow additive server changes (extra fields preserved as raw)
    while keeping documented fields typed.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
        str_strip_whitespace=True,
        arbitrary_types_allowed=True,
    )


# -- Auth / account -----------------------------------------------------------


class User(_LGModel):
    id: str
    email: str | None = None
    first_name: str | None = Field(default=None, alias="firstName")
    last_name: str | None = Field(default=None, alias="lastName")
    organization_name: str | None = Field(default=None, alias="organizationName")
    subscription_tier: str | None = Field(default=None, alias="subscriptionTier")
    is_super_admin: int | bool | None = Field(default=None, alias="isSuperAdmin")
    is_developer: int | bool | None = Field(default=None, alias="isDeveloper")
    approval_status: str | None = Field(default=None, alias="approvalStatus")


class Credits(_LGModel):
    balance: int
    monthly_allocation: int | None = Field(default=None, alias="monthlyAllocation")
    next_refill_at: datetime | None = Field(default=None, alias="nextRefillAt")


class CreditTransaction(_LGModel):
    id: int | str
    amount: int
    operation: str | None = None
    type: str | None = None  # 'topup' | 'auto_topup' | 'usage_gpu' | 'refund' | ...
    description: str | None = None
    balance_after: int | None = Field(default=None, alias="balanceAfter")
    occurred_at: datetime | None = Field(default=None, alias="occurredAt")
    created_at: datetime | None = Field(default=None, alias="createdAt")


class AccountBalance(_LGModel):
    """Current account balance and burn-rate summary."""

    credits: int = Field(alias="balance")
    burn_rate_30d: int | None = Field(default=None, alias="burnRate30d")
    days_remaining: float | None = Field(default=None, alias="daysRemaining")
    tier: str | None = None
    auto_topup_enabled: bool | None = Field(default=None, alias="autoTopupEnabled")


class TopUpResult(_LGModel):
    """Result of a credit top-up attempt."""

    success: bool
    credits_added: int | None = Field(default=None, alias="creditsAdded")
    new_balance: int | None = Field(default=None, alias="newBalance")
    payment_intent_id: str | None = Field(default=None, alias="paymentIntentId")
    checkout_url: str | None = Field(default=None, alias="checkoutUrl")


class AutoTopupConfig(_LGModel):
    """Auto top-up configuration for an account."""

    enabled: bool
    threshold_credits: int | None = Field(default=None, alias="thresholdCredits")
    amount_usd: int | None = Field(default=None, alias="amountUsd")
    last_charged_at: datetime | None = Field(default=None, alias="lastChargedAt")
    failure_count: int | None = Field(default=None, alias="failureCount")


class CostEstimate(_LGModel):
    """Credit cost estimate for a generation + folding job."""

    credits: int
    cost_usd: float = Field(alias="costUsd")
    breakdown: dict[str, int] | None = None  # {'generation': X, 'folding': Y, 'scoring': Z}


class TierLimits(_LGModel):
    tier: str
    max_peptides_per_generation: int | None = Field(default=None, alias="maxPeptidesPerGeneration")
    max_folds_per_target: int | None = Field(default=None, alias="maxFoldsPerTarget")
    max_concurrent_targets: int | None = Field(default=None, alias="maxConcurrentTargets")
    max_concurrent_gpu_slots: int | None = Field(default=None, alias="maxConcurrentGpuSlots")
    rate_limit_per_minute: int | None = Field(default=None, alias="rateLimitPerMinute")


class MSAChain(_LGModel):
    csv: str
    hits: int = 0


class MSAResult(_LGModel):
    chains: dict[str, MSAChain]
    cached: bool = False
    elapsed_ms: int | None = None
    gene: str | None = None
    cache_key: str | None = Field(default=None, alias="cache_key")


class UsageSummary(_LGModel):
    today_input_tokens: int | None = Field(default=None, alias="todayInputTokens")
    today_output_tokens: int | None = Field(default=None, alias="todayOutputTokens")
    daily_token_limit: int | None = Field(default=None, alias="dailyTokenLimit")
    # Credit consumption (today)
    credits_used_today: int | None = Field(default=None, alias="creditsUsedToday")
    credits_used_generation: int | None = Field(default=None, alias="creditsUsedGeneration")
    credits_used_folding: int | None = Field(default=None, alias="creditsUsedFolding")
    credits_used_ligandiq: int | None = Field(default=None, alias="creditsUsedLigandIQ")
    credits_used_ai: int | None = Field(default=None, alias="creditsUsedAI")
    credits_by_type: dict[str, int] | None = Field(default=None, alias="creditsByType")


class ApiCallLogEntry(_LGModel):
    """One SDK/API call audit row."""

    id: int | str | None = None
    method: str
    endpoint: str
    status_code: int | None = Field(default=None, alias="statusCode")
    latency_ms: int | None = Field(default=None, alias="latencyMs")
    sdk_version: str | None = Field(default=None, alias="sdkVersion")
    sdk_language: str | None = Field(default=None, alias="sdkLanguage")
    api_key_id: str | None = Field(default=None, alias="apiKeyId")
    client_session_id: str | None = Field(default=None, alias="clientSessionId")
    created_at: datetime | None = Field(default=None, alias="createdAt")


class ClientSessionUsageSummary(_LGModel):
    """Credit and request roll-up for a caller-provided SDK session ID."""

    total_calls: int = Field(default=0, alias="totalCalls")
    successful_calls: int = Field(default=0, alias="successfulCalls")
    error_calls: int = Field(default=0, alias="errorCalls")
    avg_latency_ms: int | None = Field(default=None, alias="avgLatencyMs")
    first_call_at: datetime | None = Field(default=None, alias="firstCallAt")
    last_call_at: datetime | None = Field(default=None, alias="lastCallAt")
    credits_used: int = Field(default=0, alias="creditsUsed")
    credit_events: int = Field(default=0, alias="creditEvents")


class ClientSessionUsage(_LGModel):
    """Server-side usage and credit accounting for one SDK session ID."""

    client_session_id: str = Field(alias="clientSessionId")
    calls: list[ApiCallLogEntry] = Field(default_factory=list)
    summary: ClientSessionUsageSummary = Field(default_factory=ClientSessionUsageSummary)
    period_days: int | None = Field(default=None, alias="periodDays")


class GoalPlanStep(_LGModel):
    """One planned step in a persistent goal-directed run."""

    step: int | None = None
    intent: str | None = None
    tool: str | None = None
    input: dict[str, Any] | None = None
    rationale: str | None = None
    optional: bool | None = None


class GoalStepRecord(_LGModel):
    """Execution record for one goal-run step."""

    step_idx: int | None = Field(default=None, alias="stepIdx")
    tool: str | None = None
    input: Any = None
    output: Any = None
    error: str | None = None
    started_at: datetime | None = Field(default=None, alias="startedAt")
    completed_at: datetime | None = Field(default=None, alias="completedAt")
    tokens_in: int | None = Field(default=None, alias="tokensIn")
    tokens_out: int | None = Field(default=None, alias="tokensOut")


class GoalAcceptanceCriterion(_LGModel):
    """Auditable criterion used to decide whether a goal run is satisfied."""

    id: str
    label: str
    metric: str | None = None
    operator: str | None = None
    target: str | int | float | bool | None = None
    required: bool | None = None


class GoalEvaluation(_LGModel):
    """Evaluator checkpoint for a persistent goal run."""

    evaluation_idx: int | None = Field(default=None, alias="evaluationIdx")
    evaluated_at: datetime | None = Field(default=None, alias="evaluatedAt")
    status: Literal["satisfied", "partial", "unsatisfied", "blocked"] | str
    rationale: str | None = None
    satisfied_criteria: list[str] = Field(default_factory=list, alias="satisfiedCriteria")
    unsatisfied_criteria: list[str] = Field(default_factory=list, alias="unsatisfiedCriteria")
    evidence: dict[str, Any] | None = None
    next_action: str | None = Field(default=None, alias="nextAction")


class GoalTaskDependency(_LGModel):
    """Directed dependency edge in the derived goal task graph."""

    from_item: str = Field(alias="from")
    to: str
    reason: str | None = None


class GoalChecklistItem(_LGModel):
    """Criterion or planned step in the derived project-management checklist."""

    id: str
    type: Literal["criterion", "step"] | str
    label: str
    status: str
    required: bool | None = None
    metric: str | None = None
    operator: str | None = None
    target: str | int | float | bool | None = None
    step_idx: int | None = Field(default=None, alias="stepIdx")
    tool: str | None = None
    optional: bool | None = None
    depends_on: list[str] = Field(default_factory=list, alias="dependsOn")
    evidence: Any = None
    blockers: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list, alias="nextActions")
    started_at: datetime | None = Field(default=None, alias="startedAt")
    completed_at: datetime | None = Field(default=None, alias="completedAt")


class GoalProgress(_LGModel):
    """Roll-up progress for a goal-directed run."""

    total_items: int = Field(alias="totalItems")
    completed_items: int = Field(alias="completedItems")
    total_criteria: int = Field(alias="totalCriteria")
    satisfied_criteria: int = Field(alias="satisfiedCriteria")
    plan_steps: int = Field(alias="planSteps")
    current_step_idx: int = Field(alias="currentStepIdx")
    percent: int


class GoalBudgetState(_LGModel):
    """Budget cap and current credit burn for the run."""

    cap_credits: int | None = Field(default=None, alias="capCredits")
    consumed_credits: int = Field(alias="consumedCredits")
    remaining_credits: int | None = Field(default=None, alias="remainingCredits")


class GoalCompletionAudit(_LGModel):
    """Completion rationale for terminal goal runs."""

    status: str
    reason: str | None = None
    evaluated_at: datetime | None = Field(default=None, alias="evaluatedAt")
    rationale: str | None = None
    satisfied_criteria: list[str] = Field(default_factory=list, alias="satisfiedCriteria")
    unsatisfied_criteria: list[str] = Field(default_factory=list, alias="unsatisfiedCriteria")


class GoalProjectState(_LGModel):
    """Derived beads-style task graph for a persistent goal run."""

    objective: str
    status: str
    satisfaction_status: str = Field(alias="satisfactionStatus")
    checklist: list[GoalChecklistItem] = Field(default_factory=list)
    dependencies: list[GoalTaskDependency] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)
    blockers: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list, alias="nextActions")
    progress: GoalProgress
    budget: GoalBudgetState
    completion_audit: GoalCompletionAudit | None = Field(default=None, alias="completionAudit")
    updated_at: datetime | None = Field(default=None, alias="updatedAt")


class GoalRun(_LGModel):
    """Persistent AutoResearch/goal run state."""

    run_id: str = Field(alias="runId")
    user_id: str | None = Field(default=None, alias="userId")
    program_db_id: int | None = Field(default=None, alias="programDbId")
    project_db_id: int | None = Field(default=None, alias="projectDbId")
    conversation_id: str | None = Field(default=None, alias="conversationId")
    goal: str
    status: str
    plan: list[GoalPlanStep] | None = None
    current_step_idx: int | None = Field(default=None, alias="currentStepIdx")
    acceptance_criteria: list[GoalAcceptanceCriterion] = Field(default_factory=list, alias="acceptanceCriteria")
    evaluation_history: list[GoalEvaluation] = Field(default_factory=list, alias="evaluationHistory")
    satisfaction_status: str | None = Field(default=None, alias="satisfactionStatus")
    iteration_count: int | None = Field(default=None, alias="iterationCount")
    max_iterations: int | None = Field(default=None, alias="maxIterations")
    step_history: list[GoalStepRecord] = Field(default_factory=list, alias="stepHistory")
    tokens_used: int | None = Field(default=None, alias="tokensUsed")
    credits_consumed: int | None = Field(default=None, alias="creditsConsumed")
    budget_cap_credits: int | None = Field(default=None, alias="budgetCapCredits")
    automatic_mode_acknowledged: bool | None = Field(default=None, alias="automaticModeAcknowledged")
    automatic_mode_acknowledged_at: datetime | None = Field(default=None, alias="automaticModeAcknowledgedAt")
    goal_state: GoalProjectState | None = Field(default=None, alias="goalState")
    error_message: str | None = Field(default=None, alias="errorMessage")
    created_at: datetime | None = Field(default=None, alias="createdAt")
    started_at: datetime | None = Field(default=None, alias="startedAt")
    completed_at: datetime | None = Field(default=None, alias="completedAt")
    updated_at: datetime | None = Field(default=None, alias="updatedAt")


class GoalRunStart(_LGModel):
    """Response returned when a persistent goal run is started."""

    run_id: str = Field(alias="runId")


class GoalRunEvent(_LGModel):
    """One SSE event from a persistent goal run stream."""

    type: str
    run_id: str | None = Field(default=None, alias="runId")
    run: GoalRun | None = None
    goal_state: GoalProjectState | None = Field(default=None, alias="goalState")
    evaluation: GoalEvaluation | None = None
    step: GoalPlanStep | None = None
    step_idx: int | None = Field(default=None, alias="stepIdx")
    plan: list[GoalPlanStep] | None = None
    acceptance_criteria: list[GoalAcceptanceCriterion] | None = Field(default=None, alias="acceptanceCriteria")
    error_message: str | None = Field(default=None, alias="errorMessage")
    reason: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


# -- Receptors / structures ---------------------------------------------------


class ReceptorComplex(_LGModel):
    id: str | int
    complex_id: str | None = Field(default=None, alias="complexId")
    complex_name: str | None = Field(default=None, alias="complexName")
    gene: str | None = None
    genes: list[str] | None = None
    oligomeric_state: str | None = Field(default=None, alias="oligomericState")
    organism: str | None = None
    pdb_code: str | None = Field(default=None, alias="pdbCode")
    chain_count: int | None = Field(default=None, alias="chainCount")
    has_pdb: bool | None = Field(default=None, alias="hasPdb")


class ReceptorListResponse(_LGModel):
    complexes: list[ReceptorComplex]
    total: int
    offset: int
    limit: int

    @property
    def has_more(self) -> bool:
        return (self.offset + len(self.complexes)) < self.total


class ChainClassification(_LGModel):
    gene: str
    receptor_chains: list[str] | None = Field(default=None, alias="receptorChains")
    ligand_chains: list[str] | None = Field(default=None, alias="ligandChains")
    classification: str | None = None
    tier: str | None = None  # 6-tier hierarchy ranking


class FoldRequest(_LGModel):
    request_id: int | str = Field(alias="requestId")
    gene: str
    status: str
    queued_at: datetime | None = Field(default=None, alias="queuedAt")


class FoldQueueStatus(_LGModel):
    request_id: int | str = Field(alias="requestId")
    status: str
    progress: float | None = None
    eta_seconds: float | None = Field(default=None, alias="etaSeconds")
    message: str | None = None


class Structure(_LGModel):
    gene: str
    source: Literal["pdb", "alphafold", "user", "boltz2", "gpcrdb"] | str
    pdb_code: str | None = Field(default=None, alias="pdbCode")
    uniprot_id: str | None = Field(default=None, alias="uniprotId")
    pdb_url: str | None = Field(default=None, alias="pdbUrl")
    pdb_data: str | None = Field(default=None, alias="pdbData")
    chain_count: int | None = Field(default=None, alias="chainCount")
    resolution: float | None = None


class StructureCandidate(_LGModel):
    pdb_code: str | None = Field(default=None, alias="pdbCode")
    source: str
    rank: int | None = None
    score: float | None = None
    notes: str | None = None


class ResidueRange(_LGModel):
    chain: str = "A"
    start: int
    end: int
    label: str | None = None

    @property
    def range(self) -> str:
        return f"{self.chain}:{self.start}-{self.end}"

    @classmethod
    def from_residues(
        cls,
        residues: Iterable[int],
        *,
        chain: str = "A",
        label: str | None = None,
    ) -> list[ResidueRange]:
        """Compress selected residue IDs into continuous ranges for one chain.

        This mirrors the Studio pocket-selection UX: agents can pass arbitrary
        selected residue IDs and send the resulting ranges to
        ``Peptides.generate(target_residues=..., targeting_strategy="pocket_targeted")``.
        """
        sorted_residues = sorted({int(residue) for residue in residues})
        if not sorted_residues:
            return []

        ranges: list[ResidueRange] = []
        start = prev = sorted_residues[0]
        for residue in sorted_residues[1:]:
            if residue == prev + 1:
                prev = residue
                continue
            ranges.append(cls(chain=chain, start=start, end=prev, label=label))
            start = prev = residue
        ranges.append(cls(chain=chain, start=start, end=prev, label=label))
        return ranges


class StructureAnalysis(_LGModel):
    gene: str
    pocket_count: int | None = Field(default=None, alias="pocketCount")
    recommended_pocket: ResidueRange | None = Field(default=None, alias="recommendedPocket")
    pockets: list[ResidueRange] | None = None
    vacancy_score: float | None = Field(default=None, alias="vacancyScore")
    surface_features: dict[str, Any] | None = Field(default=None, alias="surfaceFeatures")


class GeneResolution(_LGModel):
    query: str
    gene_symbol: str | None = Field(default=None, alias="geneSymbol")
    uniprot_id: str | None = Field(default=None, alias="uniprotId")
    organism: str | None = None
    confidence: float | None = None


# -- Discovery / transcriptomics ---------------------------------------------


class CustomDatasetTarget(_LGModel):
    dataset_id: str = Field(alias="datasetId")
    cell_types: list[str] | None = Field(default=None, alias="cellTypes")
    samples: list[str] | None = None


class TargetGroup(_LGModel):
    name: str
    samples: list[str]
    type: Literal["tissue", "cell_type", "custom"] = "tissue"


class ReferenceGroup(_LGModel):
    name: str
    samples: list[str]


class TissueMarker(_LGModel):
    gene: str
    si: float | None = None
    csi: float | None = None
    fold_change: float | None = Field(default=None, alias="foldChange")
    target_expression: float | None = Field(default=None, alias="targetExpression")
    rank: int | None = None
    receptor: bool | None = None


class MarkerResponse(_LGModel):
    top: list[TissueMarker]
    total: int | None = None
    metadata: dict[str, Any] | None = None


class ExpressionProfile(_LGModel):
    gene: str
    tissues: dict[str, float] | None = None
    organ_systems: dict[str, float] | None = Field(default=None, alias="organSystems")
    samples: list[dict[str, Any]] | None = None


class ComparisonResponse(_LGModel):
    target_group: str = Field(alias="targetGroup")
    reference_groups: list[str] = Field(alias="referenceGroups")
    mode: str
    results: list[TissueMarker]


class GeoDataset(_LGModel):
    accession: str
    title: str | None = None
    organism: str | None = None
    sample_count: int | None = Field(default=None, alias="sampleCount")
    summary: str | None = None


class GeoImportJob(_LGModel):
    job_id: str = Field(alias="jobId")
    accession: str
    status: str
    progress: float | None = None


class Dataset(_LGModel):
    id: str | int
    name: str
    type: str
    sample_count: int | None = Field(default=None, alias="sampleCount")
    cell_count: int | None = Field(default=None, alias="cellCount")
    uploaded_at: datetime | None = Field(default=None, alias="uploadedAt")


class BBBReceptor(_LGModel):
    gene: str
    score: float
    risk_factors: list[str] | None = Field(default=None, alias="riskFactors")
    notes: str | None = None


# -- Peptides / generation / folding ----------------------------------------


class StabilityScores(_LGModel):
    """Proteolytic stability + half-life scores emitted by the guided Modal worker.

    Populated when ``serum_stability=True`` (and/or ``halflife`` is set) in
    :meth:`~ligandai.resources.peptides.Peptides.generate`.

    All fields are optional — they are present only when the corresponding
    guidance module was active during generation.
    """

    predicted_halflife_min: float | None = Field(
        default=None, alias="predicted_halflife_min",
        description="Predicted plasma half-life in minutes.",
    )
    predicted_halflife_hours: float | None = Field(
        default=None, alias="predicted_halflife_hours",
        description="Predicted plasma half-life in hours.",
    )
    cleavage_risk_score: float | None = Field(
        default=None, alias="cleavage_risk_score",
        description="Composite protease cleavage risk (0 = stable, 1 = labile).",
    )
    n_terminal_class: str | None = Field(
        default=None, alias="n_terminal_class",
        description="N-end rule class (e.g. 'stabilizing', 'destabilizing').",
    )
    n_terminal_halflife_min: float | None = Field(
        default=None, alias="n_terminal_halflife_min",
    )
    c_terminal_score: float | None = Field(
        default=None, alias="c_terminal_score",
    )
    stability_grade: str | None = Field(
        default=None, alias="stability_grade",
        description="Composite proteolytic stability grade A-F.",
    )
    trypsin_sites: int | None = Field(default=None, alias="trypsin_sites")
    chymotrypsin_sites: int | None = Field(default=None, alias="chymotrypsin_sites")
    dppiv_vulnerable: bool | None = Field(default=None, alias="dppiv_vulnerable")
    trp_count: int | None = Field(default=None, alias="trp_count")


class ImmunoScores(_LGModel):
    """Immunogenicity scores emitted by the guided Modal worker.

    Populated when ``immunogenicity=True`` in
    :meth:`~ligandai.resources.peptides.Peptides.generate`.
    """

    immuno_risk_score: float | None = Field(
        default=None, alias="immuno_risk_score",
        description="Composite immunogenicity risk score (0 = low, 1 = high).",
    )
    immuno_grade: str | None = Field(
        default=None, alias="immuno_grade",
        description="Composite immunogenicity grade A-F.",
    )
    mhc_i_epitope_count: int | None = Field(default=None, alias="mhc_i_epitope_count")
    mhc_ii_epitope_count: int | None = Field(default=None, alias="mhc_ii_epitope_count")
    tap_transport_score: float | None = Field(default=None, alias="tap_transport_score")
    bcr_epitope_score: float | None = Field(default=None, alias="bcr_epitope_score")
    tcr_contact_score: float | None = Field(default=None, alias="tcr_contact_score")
    population_coverage_pct: float | None = Field(
        default=None, alias="population_coverage_pct",
        description="Estimated population HLA coverage percentage.",
    )


class Peptide(_LGModel):
    name: str | None = None
    sequence: str
    target_gene: str | None = Field(default=None, alias="targetGene")
    # Pre-fold predicted scores (available immediately after generation)
    predicted_ipsae: float | None = Field(default=None, alias="predictedIpsae")
    predicted_iptm: float | None = Field(default=None, alias="predictedIptm")
    predicted_ptm: float | None = Field(default=None, alias="predictedPtm")
    predicted_plddt: float | None = Field(default=None, alias="predictedPlddt")
    binder_prob: float | None = Field(default=None, alias="binderProb")
    ligandiq: float | None = None
    # Post-fold structural scores (only available after Boltz-2 folding)
    ipsae: float | None = None
    iptm: float | None = None
    ptm: float | None = None
    plddt: float | None = None
    # DeltaForge thermodynamic scores (only available after fold + scoring)
    deltaforge_dg: float | None = Field(default=None, alias="deltaforgeDg")
    deltaforge_kd: float | None = Field(default=None, alias="deltaforgeKd")
    classification: str | None = None
    rank: int | None = None
    fold_id: str | None = Field(default=None, alias="foldId")
    pdb_url: str | None = Field(default=None, alias="pdbUrl")
    # Academia+ tier guidance scores (populated when the corresponding guidance
    # module was active during generation)
    stability_grade: str | None = Field(
        default=None, alias="stabilityGrade",
        description="Composite proteolytic stability grade A-F.",
    )
    immunogenicity_score: float | None = Field(
        default=None, alias="immunogenicityScore",
        description="Composite immunogenicity risk score (0-1).",
    )
    # Structured stability / immunogenicity sub-scores.
    # Sourced from the ``stability_scores`` / ``immuno_scores`` JSONB columns.
    stability_scores: StabilityScores | None = Field(
        default=None, alias="stability_scores",
        description="Detailed proteolytic stability + half-life scores.",
    )
    immuno_scores: ImmunoScores | None = Field(
        default=None, alias="immuno_scores",
        description="Detailed immunogenicity epitope scores.",
    )
    # Cyclic mode used during generation (from guidance_config.cyclicMode)
    cyclic_mode: str | None = Field(
        default=None, alias="cyclicMode",
        description="Cyclic constraint active during generation: 'none'|'lactam'|'disulfide'|'head_tail_contact'.",
    )


class GeneSummary(_LGModel):
    """Per-gene peptide aggregation row from ``client.peptides.by_gene()``.

    Mirrors the server-side ``AggregatePeptidesByGeneRow`` (see
    ``server/storage.ts``) and the response shape of
    ``GET /api/v1/peptides/by-gene``.

    A ``GeneSummary`` answers "what binders do I have for gene X?" — folded
    counts, best scores, session/program coverage. To get the actual peptide
    sequences for a gene, follow up with :meth:`Peptides.list`.
    """

    gene: str = Field(description="Canonical UPPER-cased gene symbol.")
    folded_count: int = Field(
        alias="foldedCount",
        description="Total non-deleted folds for this gene.",
    )
    elite_count: int = Field(
        alias="eliteCount",
        description="Folds with iPSAE ≥ 0.85.",
    )
    great_plus_count: int = Field(
        alias="greatPlusCount",
        description="Folds with iPSAE ≥ 0.66.",
    )
    best_ipsae: float = Field(
        alias="bestIpsae",
        description="MAX(iPSAE) across all folds for this gene.",
    )
    best_deltaforge_dg: float | None = Field(
        default=None,
        alias="bestDeltaforgeDg",
        description="Legacy MIN(delta_g) from the unversioned DeltaForge field.",
    )
    best_deltaforge_v10_dg: float | None = Field(
        default=None,
        alias="bestDeltaforgeV10Dg",
        description="Versioned V10 MIN(delta_g) from ptf_deltaforge_scores.",
    )
    deltaforge_v10_scored_count: int = Field(
        default=0,
        alias="deltaforgeV10ScoredCount",
        description="Number of scored V10 folds contributing to this gene row.",
    )
    deltaforge_v10_scorer_version: str | None = Field(
        default=None,
        alias="deltaforgeV10ScorerVersion",
        description="Version label for the V10 aggregate, when present.",
    )
    session_count: int = Field(
        alias="sessionCount",
        description="Distinct PTF sessions producing this gene.",
    )
    program_count: int = Field(
        alias="programCount",
        description="Distinct non-null program_db_id values.",
    )
    last_activity_at: datetime = Field(
        alias="lastActivityAt",
        description="MAX(created_at) across all folds for this gene.",
    )


class PeptideDetail(_LGModel):
    """Single-peptide detail returned by ``client.peptides.get(id)``.

    Default response is "thin" — sequence + scores + metadata, no heavy
    fields. The heavy fields below are populated only when the matching
    string is passed to ``include=`` on the SDK call:

    - ``include=["pocket_features"]`` →  ``pocket_features_48_dim`` +
      ``pocket_features_metadata``
    - ``include=["interface"]`` → ``peptide_per_receptor`` +
      ``disulfide_analysis``
    - ``include=["pdb"]`` → ``pdb_content``

    Mirrors the server's ``GET /api/v1/peptides/:id`` response shape.
    """

    id: int = Field(description="ptf_fold_results.id (serial primary key).")
    gene: str
    session_id: str = Field(alias="sessionId")
    sequence: str
    conformation: str | None = None
    ipsae: float | None = None
    ptm: float | None = None
    iptm: float | None = None
    plddt: float | None = None
    delta_g: float | None = Field(default=None, alias="deltaG")
    predicted_kd: float | None = Field(default=None, alias="predictedKd")
    deltaforge_v10: DeltaForgeScore | None = Field(default=None, alias="deltaforgeV10")
    created_at: datetime = Field(alias="createdAt")

    # Gated by include=["pocket_features"]
    pocket_features_48_dim: list[list[float]] | None = Field(
        default=None,
        alias="pocketFeatures48Dim",
        description=(
            "Per-residue 48-dim pocket feature matrix from generation. Shape "
            "[n_pocket_residues][48]. Populated only when 'pocket_features' "
            "is in the include= list."
        ),
    )
    pocket_features_metadata: dict[str, Any] | None = Field(
        default=None,
        alias="pocketFeaturesMetadata",
        description=(
            "Pocket metadata accompanying the 48-dim matrix — pocket_residue_indices, "
            "target_regions, conformation_name, etc."
        ),
    )

    # Gated by include=["interface"]
    peptide_per_receptor: dict[str, dict[str, float]] | None = Field(
        default=None,
        alias="peptidePerReceptor",
        description=(
            "Per-receptor-chain interface metrics keyed by chain id, with "
            "values { ipsae, ipae, pdockq2, n_contacts }."
        ),
    )
    disulfide_analysis: dict[str, Any] | None = Field(
        default=None,
        alias="disulfideAnalysis",
        description=(
            "Post-fold cysteine geometry analysis: { pairs, unpaired_cys, total_cys }."
        ),
    )

    # Gated by include=["pdb"]
    pdb_content: str | None = Field(
        default=None,
        alias="pdbContent",
        description="Full PDB text content (5-50KB).",
    )


class PeptideInput(_LGModel):
    sequence: str
    name: str | None = None
    target_gene: str | None = Field(default=None, alias="targetGene")


class Sequence(_LGModel):
    """A peptide sequence + optional receptor pairing for folding."""

    sequence: str
    name: str | None = None
    target_gene: str | None = Field(default=None, alias="targetGene")
    target_chain: str | None = Field(default=None, alias="targetChain")
    msa: bool | None = None


class GenerationResult(_LGModel):
    job_id: str = Field(alias="jobId")
    session_id: str | None = Field(default=None, alias="sessionId")
    gene: str
    peptides: list[Peptide]
    total_generated: int | None = Field(default=None, alias="totalGenerated")
    parameters: dict[str, Any] | None = None


class FoldResult(_LGModel):
    job_id: str = Field(alias="jobId")
    pdb_url: str | None = Field(default=None, alias="pdbUrl")
    pdb_data: str | None = Field(default=None, alias="pdbData")
    iptm: float | None = None
    ipsae: float | None = None
    plddt: float | None = None
    ptm: float | None = None
    chain_pair_iptm: dict[str, float] | None = Field(default=None, alias="chainPairIptm")


class DeltaForgeBestPair(_LGModel):
    receptor_chain: str | None = Field(default=None, alias="receptor_chain")
    peptide_chain: str | None = Field(default=None, alias="peptide_chain")
    dg: float | None = Field(default=None, alias="delta_g")
    kd_nm: float | None = Field(default=None, alias="kd_nm")


class DeltaForgePairScore(_LGModel):
    receptor_chain: str | None = Field(default=None, alias="receptor_chain")
    peptide_chain: str | None = Field(default=None, alias="peptide_chain")
    dg: float | None = Field(default=None, alias="delta_g")
    kd_nm: float | None = Field(default=None, alias="kd_nm")
    contacts: int | None = None
    hydrogen_bonds: int | None = Field(default=None, alias="hydrogen_bonds")
    salt_bridges: int | None = Field(default=None, alias="salt_bridges")
    hydrophobic_contacts: int | None = Field(default=None, alias="hydrophobic_contacts")
    features: dict[str, Any] | None = None


class DeltaForgeScore(_LGModel):
    dg: float | None = None
    kd: float | None = None
    kd_nm: float | None = Field(default=None, alias="kd_nm")
    contacts: int | None = None
    interface_residues: list[int] | None = Field(default=None, alias="interfaceResidues")
    scorer: str | None = None
    scorer_version: str | None = Field(default=None, alias="scorer_version")
    model_sha256: str | None = Field(default=None, alias="model_sha256")
    feature_schema_version: str | None = Field(default=None, alias="feature_schema_version")
    aggregate_method: str | None = Field(default=None, alias="aggregate_method")
    best_pair: DeltaForgeBestPair | None = Field(default=None, alias="best_pair")
    pair_scores: list[DeltaForgePairScore] | None = Field(default=None, alias="pair_scores")
    pair_errors: list[dict[str, Any]] | None = Field(default=None, alias="pair_errors")
    warnings: list[str] | None = None
    metadata: dict[str, Any] | None = None


class LigandIQScore(_LGModel):
    sequence: str
    target_gene: str | None = Field(default=None, alias="targetGene")
    score: float
    classification: str | None = None
    fold_change: float | None = Field(default=None, alias="foldChange")


class SolubilityResult(_LGModel):
    sequence: str
    gravy: float | None = None
    cysteine_count: int | None = Field(default=None, alias="cysteineCount")
    multi_cys_flag: bool | None = Field(default=None, alias="multiCysFlag")
    passes_filter: bool | None = Field(default=None, alias="passesFilter")
    notes: str | None = None


# -- Bivalent ----------------------------------------------------------------


class BivalentTarget(_LGModel):
    gene: str
    chain: str | None = "A"
    pocket: ResidueRange | None = None


class LinkerConfig(_LGModel):
    position: Literal["N", "C", "internal"] = "C"
    length_min: int = Field(alias="lengthMin")
    length_max: int = Field(alias="lengthMax")
    composition: str | None = None  # e.g. "GGS"


class BivalentSession(_LGModel):
    id: str
    target1: BivalentTarget
    target2: BivalentTarget
    linker: LinkerConfig
    status: str
    run1_job_id: str | None = Field(default=None, alias="run1JobId")
    run2_job_id: str | None = Field(default=None, alias="run2JobId")
    created_at: datetime | None = Field(default=None, alias="createdAt")


class FoldCandidate(_LGModel):
    sequence: str
    name: str | None = None
    iptm: float | None = None


class GenerationAnalysis(_LGModel):
    session_id: str = Field(alias="sessionId")
    stage: str
    summary: str
    recommendations: list[str] | None = None


class FoldAnalysis(_LGModel):
    session_id: str = Field(alias="sessionId")
    fold_mode: str = Field(alias="foldMode")
    summary: str
    top_candidates: list[FoldCandidate] | None = Field(default=None, alias="topCandidates")


# -- Proteins ----------------------------------------------------------------


class ProteinInfo(_LGModel):
    gene: str
    uniprot_id: str | None = Field(default=None, alias="uniprotId")
    sequence: str | None = None
    length: int | None = None
    organism: str | None = None
    domains: list[dict[str, Any]] | None = None
    ptms: list[dict[str, Any]] | None = None
    description: str | None = None


class DisorderProfile(_LGModel):
    gene: str
    plddt: list[float] | None = None
    disorder_scores: list[float] | None = Field(default=None, alias="disorderScores")
    disordered_regions: list[ResidueRange] | None = Field(default=None, alias="disorderedRegions")


class ReceptorTopology(_LGModel):
    gene: str
    tm_regions: list[ResidueRange] | None = Field(default=None, alias="tmRegions")
    extracellular: list[ResidueRange] | None = None
    intracellular: list[ResidueRange] | None = None
    signal_peptide: ResidueRange | None = Field(default=None, alias="signalPeptide")


class ReceptorIntelligence(_LGModel):
    gene: str
    endocytosis: dict[str, Any] | None = None
    internalization: dict[str, Any] | None = None
    biased_agonism: dict[str, Any] | None = Field(default=None, alias="biasedAgonism")


class GlycosylationData(_LGModel):
    gene: str
    n_linked_sites: list[int] | None = Field(default=None, alias="nLinkedSites")
    o_linked_sites: list[int] | None = Field(default=None, alias="oLinkedSites")
    tissue_compatibility: dict[str, float] | None = Field(default=None, alias="tissueCompatibility")


class ProteinVariant(_LGModel):
    id: int
    gene: str
    alias: str | None = None
    mutations: list[str] | None = None
    pdb_url: str | None = Field(default=None, alias="pdbUrl")
    is_shared: bool | None = Field(default=None, alias="isShared")
    created_at: datetime | None = Field(default=None, alias="createdAt")


class UserProtein(_LGModel):
    id: int
    gene: str
    custom_name: str | None = Field(default=None, alias="customName")
    pdb_url: str | None = Field(default=None, alias="pdbUrl")
    uploaded_at: datetime | None = Field(default=None, alias="uploadedAt")


# -- Diseases ----------------------------------------------------------------


class Disease(_LGModel):
    id: int
    name: str
    category: str | None = None
    summary: str | None = None
    gene_count: int | None = Field(default=None, alias="geneCount")


class Mutation(_LGModel):
    gene: str
    variant: str
    consequence: str | None = None
    pathogenic: bool | None = None
    disease_id: int | None = Field(default=None, alias="diseaseId")


# -- Synthesis / BLI Linker ---------------------------------------------------


class BiotinLinker(_LGModel):
    """A biotinylation linker/spacer option for BLI synthesis orders.

    Mirrors ``LinkerConfig`` in ``server/linker-configuration.ts``.
    Used by :meth:`~ligandai.resources.synthesis.Synthesis.linker_options`,
    :meth:`~ligandai.resources.synthesis.Synthesis.recommend_linker`, and
    :meth:`~ligandai.resources.synthesis.Synthesis.generation_mask_guidance`.
    """

    id: str
    position: Literal["n_terminal", "c_terminal"]
    type: str
    description: str
    format: str | None = None
    recommended_for: list[str] = Field(default_factory=list, alias="recommendedFor")
    length_angstroms: float = Field(alias="lengthAngstroms")
    cost_addon: int | None = Field(default=None, alias="costAddon")
    flexibility: Literal["rigid", "semi_flexible", "flexible", "highly_flexible"] | None = None


class LinkerRecommendation(_LGModel):
    """Server-recommended BLI biotinylation linker with reasoning."""

    recommended: BiotinLinker
    alternatives: list[BiotinLinker] = Field(default_factory=list)
    reasoning: list[str] = Field(default_factory=list)


class BindingOrientationResult(_LGModel):
    """Which peptide terminus contacts the target — drives biotinylation choice."""

    binding_end: Literal["n", "c", "middle"] = Field(alias="bindingEnd")
    recommended_biotinylation: Literal["n", "c"] = Field(alias="recommendedBiotinylation")
    confidence: float
    reasoning: str
    contact_density: dict[str, int] = Field(default_factory=dict, alias="contactDensity")


class GenerationMaskGuidance(_LGModel):
    """Generation-time mask hint derived from the planned BLI linker.

    Tells the generator which terminus to avoid placing the binding interface
    near (because that end will be tethered to the sensor surface).
    """

    avoid_binding_region: Literal["n_terminal", "c_terminal"] | None = Field(
        default=None, alias="avoidBindingRegion"
    )
    avoid_residue_count: int = Field(default=0, alias="avoidResidueCount")
    mask_hint: str = Field(alias="maskHint")
    generation_constraints: dict[str, Any] = Field(
        default_factory=dict, alias="generationConstraints"
    )


# -- Segment / scaffold config ------------------------------------------------


class PeptideSegment(_LGModel):
    """One segment in a multi-segment peptide design.

    ``type`` values:
    - ``"binding"``   — diffusion-generated with binding objective (contacts target)
    - ``"linker"``    — diffusion-generated without binding mask (flexible connector)
    - ``"stability"`` — diffusion-generated with intramolecular stability contacts
    - ``"premade"``   — fixed, user-provided sequence (no generation)
    """

    id: str
    type: Literal["binding", "linker", "stability", "premade"]
    position: int
    sequence: str | None = None
    length_range: tuple[int, int] | None = Field(default=None, alias="lengthRange")
    label: str | None = None
    locked: bool = False


class SegmentConfig(_LGModel):
    """Multi-segment scaffold configuration for complex peptide designs.

    ``mode="simple"`` — single contiguous binding domain (length_range applies to whole peptide).
    ``mode="custom"`` — explicit ordered list of segments with individual types and lengths.

    Presets exposed in the UI:
    - ``"simple_binding"`` — one binding segment [20-50 AA]
    - ``"stable_binding"`` — stability cap + binding core + stability cap
    - ``"tat_cpp"`` — premade TAT + binding domain
    - ``"helix_loop_helix"`` — binding + linker + binding
    - ``"nes_signal"`` — binding domain + premade NES signal
    - ``"rigid_linker_binding"`` — binding + premade GS rigid helical linker
    """

    mode: Literal["simple", "custom"] = "simple"
    length_range: tuple[int, int] = Field(default=(20, 70), alias="lengthRange")
    segments: list[PeptideSegment] = Field(default_factory=list)
    auto_switch_to_custom: bool = Field(default=False, alias="autoSwitchToCustom")


class PdcConfig(_LGModel):
    """Peptide-Drug Conjugate configuration (Pro+ tier).

    The drug payload is co-folded with Boltz-2 for accurate 3D structure prediction.
    Built-in drugs: ciprofloxacin, vancomycin, gentamicin, doxorubicin, MMAE,
    maytansine, FITC, Cy5, Alexa488, biotin, SN-38, gemcitabine.
    """

    drug_name: str | None = Field(default=None, alias="drugName")
    drug_smiles: str | None = Field(default=None, alias="drugSmiles")
    drug_mw: float | None = Field(default=None, alias="drugMw")
    linker_sequence: str = Field(default="GSGSG", alias="linkerSequence")
    linker_position: Literal["n_terminal", "c_terminal"] = Field(
        default="c_terminal", alias="linkerPosition"
    )
    linker_type: Literal["stable", "cleavable_protease", "cleavable_ph", "disulfide"] = Field(
        default="stable", alias="linkerType"
    )
    conjugation_chemistry: Literal["amide", "thioether", "ester", "click"] = Field(
        default="amide", alias="conjugationChemistry"
    )


class EcTrimmingConfig(_LGModel):
    """Full EC-trimming / structure-preparation configuration.

    ``generation_mode`` controls which portion of the receptor is used for LigandForge
    pocket feature extraction. ``folding_mode`` controls what is sent to Boltz-2.

    Defaults are topology-aware:
    - Single-pass TM: ``ec_only`` / ``ec_only``
    - Multi-pass TM (GPCRs): ``ec_tm`` / ``trim_terminal_ic``
    """

    remove_signal_peptide: bool = Field(default=True, alias="removeSignalPeptide")
    generation_mode: Literal["ec_only", "ec_tm", "full"] = Field(
        default="ec_only", alias="generationMode"
    )
    folding_mode: Literal["ec_only", "trim_terminal_ic", "full"] = Field(
        default="ec_only", alias="foldingMode"
    )


# -- Synthesis ----------------------------------------------------------------


class SynthesisOption(_LGModel):
    id: str
    name: str
    category: Literal["linker", "modification", "purity", "quantity"] | str
    price_usd: float | None = Field(default=None, alias="priceUsd")
    description: str | None = None


class SynthesisOptions(_LGModel):
    linkers: list[SynthesisOption] | None = None
    modifications: list[SynthesisOption] | None = None
    purities: list[SynthesisOption] | None = None
    quantities: list[SynthesisOption] | None = None


class SynthesisPeptide(_LGModel):
    sequence: str
    name: str | None = None
    quantity: str = "1mg"
    purity: str = ">95%"
    n_term_mod: str | None = Field(default=None, alias="nTermMod")
    c_term_mod: str | None = Field(default=None, alias="cTermMod")
    notes: str | None = None


class SynthesisQuote(_LGModel):
    total_usd: float = Field(alias="totalUsd")
    line_items: list[dict[str, Any]] = Field(alias="lineItems")
    delivery_weeks: float | None = Field(default=None, alias="deliveryWeeks")
    bli_included: bool | None = Field(default=None, alias="bliIncluded")
    target_expression_included: bool | None = Field(default=None, alias="targetExpressionIncluded")


class SynthesisRecommendation(_LGModel):
    intent: str
    synthesis_mode: str = Field(alias="synthesisMode")
    recommendations: list[dict[str, Any]]
    rationale: str | None = None


class SynthesisCart(_LGModel):
    cart_id: str = Field(alias="cartId")
    deep_link: str | None = Field(default=None, alias="deepLink")
    total_usd: float | None = Field(default=None, alias="totalUsd")
    item_count: int | None = Field(default=None, alias="itemCount")


class SynthesisOrder(_LGModel):
    id: str
    cart_id: str | None = Field(default=None, alias="cartId")
    status: str
    placed_at: datetime | None = Field(default=None, alias="placedAt")
    total_usd: float | None = Field(default=None, alias="totalUsd")
    vendor: str | None = None


class AdaptyvSequence(_LGModel):
    sequence: str
    name: str | None = None
    quantity: str | None = None


class AdaptyvExperiment(_LGModel):
    id: str
    target: str | None = None
    status: str
    sequences: list[AdaptyvSequence] | None = None
    quote_usd: float | None = Field(default=None, alias="quoteUsd")
    placed_at: datetime | None = Field(default=None, alias="placedAt")


class AdaptyvTarget(_LGModel):
    id: str
    name: str
    organism: str | None = None
    description: str | None = None


# -- Memory / activity --------------------------------------------------------


class MemoryItem(_LGModel):
    id: str | int
    title: str | None = None
    content: str
    memory_type: str = Field(alias="memoryType")
    tags: list[str] | None = None
    created_at: datetime | None = Field(default=None, alias="createdAt")
    relevance: float | None = None


class RecentActivity(_LGModel):
    sessions: list[dict[str, Any]] | None = None
    programs: list[dict[str, Any]] | None = None
    results: list[dict[str, Any]] | None = None


# -- Programs / sessions ------------------------------------------------------


class Program(_LGModel):
    id: int
    name: str
    description: str | None = None
    color: str | None = None
    created_at: datetime | None = Field(default=None, alias="createdAt")


class Workstream(_LGModel):
    id: int
    program_id: int = Field(alias="programId")
    name: str
    description: str | None = None
    color: str | None = None
    genes: list[str] | None = None


class ProgramDetail(Program):
    workstreams: list[Workstream] | None = None
    session_count: int | None = Field(default=None, alias="sessionCount")


class Session(_LGModel):
    id: str
    gene: str | None = None
    program_id: int | None = Field(default=None, alias="programId")
    workstream_id: int | None = Field(default=None, alias="workstreamId")
    status: str | None = None
    created_at: datetime | None = Field(default=None, alias="createdAt")


class SessionDetail(Session):
    peptide_count: int | None = Field(default=None, alias="peptideCount")
    fold_count: int | None = Field(default=None, alias="foldCount")
    parameters: dict[str, Any] | None = None


# -- Charts / reports --------------------------------------------------------


class Chart(_LGModel):
    id: str
    chart_type: str = Field(alias="chartType")
    title: str
    image_url: str | None = Field(default=None, alias="imageUrl")
    created_at: datetime | None = Field(default=None, alias="createdAt")


class ReportSection(_LGModel):
    title: str
    content: str
    section_type: Literal["text", "table", "chart", "code"] | str = Field(
        default="text", alias="sectionType"
    )


class Report(_LGModel):
    id: str
    title: str
    pdf_url: str | None = Field(default=None, alias="pdfUrl")
    created_at: datetime | None = Field(default=None, alias="createdAt")


# -- Jobs --------------------------------------------------------------------


class JobInfo(_LGModel):
    id: str
    type: Literal["generation", "folding", "scoring"] | str
    status: Literal["queued", "running", "complete", "failed", "cancelled"] | str
    progress: float | None = None
    estimated_credits: int | None = Field(default=None, alias="estimatedCredits")
    created_at: datetime | None = Field(default=None, alias="createdAt")
    completed_at: datetime | None = Field(default=None, alias="completedAt")
    error_message: str | None = Field(default=None, alias="errorMessage")
    result: dict[str, Any] | None = None


class JobEvent(_LGModel):
    """A single event from a job's SSE stream."""

    event_type: str = Field(alias="eventType")
    stage: str | None = None
    message: str | None = None
    progress: float | None = None
    payload: dict[str, Any] | None = None
    timestamp: datetime | None = None


class StopAllResult(_LGModel):
    cancelled_count: int = Field(alias="cancelledCount")
    job_ids: list[str] = Field(alias="jobIds")
