# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Public pydantic models for SDK request/response payloads.

Models mirror the platform's request/response schemas. They are
intentionally permissive (``ConfigDict(extra="allow")``) so additive platform
changes don't break SDK callers — but every documented field is typed.

For internal-only types (HTTP machinery, rate limiter state) see
:mod:`ligandai._internal`.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    """Credit balance payload from ``GET /api/user-credits``.

    Notes
    -----
    The server may return a sentinel value (e.g. ``Number.MAX_SAFE_INTEGER``
    = 9_007_199_254_740_991, or 1e16) for superadmin / unlimited accounts.
    When the raw ``balance`` exceeds :data:`UNLIMITED_BALANCE_THRESHOLD`
    (1e10), the SDK treats it as unlimited and surfaces
    :attr:`is_unlimited` as ``True`` rather than exposing a giant int.

    Either ``balance`` or ``credits`` may appear depending on endpoint —
    they're aliased to the same field. Validators may also see a top-level
    ``isUnlimited`` flag from the server in future revisions.
    """

    balance: int = 0
    credits: int | None = None
    is_unlimited: bool | None = Field(default=None, alias="isUnlimited")
    monthly_allocation: int | None = Field(default=None, alias="monthlyAllocation")
    next_refill_at: datetime | None = Field(default=None, alias="nextRefillAt")

    @model_validator(mode="before")
    @classmethod
    def _coerce_balance_and_flag_unlimited(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        d = dict(data)
        # Accept either 'balance' or 'credits' key; mirror to both attrs.
        raw = d.get("balance")
        if raw is None:
            raw = d.get("credits")
        try:
            bal = int(raw) if raw is not None else 0
        except (TypeError, ValueError):
            bal = 0
        d["balance"] = bal
        if "credits" not in d or d.get("credits") is None:
            d["credits"] = bal
        # Sentinel detection — superadmin / unlimited / tier-bug accounts.
        if bal >= 10_000_000_000 and d.get("isUnlimited") is None and d.get("is_unlimited") is None:
            d["isUnlimited"] = True
        return d


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


class CreditsWidget(_LGModel):
    """Snapshot of credit state for a compact billing widget.

    Conversion rate: 100 credits = $0.01 → 1 USD = 10,000 credits.

    The :attr:`pct_used` field is 0-100 (rounded). The :attr:`reset_date`
    is the first of the following month at 00:00 UTC (matches the platform's
    /api/credits/balance response).
    """

    balance_credits: int = Field(alias="available")
    monthly_limit_credits: int = Field(alias="total")
    used_this_month_credits: int = Field(alias="usedThisMonth")
    balance_usd: float
    monthly_limit_usd: float
    spent_this_month_usd: float
    pct_used: int  # 0-100
    reset_date: datetime | None = Field(default=None, alias="resetDate")
    auto_reload_enabled: bool = Field(default=False, alias="autoReplenish")
    auto_reload_threshold_credits: int | None = Field(default=None, alias="threshold")
    auto_reload_amount_credits: int | None = Field(default=None, alias="replenishAmount")
    tier: str | None = None


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
    # Server's canonical identifier is `complexId` (string). The SDK historically
    # required `id` which the server never sends — this caused validation failures
    # for every basic-tier caller of receptors.list(). Both fields are now optional;
    # callers should prefer `complex_id`.
    id: str | int | None = None
    complex_id: str | None = Field(default=None, alias="complexId")
    complex_name: str | None = Field(default=None, alias="complexName")
    gene: str | None = None
    genes: list[str] | None = None
    oligomeric_state: str | None = Field(default=None, alias="oligomericState")
    organism: str | None = None
    pdb_code: str | None = Field(default=None, alias="pdbCode")
    chain_count: int | None = Field(default=None, alias="chainCount")
    has_pdb: bool | None = Field(default=None, alias="hasPdb")
    stoichiometry: str | None = None
    complex_type: str | None = Field(default=None, alias="complexType")
    receptor_class: str | None = Field(default=None, alias="receptorClass")
    confidence: str | None = None
    iptm: float | None = None
    ipsae: float | None = None
    plddt: float | None = None
    ptm: float | None = None
    has_experimental_pdb: bool | None = Field(default=None, alias="hasExperimentalPDB")
    pdb_ids: list[str] | None = Field(default=None, alias="pdbIds")


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
    """Proteolytic stability + half-life scores emitted by the guided design worker.

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
    """Immunogenicity scores emitted by the guided design worker.

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
    # v0.5.0 — fields returned by /api/v1/peptides/list and /api/v1/peptides/search
    peptide_id: int | None = Field(
        default=None, alias="peptide_id",
        description="ptf_fold_results.id — pass to client.peptides.get(peptide_id).",
    )
    length: int | None = Field(
        default=None,
        description="True peptide length (always reported, even when sequence is masked for free tier).",
    )
    predicted_kd: float | None = Field(
        default=None, alias="predictedKd",
        description="Predicted dissociation constant (M) from DeltaForge.",
    )
    predicted_binder: bool | None = Field(
        default=None,
        alias="predictedBinder",
        description="Separate DeltaForge structure/energy binder call.",
    )
    predicted_binder_call: str | None = Field(
        default=None,
        alias="predictedBinderCall",
        description="'binder', 'not_binder', or 'unassigned'.",
    )
    predicted_binder_label: str | None = Field(
        default=None,
        alias="predictedBinderLabel",
        description="Human-readable DeltaForge binder-call label.",
    )
    binder_call_method: str | None = Field(
        default=None,
        alias="binderCallMethod",
        description="Method used to assign the separate binder/non-binder call.",
    )
    predicted_non_binder_reasons: list[str] | None = Field(
        default=None,
        alias="predictedNonBinderReasons",
        description="Failed gate reasons when DeltaForge calls not_binder.",
    )
    is_elite: bool | None = Field(
        default=None, alias="isElite",
        description="Server-side elite classification (typically iPSAE >= 0.85).",
    )
    masked: bool | None = Field(
        default=None, alias="_masked",
        description="True when sequence has been truncated due to tier (free = first 10 AA + '********').",
    )
    # Per-chain iPTM + PAE (post 2026-05-09 schema_version >= 2)
    peptide_interface_iptm: float | None = Field(
        default=None, alias="peptideInterfaceIptm",
        description="Per-peptide-chain iPTM, uncontaminated by protein-protein contributions. "
                    "Distinct from the global ``iptm`` (overall complex score). "
                    "Populated when fold ran with schema_version >= 2 (post 2026-05-09).",
    )
    chain_pair_iptm: dict[str, float] | None = Field(
        default=None, alias="chainPairIptm",
        description="Per-chain-pair iPTM matrix from Boltz-2 summary_confidences. "
                    "Keys are 'A_B', 'A_C', etc. Tier-gated: free/basic see summary "
                    "(min/max/pair_count); academia+ see full matrix.",
    )
    fold_metric_details: dict | None = Field(
        default=None, alias="foldMetricDetails",
        description="Detailed per-chain + per-pair metrics: overall, peptide, perChain, "
                    "peptidePerReceptor, chainPairs, plddtDetails. Same shape as "
                    "ptf_fold_results.fold_metric_details JSONB column.",
    )


class GeneSummary(_LGModel):
    """Per-gene peptide aggregation row from ``client.peptides.by_gene()``.

    Mirrors the platform's per-gene aggregation row and the response shape of
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
    predicted_binder: bool | None = Field(default=None, alias="predictedBinder")
    predicted_binder_call: str | None = Field(default=None, alias="predictedBinderCall")
    predicted_binder_label: str | None = Field(default=None, alias="predictedBinderLabel")
    binder_call_method: str | None = Field(default=None, alias="binderCallMethod")
    predicted_non_binder_reasons: list[str] | None = Field(
        default=None, alias="predictedNonBinderReasons"
    )
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

    @property
    def view_url(self) -> str:
        """Public URL where this run's peptides + folds + scores live on
        ligandai.com. Tell the user to open this in their browser to see
        results, export CSV, view 3D structures, and request synthesis."""
        sid = self.session_id or self.job_id
        return f"https://ligandai.com/workspace?session={sid}"

    @property
    def csv_url(self) -> str:
        """Public CSV-export URL for this run (sequence, scores, fold metrics).
        Authenticated download — same API key as the SDK client."""
        sid = self.session_id or self.job_id
        return f"https://ligandai.com/api/ptf/sessions/{sid}/export.csv"

    def save_to(
        self,
        directory: str | Path,
        *,
        write_pdbs: bool = True,
        write_csv: bool = True,
        write_summary: bool = True,
        transport: Any | None = None,
    ) -> dict[str, Any]:
        """Download this run's artifacts to a local directory.

        Writes:
            * ``peptides.csv``  — every peptide with sequence + scores + ranks
            * ``folds/{rank:03d}_{seq}.pdb`` — folded PDB per peptide
              (only when the peptide has a ``pdb_url`` or ``fold_id`` and a
              ``transport=`` is provided)
            * ``summary.json`` — full :class:`GenerationResult` (for replay)

        Args:
            directory: Output directory (created if missing).
            write_pdbs: When True and a transport is provided, fetch each
                peptide's PDB content and save it. Skipped silently when no
                transport is available — the platform UI is still the
                source of truth in that case.
            write_csv: Write peptides.csv with sequence, predicted scores,
                fold scores (when present), and rank.
            write_summary: Write a summary.json + view_url + csv_url.
            transport: Optional :class:`HTTPTransport` for fetching PDBs;
                pass ``client.transport`` when calling.

        Returns:
            Dict with keys ``directory``, ``peptide_count``, ``pdb_count``,
            ``view_url``, ``csv_url``.
        """
        import csv
        import json
        from pathlib import Path as _Path

        out = _Path(directory).expanduser()
        out.mkdir(parents=True, exist_ok=True)

        if write_csv:
            csv_path = out / "peptides.csv"
            with csv_path.open("w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "rank", "name", "sequence", "target_gene",
                    "ipsae", "iptm", "ptm", "plddt",
                    "predicted_ipsae", "binder_prob", "ligandiq",
                    "deltaforge_dg", "deltaforge_kd",
                    "stability_grade", "immunogenicity_score", "cyclic_mode",
                ])
                for i, p in enumerate(self.peptides, start=1):
                    writer.writerow([
                        p.rank if p.rank is not None else i,
                        p.name or "",
                        p.sequence,
                        p.target_gene or self.gene,
                        p.ipsae, p.iptm, p.ptm, p.plddt,
                        p.predicted_ipsae, p.binder_prob, p.ligandiq,
                        p.deltaforge_dg, p.deltaforge_kd,
                        p.stability_grade, p.immunogenicity_score, p.cyclic_mode,
                    ])

        pdb_count = 0
        if write_pdbs and transport is not None:
            folds_dir = out / "folds"
            folds_dir.mkdir(parents=True, exist_ok=True)

            def _fetch_pdb(idx: int, p: "Peptide") -> tuple[int, "Peptide", str | None]:
                pdb_text: str | None = None
                # Path 1 — direct URL on the peptide
                if p.pdb_url:
                    try:
                        resp = transport.request("GET", p.pdb_url, expect_json=False)
                        if isinstance(resp, (str, bytes)):
                            pdb_text = resp.decode() if isinstance(resp, bytes) else resp
                    except Exception:
                        pdb_text = None
                # Path 2 — fold-id fetch via /api/v1/peptides/:id?include=pdb
                if pdb_text is None and p.fold_id:
                    try:
                        resp = transport.request(
                            "GET", f"/api/v1/peptides/{p.fold_id}",
                            params={"include": "pdb"},
                        ) or {}
                        pdb_text = resp.get("pdbContent") or resp.get("pdb_content")
                    except Exception:
                        pdb_text = None
                # Path 3 — session+sequence endpoint (canonical fallback for
                # post-fold sessions where pdb_url / fold_id aren't echoed back).
                if pdb_text is None and self.session_id and p.sequence:
                    try:
                        resp = transport.request(
                            "GET",
                            f"/api/ptf/sessions/{self.session_id}/pdb/{p.sequence}",
                        ) or {}
                        if isinstance(resp, dict):
                            pdb_text = resp.get("pdb_content") or resp.get("pdbContent") or resp.get("pdb")
                        elif isinstance(resp, (str, bytes)):
                            pdb_text = resp.decode() if isinstance(resp, bytes) else resp
                    except Exception:
                        pdb_text = None
                return idx, p, pdb_text

            # Parallel batch fetch — 8 concurrent requests is comfortable under
            # academia/pro rate limits (30/60 req/min). Per-peptide GETs are the
            # only fallback today since the platform doesn't expose a single
            # zip-of-PDBs endpoint yet.
            from concurrent.futures import ThreadPoolExecutor, as_completed
            futures = []
            with ThreadPoolExecutor(max_workers=8) as pool:
                for i, p in enumerate(self.peptides, start=1):
                    futures.append(pool.submit(_fetch_pdb, i, p))
                for fut in as_completed(futures):
                    idx, p, pdb_text = fut.result()
                    if not pdb_text:
                        continue
                    rank = p.rank if p.rank is not None else idx
                    fname = f"{rank:03d}_{p.sequence[:20]}.pdb"
                    (folds_dir / fname).write_text(pdb_text)
                    pdb_count += 1

        if write_summary:
            summary = {
                "job_id": self.job_id,
                "session_id": self.session_id,
                "gene": self.gene,
                "view_url": self.view_url,
                "csv_url": self.csv_url,
                "peptide_count": len(self.peptides),
                "pdb_count": pdb_count,
                "parameters": self.parameters,
            }
            (out / "summary.json").write_text(json.dumps(summary, indent=2, default=str))

        return {
            "directory": str(out),
            "peptide_count": len(self.peptides),
            "pdb_count": pdb_count,
            "view_url": self.view_url,
            "csv_url": self.csv_url,
        }


class LigandChain(_LGModel):
    """Small-molecule ligand co-folded alongside the receptor + peptide.

    Boltz-2 folds receptors with their crystallographic ligands intact
    (sulfates, ions, glycans, prosthetic groups, covalent modifiers).
    These come back as separate chains in the PDB and are tracked here
    so platform code never confuses them with protein chains. The
    structure viewer can show/hide them; scorers (LigandIQ, DeltaForge)
    skip them when evaluating peptide-receptor binding.
    """

    chain: str | None = None
    """PDB chain identifier of the ligand (e.g. 'B', 'C')."""

    ccd: str | None = None
    """Three- or four-letter Chemical Component Dictionary code (e.g. 'SO4', 'MG', 'ATP')."""

    smiles: str | None = None
    """SMILES string for non-CCD small molecules."""


class ReceptorChain(_LGModel):
    """A protein chain belonging to the target receptor (not the peptide)."""

    chain: str | None = None
    length: int | None = None
    type: str | None = None  # 'protein' | 'dna' | 'rna'


class FoldResult(_LGModel):
    job_id: str = Field(alias="jobId")
    pdb_url: str | None = Field(default=None, alias="pdbUrl")
    pdb_data: str | None = Field(default=None, alias="pdbData")
    """Full PDB structure content (inline). Non-None when ``has_structure`` is True.

    When ``Job.wait(durable=True)`` (the default) returns, this MUST be a
    non-empty string for a succeeded fold. If it isn't, the SDK raises
    :class:`~ligandai.errors.LigandAIIncompleteResult` instead of silently
    handing back a half-populated result.
    """
    pdb_path: Path | None = Field(default=None, alias="pdbPath")
    """Local filesystem path of the on-disk PDB. Set by the SDK whenever
    ``Job.wait(save_to=...)`` runs OR whenever the SDK writes a side-effect
    copy under ``~/.ligandai/structures/<job_id>.pdb``. Decoupled from
    ``pdb_url`` (server URL) and ``pdb_data`` (inline content)."""
    has_structure: bool = Field(default=False, alias="hasStructure")
    """Mirror of the server's ``has_structure`` flag. The SDK trusts this
    field's True only when ``pdb_data`` is also non-empty — ``status='completed'``
    + ``has_structure=False`` triggers the durable-wait re-poll loop."""
    pae_matrix: list[list[float]] | None = Field(default=None, alias="paeMatrix")
    """Decoded PAE matrix (residue x residue). None for tier < pro/academia OR
    when ``pae_matrix_uri`` has not been hydrated yet — call
    ``client.folds.download_pae(job_id)`` to fetch."""
    cif_data: str | None = Field(default=None, alias="cifData")
    """Full mmCIF content (inline). May be None even when ``pdb_data`` is set
    (older fold writers only emit PDB)."""
    scores: dict[str, Any] | None = None
    """Server-provided per-fold scores (e.g. DeltaForge ΔG, LigandIQ readout)
    when available. May be empty for tier < basic or when the scoring pipeline
    was skipped."""
    metrics: dict[str, float] | None = None
    """Convenience flat dict of headline confidence metrics — keys include
    ``iptm``, ``ipsae``, ``ptm``, ``mean_plddt``, ``peptide_iptm``. Pre-computed
    server-side so callers can subscript without poking individual fields."""
    confidence: dict[str, Any] | None = None
    """Detailed confidence breakdown — per-chain pLDDT, per-pair iPTM, etc.
    Populated by the Boltz-2 writer when schema_version >= 2."""
    iptm: float | None = None
    ipsae: float | None = None
    plddt: float | None = None
    ptm: float | None = None
    ipae: float | None = None
    """Per-interface predicted aligned error (chain-pair). Populated when the
    fold writer ran with schema_version >= 2; nullable for older runs."""

    chain_pair_iptm: dict[str, float] | None = Field(default=None, alias="chainPairIptm")
    per_chain: dict[str, dict[str, float]] | None = Field(default=None, alias="perChain")
    """Per-chain pLDDT / pTM / iPTM map keyed by chain id."""

    peptide_chain_id: str | None = Field(default=None, alias="peptideChainId")
    """PDB chain id of the peptide in the folded structure (e.g. 'D' for a
    PTGES3 monomer with two SO4 ligand chains, 'C' for a typical complex)."""

    receptor_chains: list[ReceptorChain] | None = Field(default=None, alias="receptorChains")
    ligands: list[LigandChain] | None = None
    """Small-molecule ligands present in the fold. Empty list means no
    ligands were co-folded; ``None`` means schema_version < 2 (legacy)."""

    pae_url: str | None = Field(default=None, alias="paeUrl")
    """Lazy-fetch URL for the gzipped PAE matrix. Tier-gated (paid only)."""

    peptide_interface_iptm: float | None = Field(
        default=None, alias="peptideInterfaceIptm",
        description="Per-peptide-chain iPTM, uncontaminated by protein-protein contributions.",
    )
    fold_metric_details: dict | None = Field(
        default=None, alias="foldMetricDetails",
        description="Detailed per-chain + per-pair fold metrics (same as Peptide.fold_metric_details).",
    )
    pae_matrix_uri: str | None = Field(
        default=None, alias="paeMatrixPath",
        description="Canonical PAE URI 'pae://<pdb_hash>:<rank>'. Use client.folds.download_pae(fold_id) "
                    "to fetch the matrix. Tier-gated: academia+.",
    )


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


class DeltaForgeGateReadout(_LGModel):
    """Separate structure/energy binder-call readout from DeltaForge."""

    predicted_binder: bool | None = Field(default=None, alias="predicted_binder")
    predicted_binder_call: str | None = Field(default=None, alias="predicted_binder_call")
    predicted_binder_label: str | None = Field(default=None, alias="predicted_binder_label")
    predicted_binder_probability: float | None = Field(
        default=None, alias="predicted_binder_probability"
    )
    binder_call_method: str | None = Field(default=None, alias="binder_call_method")
    fold_metrics_available: bool | None = Field(default=None, alias="fold_metrics_available")
    gate_passed: bool | None = Field(default=None, alias="gate_passed")
    failed_gate_reasons: list[str] | None = Field(default=None, alias="failed_gate_reasons")
    missing_gate_inputs: list[str] | None = Field(default=None, alias="missing_gate_inputs")


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
    version_family: str | None = Field(default=None, alias="version_family")
    affinity_scorer: str | None = Field(default=None, alias="affinity_scorer")
    affinity_scorer_version: str | None = Field(default=None, alias="affinity_scorer_version")
    calibration_head: str | None = Field(default=None, alias="calibration_head")
    structure_source_detected: str | None = Field(default=None, alias="structure_source_detected")
    calibration_router: str | None = Field(default=None, alias="calibration_router")
    peptide_length: int | None = Field(default=None, alias="peptide_length")
    platform_length_scope: str | None = Field(default=None, alias="platform_length_scope")
    predicted_affinity_tier: str | None = Field(default=None, alias="predicted_affinity_tier")
    predicted_binder: bool | None = Field(default=None, alias="predicted_binder")
    predicted_binder_call: str | None = Field(default=None, alias="predicted_binder_call")
    predicted_binder_label: str | None = Field(default=None, alias="predicted_binder_label")
    predicted_binder_probability: float | None = Field(
        default=None, alias="predicted_binder_probability"
    )
    binder_call_method: str | None = Field(default=None, alias="binder_call_method")
    predicted_non_binder_reasons: list[str] | None = Field(
        default=None, alias="predicted_non_binder_reasons"
    )
    missing_binder_gate_inputs: list[str] | None = Field(
        default=None, alias="missing_binder_gate_inputs"
    )
    readout_note: str | None = Field(default=None, alias="readout_note")
    affinity_plus_structure_readout: dict[str, Any] | None = Field(
        default=None, alias="affinity_plus_structure_readout"
    )
    dual_readout: dict[str, Any] | None = Field(default=None, alias="dual_readout")
    structural_energy_gates: DeltaForgeGateReadout | None = Field(
        default=None, alias="structural_energy_gates"
    )
    best_pair: DeltaForgeBestPair | None = Field(default=None, alias="best_pair")
    pair_scores: list[DeltaForgePairScore] | None = Field(default=None, alias="pair_scores")
    pair_errors: list[dict[str, Any]] | None = Field(default=None, alias="pair_errors")
    warnings: list[str] | None = None
    metadata: dict[str, Any] | None = None
    # Fold confidence metrics — always returned by score-fold (and forwarded by
    # score-pdb when the caller passes fold_*). iPTM is more reliable than iPSAE (which can
    # be inflated); both are surfaced.
    iptm: float | None = None
    ptm: float | None = None
    ipsae: float | None = None
    plddt_mean: float | None = Field(default=None, alias="plddt_mean")
    fold_job_id: str | None = Field(default=None, alias="foldJobId")
    # Optional NxN PAE matrix (Angstroms). Present only when include_pae=True AND
    # the artifact resolved; otherwise pae is None and pae_status explains why
    # ('ok' | 'pending' | 'unavailable').
    pae: list[list[float]] | None = None
    pae_status: str | None = Field(default=None, alias="paeStatus")


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


class LigandScore(_LGModel):
    """LF-SM v3 small-molecule Kd prediction for one (holo structure, ligand).

    Returned by :meth:`ligandai.resources.ligands.Ligands.score_ligand`. The
    free-tier CPU scorer extracts 288-d NEURAL_DOCKER features from a HOLO
    complex (protein + bound ligand HETATM) and runs the KdHead v3 MLP.
    """

    p_kd: float | None = Field(default=None, alias="pKd")
    binder_probability: float | None = Field(default=None, alias="binder_probability")
    binder: bool | None = None
    log_kd_nm_pred: float | None = Field(default=None, alias="logKd_nM_pred")
    model_version: str | None = Field(default=None, alias="model_version")
    feature_status: str | None = Field(default=None, alias="feature_status")
    het_code: str | None = Field(default=None, alias="het_code")
    n_contacts: int | None = Field(default=None, alias="n_contacts")
    n_candidate_ligands: int | None = Field(default=None, alias="n_candidate_ligands")
    ligand_smiles: str | None = Field(default=None, alias="ligand_smiles")
    latency_ms: float | None = Field(default=None, alias="latency_ms")


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
    """Server-registered protein variant.

    All non-id fields are Optional so partial server responses (e.g. when the
    upstream CIF/PDB parser fails to extract gene symbol or chain metadata)
    don't reject the SDK call. Inherits ``extra="allow"`` so additive server
    fields are preserved as-is.
    """

    id: int
    gene: str | None = None
    gene_symbol: str | None = Field(default=None, alias="geneSymbol")
    alias: str | None = None
    custom_name: str | None = Field(default=None, alias="customName")
    user_id: str | None = Field(default=None, alias="userId")
    mutations: list[str] | None = None
    pdb_url: str | None = Field(default=None, alias="pdbUrl")
    chain_count: int | None = Field(default=None, alias="chainCount")
    residue_count: int | None = Field(default=None, alias="residueCount")
    chain_info: list[Any] | None = Field(default=None, alias="chainInfo")
    status: str | None = None
    is_shared: bool | None = Field(default=None, alias="isShared")
    created_at: datetime | None = Field(default=None, alias="createdAt")


class UserProtein(_LGModel):
    """User-uploaded protein record from ``/api/user/proteins/upload``.

    All structural/metadata fields are Optional so the SDK tolerates degraded
    server responses (e.g. CIF parser returning ``residueCount=0``,
    ``chainInfo=[]``, ``geneSymbol=null``). Inherits ``extra="allow"`` from
    :class:`_LGModel` so future server fields are preserved.
    """

    id: int
    gene: str | None = None
    gene_symbol: str | None = Field(default=None, alias="geneSymbol")
    user_id: str | None = Field(default=None, alias="userId")
    custom_name: str | None = Field(default=None, alias="customName")
    pdb_url: str | None = Field(default=None, alias="pdbUrl")
    chain_count: int | None = Field(default=None, alias="chainCount")
    residue_count: int | None = Field(default=None, alias="residueCount")
    chain_info: list[Any] | None = Field(default=None, alias="chainInfo")
    status: str | None = None
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

    Mirrors the platform's linker configuration.
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
    # Server returns either numeric `id` (DB programs from /api/ptf/programs/:id)
    # or stringly `programId` (in-memory programs from /api/ptf/programs list).
    # Make both optional and accept either as the canonical identifier.
    id: int | None = None
    program_id: str | None = Field(default=None, alias="programId")
    name: str | None = None
    description: str | None = None
    color: str | None = None
    status: str | None = None
    gene_count: int | None = Field(default=None, alias="geneCount")
    completed_count: int | None = Field(default=None, alias="completedCount")
    in_progress_count: int | None = Field(default=None, alias="inProgressCount")
    queued_count: int | None = Field(default=None, alias="queuedCount")
    failed_count: int | None = Field(default=None, alias="failedCount")
    total_elites: int | None = Field(default=None, alias="totalElites")
    best_ipsae: float | None = Field(default=None, alias="bestIpsae")
    genes: list[dict[str, Any]] | None = None
    created_at: datetime | None = Field(default=None, alias="createdAt")
    updated_at: datetime | None = Field(default=None, alias="updatedAt")


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
    # /api/jobs/history merges peptide / GPU / folding jobs from heterogeneous
    # tables. Some columns (type, status, progress) aren't always populated for
    # legacy rows. Required-only fields here would fail validation on basic-tier
    # users with old jobs in their history. All fields are optional.
    id: str | None = None
    type: Literal["generation", "folding", "scoring"] | str | None = None
    status: Literal["queued", "running", "complete", "failed", "cancelled"] | str | None = None
    job_source: str | None = Field(default=None, alias="jobSource")
    progress: float | None = None
    estimated_credits: int | None = Field(default=None, alias="estimatedCredits")
    created_at: datetime | None = Field(default=None, alias="createdAt")
    completed_at: datetime | None = Field(default=None, alias="completedAt")
    error_message: str | None = Field(default=None, alias="errorMessage")
    result_count: int | None = Field(default=None, alias="resultCount")
    target_protein: dict[str, Any] | None = Field(default=None, alias="targetProtein")
    workspace_session: dict[str, Any] | None = Field(default=None, alias="workspaceSession")
    metrics: dict[str, Any] | None = None
    result: dict[str, Any] | None = None


class JobEvent(_LGModel):
    """A single event from a job's SSE stream."""

    event_type: str = Field(alias="eventType")
    stage: str | None = None
    message: str | None = None
    progress: float | None = None
    payload: dict[str, Any] | None = None
    timestamp: datetime | None = None


class BatchFoldEvent(_LGModel):
    """A single per-peptide completion event from
    :meth:`~ligandai.resources.peptides.BatchFoldJob.stream`.

    One event is yielded as each sub-job becomes terminal AND its structural
    payload has landed (durable contract — never yields a "completed but PDB
    empty" event). For batches of N peptides,
    callers can pipeline scoring/visualization per-event instead of waiting
    for the whole batch.

    Attributes
    ----------
    record_id : str | None
        Caller-provided per-peptide identifier when supplied, otherwise the
        sub-job's ``job_id``.
    job_id : str
        Server-assigned fold job id (``fold_<ms>_<rand>``).
    peptide_index : int | None
        Zero-based index into the batch's input peptide list.
    peptide_sequence : str | None
        The peptide AA sequence that was folded.
    status : str
        Final sub-job status — ``"succeeded"`` when ``pdb_content`` is non-empty,
        ``"failed"`` / ``"cancelled"`` / ``"incomplete"`` otherwise.
    pdb_content : str | None
        Inline PDB structure content. Non-None on success.
    cif_data : str | None
        Inline mmCIF content (may be None for older fold writers).
    iptm, ipsae, ipae, ptm, mean_plddt
        Headline confidence metrics. None for failed sub-jobs.
    pae_url : str | None
        Lazy-fetch URL for the PAE matrix (tier-gated).
    confidence : dict | None
        Per-chain / per-pair confidence breakdown (when emitted server-side).
    per_chain : dict | None
        Per-chain pLDDT/pTM/iPTM map keyed by chain id.
    phase : str | None
        Free-form processing phase tag (``"submitted"``, ``"folding"``,
        ``"scoring"``, ``"complete"``) — useful for richer UI progress bars.
    timestamp : datetime
        Event emission time.
    """

    record_id: str | None = Field(default=None, alias="recordId")
    job_id: str = Field(alias="jobId")
    peptide_index: int | None = Field(default=None, alias="peptideIndex")
    peptide_sequence: str | None = Field(default=None, alias="peptideSequence")
    status: str
    pdb_content: str | None = Field(default=None, alias="pdbContent")
    cif_data: str | None = Field(default=None, alias="cifData")
    iptm: float | None = None
    ipsae: float | None = None
    ipae: float | None = None
    ptm: float | None = None
    mean_plddt: float | None = Field(default=None, alias="meanPlddt")
    pae_url: str | None = Field(default=None, alias="paeUrl")
    confidence: dict[str, Any] | None = None
    per_chain: dict[str, Any] | None = Field(default=None, alias="perChain")
    phase: str | None = None
    timestamp: datetime | None = None


class StopAllResult(_LGModel):
    cancelled_count: int = Field(alias="cancelledCount")
    job_ids: list[str] = Field(alias="jobIds")
