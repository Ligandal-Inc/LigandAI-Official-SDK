# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Public pydantic models for SDK request/response payloads.

Models mirror the platform's request/response schemas. They are
intentionally permissive (``ConfigDict(extra="allow")``) so additive platform
changes don't break SDK callers — but every documented field is typed.

For internal-only types (HTTP machinery, rate limiter state) see
:mod:`ligandai._internal`.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Any, ClassVar, Literal, TypeAlias, TypedDict, cast, get_args

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)
from pydantic.functional_serializers import SerializerFunctionWrapHandler
from typing_extensions import NotRequired

from ligandai.errors import LigandAIAnalysisNotComputed

# -- Base ---------------------------------------------------------------------


#: A Postgres ``timestamptz`` rendered by ``to_char``/default text output:
#: ``2026-06-11 16:03:02.793753+00``. The offset is TWO digits, which ISO 8601
#: (and pydantic) reject; ``+00:00`` is accepted.
_PG_TIMESTAMPTZ = re.compile(
    r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?[+-]\d{2}$"
)


def _mentions_datetime(annotation: Any) -> bool:
    if annotation is datetime:
        return True
    return any(_mentions_datetime(arg) for arg in get_args(annotation))


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

    @field_validator("*", mode="before")
    @classmethod
    def _complete_two_digit_utc_offset(cls, value: Any, info: Any) -> Any:
        """Accept a Postgres ``...+00`` timestamp in a ``datetime`` field.

        ⛔ Some routes return raw Postgres text (``GET /api/ligandforge/bivalent``
        rows carry ``"created_at": "2026-06-11 16:03:02.793753+00"``). The
        two-digit offset is not ISO 8601, so ``bivalent.list_sessions()`` and
        ``get_session()`` raised ``ValidationError: created_at ... unexpected
        extra characters`` on every row of an account that has any — the whole
        listing lost to one field. Only a string that is EXACTLY such a
        timestamp, in a field typed as a datetime, is touched; ``str`` fields
        keep the server's text verbatim. Measured 2026-10-02 against blue.
        [bd-dre-3imtv.4]
        """
        if isinstance(value, str) and _PG_TIMESTAMPTZ.match(value):
            field = cls.model_fields.get(info.field_name)
            if field is not None and _mentions_datetime(field.annotation):
                return value + ":00"
        return value


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


class UnlimitedCredits(int):
    """An ``int`` representing an effectively-infinite credit balance.

    Returned by :attr:`ligandai.LigandAI.credits` for unlimited / superadmin
    accounts. It **is** an ``int`` — existing arithmetic and
    ``isinstance(balance, int)`` checks keep working, and it compares as larger
    than any realistic cost so pre-flight checks like
    ``if cost <= client.credits:`` behave correctly. But it renders as
    ``"unlimited"`` (via ``str`` / ``repr`` / default ``f"{...}"``) so CLIs and
    logs never print a misleading bare ``0`` or a giant sentinel such as
    ``9,007,199,254,740,991``.

    An explicit numeric format spec still formats the underlying integer, so a
    caller who deliberately asks for digits (``f"{client.credits:,d}"``) gets
    them.
    """

    #: Marker mirroring :attr:`Credits.is_unlimited` for duck-typed checks.
    is_unlimited: bool = True

    # Default to Number.MAX_SAFE_INTEGER — the sentinel the platform returns
    # for unlimited accounts — when no explicit balance is supplied.
    def __new__(cls, value: int = 9_007_199_254_740_991) -> UnlimitedCredits:
        return super().__new__(cls, int(value))

    def __repr__(self) -> str:
        return "unlimited"

    def __str__(self) -> str:
        return "unlimited"

    def __format__(self, format_spec: str) -> str:
        if format_spec == "":
            return "unlimited"
        return super().__format__(format_spec)


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
    # ⛔ A burn RATE is fractional by nature and the server sends it that way
    # (``/api/billing/account-summary`` returned ``burn_rate_30d: 17.2``). Typed
    # ``int`` it raised a raw pydantic ValidationError straight out of
    # ``client.account.get_balance()`` — not a LigandAI* error, on the one call
    # the fold/panel docs tell users to make before spending. [bd-dre-if562]
    burn_rate_30d: float | None = Field(default=None, alias="burnRate30d")
    # The same endpoint names the runway ``runway_days``; without the alias
    # choice this field was silently None on every real response.
    days_remaining: float | None = Field(
        default=None,
        validation_alias=AliasChoices("days_remaining", "daysRemaining", "runway_days"),
        # Kept so model_dump(by_alias=True) still emits what it always emitted.
        serialization_alias="daysRemaining",
    )
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

    Conversion rate: 100 credits = $1.00 → 1 USD = 100 credits.

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
    """Credit cost estimate for a generation + folding job.

    Tolerant by design. The server's ``GET /api/billing/estimate`` response
    nests richer structure under ``breakdown`` (``params`` and ``rates``
    sub-dicts alongside the per-phase integer totals), so ``breakdown`` is
    typed ``dict[str, Any]`` rather than ``dict[str, int]`` — a stricter type
    raised ``ValidationError`` ("Input should be a valid integer") on the
    nested objects. ``credits`` / ``cost_usd`` default to ``0`` so a partial or
    restructured server payload parses instead of raising; the base
    ``extra="allow"`` config preserves any additional top-level keys as raw.
    """

    # Defaulted (not required) so a richer/partial server dict never raises.
    credits: int = 0
    cost_usd: float = Field(default=0.0, alias="costUsd")
    # dict[str, Any] (not dict[str, int]) — server breakdown carries nested
    # ``params`` / ``rates`` objects, not only per-phase integer totals.
    breakdown: dict[str, Any] | None = None  # {'generation': X, 'folding': Y, 'scoring': Z, 'params': {...}, 'rates': {...}}


class TierLimits(_LGModel):
    tier: str
    max_peptides_per_generation: int | None = Field(default=None, alias="maxPeptidesPerGeneration")
    max_folds_per_target: int | None = Field(default=None, alias="maxFoldsPerTarget")
    max_concurrent_targets: int | None = Field(default=None, alias="maxConcurrentTargets")
    # /api/user-tier-limits emits gpuSlots; retain the canonical SDK wire name.
    max_concurrent_gpu_slots: int | None = Field(
        default=None,
        alias="maxConcurrentGpuSlots",
        validation_alias=AliasChoices("maxConcurrentGpuSlots", "gpuSlots"),
    )
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


class TargetRegion(TypedDict, total=False):
    """Wire shape of one ``target_residues`` / ``avoid_residues`` entry:
    ``{"chain": "A", "start": 36, "end": 46}``, canonical PDB numbering.

    See :class:`ResidueRange` for the full semantics — this ``TypedDict`` is
    for callers who'd rather build the plain-dict shape directly (e.g. from
    JSON) than construct a :class:`ResidueRange`. Both are accepted
    interchangeably by :meth:`~ligandai.resources.peptides.Peptides.generate`.
    """

    chain: str
    start: int
    end: int


class HotspotResidue(TypedDict, total=False):
    """Dict shape accepted for one ``hotspot_residues`` entry:
    ``{"chain": "A", "residue": 792}``.

    The wire format ``hotspot_residues`` sends to the server is a flat list
    of PDB residue numbers (``[792, 855]``) — this dict form is an SDK-side
    convenience when a chain needs to travel alongside the residue for your
    own bookkeeping; only the residue number reaches the server. Same
    PDB-numbering rule as :class:`TargetRegion`: 1-based, never an array
    index [bd-LIGANDAI_ALPHA_V2-x4vt8].
    """

    chain: str
    residue: int


class ResidueRange(_LGModel):
    """An inclusive receptor residue range, in PDB numbering.

    ``start`` and ``end`` are **PDB residue numbers as numbered in the receptor
    structure being designed against** — the uploaded PDB, or the structure the
    platform resolved for the gene. They are NOT indices into any internal
    array and NOT offsets from the start of the sequence. ``chain`` is honoured;
    if your construct is renumbered 1..N, use the construct's own numbering.

    Used by ``Peptides.generate(target_residues=..., avoid_residues=...)``.
    Residue numbers with no counterpart in the receptor's featurized residue set
    are reported as ``unresolved_resnums`` on the generation stream's targeting
    events rather than failing the job.

    Client-side validation (:mod:`ligandai._targeting`) rejects ``start`` /
    ``end`` ``< 1`` with :class:`~ligandai.errors.LigandAIValidationError`
    before any HTTP call — that is the array-index-instead-of-residue-number
    mistake that silently no-op'd an exclusion in production
    [bd-LIGANDAI_ALPHA_V2-x4vt8].
    """

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


class StructureSuitability(_LGModel):
    """The target-suitability verdict ``POST /api/structure/analyze`` produces.

    This is the block the endpoint actually computes. The legacy
    ``pocket*`` / ``vacancy*`` / ``surface*`` fields on
    :class:`StructureAnalysis` are NOT produced here and are null on every
    correct response — the route performs suitability analysis, not pocket
    detection, and says so in its own ``pocket_analysis`` block. Read this
    instead. [bd-dre-rp11j.28, server side bd-dre-rp11j.31]
    """

    is_good_target: bool | None = Field(default=None, alias="isGoodTarget")
    suitability_score: float | None = Field(default=None, alias="suitabilityScore")
    target_type: str | None = Field(default=None, alias="targetType")
    suggested_configuration: str | None = Field(default=None, alias="suggestedConfiguration")
    binding_study_compatibility: dict[str, Any] | None = Field(
        default=None, alias="bindingStudyCompatibility"
    )
    warnings: list[Any] | None = None
    recommendations: list[Any] | None = None
    suggested_actions: list[Any] | None = Field(default=None, alias="suggestedActions")


def _carries_a_value(value: Any) -> bool:
    """Whether ``value`` is evidence that something was COMPUTED.

    ``None`` is absence. An empty list / dict / string is absence too — an
    empty ``warnings`` array is not evidence of an analysis. ``False`` is
    absence *for a flag*, because the only bool in this contract is
    ``computed``, and ``computed=False`` is the server saying it did not.
    But a numeric ``0`` IS a value: ``pocket_count=0`` is a measurement that
    found no pockets, which is an honest empty result and must survive.
    """
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (list, tuple, dict, set, str)):
        return len(value) > 0
    return True


class StructureAnalysis(_LGModel):
    """Result of ``POST /api/structure/analyze``.

    ⛔ AN ALL-NULL BODY IS REJECTED, BY DESIGN. See
    :class:`~ligandai.errors.LigandAIAnalysisNotComputed` for the defect that
    makes this necessary: this endpoint was a no-op for its entire life and
    every layer — status, transport, schema, call site — agreed on the
    non-answer it produced, because every field here except ``gene`` is
    optional and the call site manufactured ``gene`` from its own argument.

    An honest empty result still validates and is still returned: a body
    saying ``pocket_count=0`` / ``pockets=[]``, or carrying a ``suitability``
    verdict, is a measurement. Only a body carrying no evidence that anything
    was computed raises.
    """

    gene: str

    # ---- what the route returns today (bd-dre-rp11j.31) -------------------
    canonical_gene: str | None = Field(default=None, alias="canonicalGene")
    uniprot_id: str | None = Field(default=None, alias="uniprotId")
    analysis_depth: str | None = Field(
        default=None, validation_alias=AliasChoices("analysis_depth", "analysisDepth")
    )
    #: Server-side proof that the two not-computed guards ran and passed. A
    #: caller can assert on this instead of trusting the 200.
    computed: bool | None = None
    computed_fields: list[str] | None = Field(
        default=None, validation_alias=AliasChoices("computed_fields", "computedFields")
    )
    #: Proof the ``analysis_depth`` knob reached the thing that acts on it:
    #: True only when depth="full" AND coordinates were actually obtained.
    structure_used: bool | None = Field(
        default=None, validation_alias=AliasChoices("structure_used", "structureUsed")
    )
    structure_source: str | None = Field(
        default=None, validation_alias=AliasChoices("structure_source", "structureSource")
    )
    structure_note: str | None = Field(
        default=None, validation_alias=AliasChoices("structure_note", "structureNote")
    )
    suitability: StructureSuitability | None = None

    # ---- legacy pocket keys: null on every correct response ---------------
    # Retained because removing them would break older callers, and because
    # `pocket_analysis` now explains IN WORDS why they are null rather than
    # leaving a caller to interpret an unexplained one.
    pocket_count: int | None = Field(default=None, alias="pocketCount")
    recommended_pocket: ResidueRange | None = Field(default=None, alias="recommendedPocket")
    pockets: list[ResidueRange] | None = None
    vacancy_score: float | None = Field(default=None, alias="vacancyScore")
    surface_features: dict[str, Any] | None = Field(default=None, alias="surfaceFeatures")
    pocket_analysis: dict[str, Any] | None = Field(
        default=None, validation_alias=AliasChoices("pocket_analysis", "pocketAnalysis")
    )

    #: Every field inspected for evidence that the platform computed an answer.
    #: `gene` is deliberately NOT in this set: it is echoed from the request,
    #: so it proves only that a request was made.
    EVIDENCE_FIELDS: ClassVar[tuple[str, ...]] = (
        "computed",
        "computed_fields",
        "suitability",
        "pocket_count",
        "recommended_pocket",
        "pockets",
        "vacancy_score",
        "surface_features",
    )

    def evidence_of_computation(self) -> list[str]:
        """Which :attr:`EVIDENCE_FIELDS` carry a value. Empty means nothing ran."""
        found: list[str] = []
        for name in self.EVIDENCE_FIELDS:
            value = getattr(self, name, None)
            if name == "suitability":
                if value is not None and any(
                    _carries_a_value(v) for v in value.model_dump().values()
                ):
                    found.append(name)
                continue
            if _carries_a_value(value):
                found.append(name)
        return found

    @model_validator(mode="after")
    def _refuse_an_uncomputed_analysis(self) -> StructureAnalysis:
        """Raise a NAMED error rather than return an object that means nothing.

        Raising from a validator (instead of returning the object and letting
        the caller check) is deliberate: the previous shape was reachable by
        ``StructureAnalysis.model_validate(anything_with_a_gene)``, and a check
        a caller has to remember to perform is a check that does not happen.
        """
        found = self.evidence_of_computation()
        if found:
            return self
        raise LigandAIAnalysisNotComputed(
            f"POST /api/structure/analyze answered for {self.gene!r} with a body that "
            f"carries no computed analysis: every one of "
            f"{', '.join(self.EVIDENCE_FIELDS)} was absent or empty.\n"
            f"  attempted      : analysing {self.gene!r} for peptide-binder suitability\n"
            f"  what happened  : the platform returned a readable body containing no "
            f"result. This is NOT a finding that {self.gene!r} is unsuitable and NOT an "
            f"empty pocket list — no verdict was reached. The SDK refuses to return it "
            f"as an analysis because an all-null StructureAnalysis is "
            f"indistinguishable from a real one.\n"
            f"  what to do     : retry — whether the analyser reaches a verdict is "
            f"response-dependent per request, so a retry frequently succeeds. If it "
            f"persists, the server is "
            f"older than the fix for this endpoint (it returned 200 + all-null for its "
            f"entire life); upgrade the server or report the gene to support@ligandal.com. "
            f"For pockets/druggability/vacancy use "
            f"/api/receptor-intelligence/profile/{{gene}} — this endpoint has never "
            f"performed pocket detection.",
            subject=self.gene,
            endpoint="POST /api/structure/analyze",
            checked_fields=list(self.EVIDENCE_FIELDS),
            populated_fields=found,
            body=self.model_dump(by_alias=True),
        )


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
    """A group to compare in comparative transcriptomics.

    A group can be one of several kinds (``type``):

    - ``custom``  — a user-uploaded transcriptomics dataset
      (set ``dataset_id`` / ``datasetId`` and optional ``cell_types``).
    - ``gtex``    — a GTEx reference tissue (set ``tissue``).
    - ``geneset`` — an explicit gene / receptor set or pathway
      (set ``genes``), e.g. a BBB transcytosis shuttle set
      ``["TFRC", "LRP1", "FCGRT"]`` or a signalling pathway. Lets you
      compare "BBB-shuttle set vs brain-cortex" or "pathway A vs pathway B"
      in a single :meth:`compare_groups` / :meth:`compare_targets` call.

    ``tissue`` / ``cell_type`` are retained for backward compatibility.

    ⚠ Target-slot rule (live ``compare-groups``): when used as the TARGET
    group, only ``custom`` (all modes; required for ``focus``) and ``geneset``
    (``global``/``compare``) are accepted. ``gtex`` is reference-only in
    ``compare`` mode. Use :class:`ReferenceGroup` for GTEx comparisons.
    [bd-LIGANDAI_ALPHA_V2-tom-D2]
    [bd-LIGANDAI_ALPHA_V2-k5usa]
    """

    name: str
    samples: list[str] = Field(default_factory=list)
    type: Literal["tissue", "cell_type", "custom", "gtex", "geneset"] = "tissue"
    # Group-kind-specific fields (optional; populated per ``type``).
    dataset_id: str | int | None = Field(default=None, alias="datasetId")
    cell_types: list[str] | None = Field(default=None, alias="cellTypes")
    tissue: str | None = None
    genes: list[str] | None = None


class ReferenceGroup(_LGModel):
    """A reference group for comparative transcriptomics. Same shape as
    :class:`TargetGroup` (custom | gtex | geneset). [bd-LIGANDAI_ALPHA_V2-k5usa]
    """

    name: str
    samples: list[str] = Field(default_factory=list)
    type: Literal["tissue", "cell_type", "custom", "gtex", "geneset"] = "tissue"
    dataset_id: str | int | None = Field(default=None, alias="datasetId")
    cell_types: list[str] | None = Field(default=None, alias="cellTypes")
    tissue: str | None = None
    genes: list[str] | None = None


# Measured marker-row key names, in order of preference, used by
# MarkerResponse._gene_and_si. An Ensembl id is the last resort: it is an
# identifier, not the symbol every other TissueMarker field is keyed by.
MARKER_GENE_KEYS = ("gene_name", "Primary_gene", "gene_symbol", "geneSymbol", "symbol",
                    "Ensembl_gene")
MARKER_SI_KEYS = ("Specificity_index", "SI_within_tissue", "specificity_index")


class TissueMarker(_LGModel):
    gene: str
    si: float | None = None
    csi: float | None = None
    fold_change: float | None = Field(default=None, alias="foldChange")
    target_expression: float | None = Field(default=None, alias="targetExpression")
    rank: int | None = None
    receptor: bool | None = None
    # [bd-LIGANDAI_ALPHA_V2-k5usa] comparison provenance
    reference_type: str | None = Field(default=None, alias="referenceType")
    reference_tissues: list[str] | None = Field(default=None, alias="referenceTissues")
    target_tpm: float | None = Field(default=None, alias="targetTpm")
    ref_tpm: float | None = Field(default=None, alias="refTpm")


class MarkerResponse(_LGModel):
    """SI-ranked surface markers returned by
    :meth:`~ligandai.resources.discovery.Discovery.tissue_markers` and
    :meth:`~ligandai.resources.discovery.Discovery.cell_type_markers`.

    The server's ``top-markers`` (GTEx) and ``analyze-fast`` (scRNA / custom
    dataset) endpoints return the ranked rows under a ``markers`` array whose
    gene key is ``gene_name``; the scRNA ``cell-type-markers`` endpoint does the
    same. We normalize that — plus a bare ``top`` array and an empty/partial
    body — into ``top`` of :class:`TissueMarker`, so a caller always reads
    ``response.top`` regardless of which endpoint produced it.
    [bd-LIGANDAI_ALPHA_V2-5p57t]
    """

    top: list[TissueMarker] = Field(default_factory=list)
    total: int | None = None
    metadata: dict[str, Any] | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_markers(cls, data: Any) -> Any:
        """Accept the server's ``markers`` array (gene keyed as ``gene_name``),
        a bare ``top`` array, or an empty body, and surface them all as
        ``top``. Mirrors :meth:`ComparisonResponse._normalize_markers` for the
        ranking (vs comparison) endpoints. [bd-LIGANDAI_ALPHA_V2-5p57t]"""
        if not isinstance(data, dict):
            return data
        d = dict(data)
        raw = d.get("top")
        if not raw:
            raw = d.get("markers")
        if isinstance(raw, list):
            norm = []
            for m in raw:
                if isinstance(m, dict) and "gene" not in m:
                    m = {**m, **cls._gene_and_si(m)}
                norm.append(m)
            d["top"] = norm
        if d.get("total") is None and isinstance(d.get("count"), int):
            d["total"] = d["count"]
        return d

    # Measured row keys, in order of preference. The scRNA cell-type-markers rows
    # carry NO ``gene`` and no ``gene_name``; their symbol is ``Primary_gene``,
    # alongside ``Ensembl_gene``, ``UniProt_accession``, ``Specificity_index`` and
    # the rest of a UniProt-style row (measured live 2026-10-22: AlphaFoldDB,
    # DrugBank, Max, Mean, PDB, Primary_gene, Protein_name, SI_vs_parent,
    # SI_within_tissue, Specificity_index, Transmembrane, UniProt_accession,
    # cell_type_breakdown, method, parent_tissue, tissue_expression). Before this,
    # every such response failed ``top.N.gene Field required`` and the caller got
    # an exception instead of real ranked markers -- the same defect that made
    # tissue_markers unusable. An Ensembl id is a last resort, because it is an
    # identifier and not the symbol every other field in TissueMarker is keyed by.
    # (the key lists live at module scope as MARKER_GENE_KEYS / MARKER_SI_KEYS:
    #  pydantic turns a leading-underscore class attribute into a private attr,
    #  so a tuple declared here is not iterable from a validator.)

    @classmethod
    def _gene_and_si(cls, row: dict[str, Any]) -> dict[str, Any]:
        """The ``gene`` (and ``si``, when absent) a UniProt-style marker row implies."""
        out: dict[str, Any] = {}
        for key in MARKER_GENE_KEYS:
            value = row.get(key)
            if isinstance(value, str) and value.strip():
                out["gene"] = value
                break
        if row.get("si") is None:
            for key in MARKER_SI_KEYS:
                value = row.get(key)
                if isinstance(value, (int, float)):
                    out["si"] = float(value)
                    break
        return out


class ExpressionProfile(_LGModel):
    gene: str
    tissues: dict[str, float] | None = None
    organ_systems: dict[str, float] | list[str] | None = Field(
        default=None, alias="organSystems"
    )
    samples: list[dict[str, Any]] | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_live_shape(cls, data: Any) -> Any:
        """The live ``GET /api/transcriptomics/gene-expression/<gene>`` returns
        ``{gene, chartData: [{tissue, mean, ...}], maxExpression, minExpression}``
        with ``organSystems`` arriving as a plain LIST of system names — not the
        ``{tissues: {...}, organSystems: {...}}`` shape this model documented.
        Tom's GUI testing (SDK discovery asks, 2026-11-05) reproduced it.

        Build ``tissues`` from ``chartData`` rows that carry a real numeric
        mean/value/tpm; rows without one are skipped, never imputed. A
        documented dict ``tissues`` always wins over ``chartData``. Raw
        ``chartData`` stays visible via ``extra=allow``.
        [bd-LIGANDAI_ALPHA_V2-tom-D3]
        """
        if not isinstance(data, dict):
            return data
        if data.get("tissues"):
            return data
        chart = data.get("chartData") or data.get("chart_data")
        if not isinstance(chart, list):
            return data
        tissues: dict[str, float] = {}
        for row in chart:
            if not isinstance(row, dict):
                continue
            name = row.get("tissue") or row.get("name") or row.get("tissueName")
            if not isinstance(name, str) or not name.strip():
                continue
            val = row.get("mean")
            if val is None:
                val = row.get("value")
            if val is None:
                val = row.get("tpm")
            if val is None:
                continue
            try:
                tissues[name.strip()] = float(val)
            except (TypeError, ValueError):
                continue  # not a real number — skip, do not fabricate
        if not tissues:
            return data
        d = dict(data)
        d["tissues"] = tissues
        return d


class ComparisonResponse(_LGModel):
    """Result of :meth:`compare_groups` / :meth:`compare_targets`.

    The server returns ``markers`` (not ``results``); we normalize both into
    ``results`` so callers have a single field. ``shared_genes`` /
    ``differential_genes`` are populated by :meth:`compare_targets`.
    [bd-LIGANDAI_ALPHA_V2-k5usa]
    """

    target_group: str = Field(default="", alias="targetGroup")
    reference_groups: list[str] = Field(default_factory=list, alias="referenceGroups")
    mode: str = "compare"
    results: list[TissueMarker] = Field(default_factory=list)
    method: str | None = None
    metadata: dict[str, Any] | None = None
    success: bool | None = None
    # Populated by compare_targets (client-side shared/differential split).
    shared_genes: list[str] | None = Field(default=None, alias="sharedGenes")
    differential_genes: list[str] | None = Field(default=None, alias="differentialGenes")

    @model_validator(mode="before")
    @classmethod
    def _normalize_markers(cls, data: Any) -> Any:
        """Accept the server's ``markers`` array (whose entries key the gene as
        ``gene_name``) and surface it as ``results`` of :class:`TissueMarker`."""
        if not isinstance(data, dict):
            return data
        d = dict(data)
        raw = d.get("results")
        if not raw:
            raw = d.get("markers")
        if isinstance(raw, list):
            norm = []
            for m in raw:
                if isinstance(m, dict) and "gene" not in m and "gene_name" in m:
                    m = {**m, "gene": m["gene_name"]}
                norm.append(m)
            d["results"] = norm
        return d


class GeoDataset(_LGModel):
    accession: str = Field(min_length=1, pattern=r"^GSE\d+$")
    title: str | None = None
    organism: str | None = None
    sample_count: int | None = Field(default=None, alias="sampleCount")
    summary: str | None = None
    provenance: Any = None


class GeoSearchResponse(_LGModel):
    """Public GEO search envelope, including warnings and additive source metadata."""

    results: list[GeoDataset]
    success: bool | None = None
    query: str | None = None
    warnings: list[str] = Field(default_factory=list)


class GeoInspectionFile(_LGModel):
    name: str
    url: str
    format: str
    supported: bool
    container: bool
    sample_id: str | None = Field(default=None, alias="sampleId")
    source_allowed: bool | None = Field(default=None, alias="sourceAllowed")


class GeoInspectionSample(_LGModel):
    id: str
    title: str
    source: str
    characteristics: list[str]
    fields: dict[str, list[str]]
    files: list[GeoInspectionFile]


class GeoInspectionSource(_LGModel):
    repository: Literal["GEO"]
    url: str
    metadata_sha256: str = Field(alias="metadataSha256", pattern=r"^[0-9a-f]{64}$")
    metadata_completeness: str = Field(alias="metadataCompleteness")
    retrieved_at: datetime | None = Field(alias="retrievedAt")


class GeoFieldCoverage(_LGModel):
    samples_with_value: int = Field(alias="samplesWithValue", ge=0)
    distinct_values: list[str] = Field(alias="distinctValues")


class GeoMetadataCoverage(_LGModel):
    samples_resolved: int = Field(alias="samplesResolved", ge=0)
    declared_samples: int = Field(alias="declaredSamples", ge=0)
    fields: dict[str, GeoFieldCoverage]


class GeoParserInspection(_LGModel):
    status: Literal["inspection_required"]
    formats: list[str]
    requires_selection: bool = Field(alias="requiresSelection")
    expression_unit: Literal["unknown"] = Field(alias="expressionUnit")


class GeoInspection(_LGModel):
    """Official metadata and parser readiness; inspection does not download expression data.

    Sample annotations and parser coverage are preserved verbatim. Unknown
    expression units and cohort labels are not inferred by the SDK.
    """

    inspection_schema: Literal["transcriptomic-dataset-inspection/1"] = Field(alias="schema")
    accession: str = Field(min_length=1, pattern=r"^GSE\d+$")
    title: str
    source: GeoInspectionSource
    organism: str
    sample_count: int = Field(alias="sampleCount", ge=0)
    samples: list[GeoInspectionSample]
    files: list[GeoInspectionFile]
    metadata_coverage: GeoMetadataCoverage = Field(alias="metadataCoverage")
    parser: GeoParserInspection
    warnings: list[str]
    downloaded: Literal[False]


class GeoImportJob(_LGModel):
    job_id: str = Field(alias="jobId", min_length=1)
    accession: str = Field(min_length=1, pattern=r"^GSE\d+$")
    status: str = Field(min_length=1)
    progress: float | None = None
    dataset_id: str | int | None = Field(default=None, alias="datasetId")
    metadata: dict[str, Any] | None = None
    provenance: Any = None
    result_summary: dict[str, Any] | None = Field(default=None, alias="resultSummary")
    error: str | None = None
    started_at: datetime | None = Field(default=None, alias="startedAt")
    completed_at: datetime | None = Field(default=None, alias="completedAt")


class Dataset(_LGModel):
    """A transcriptomics dataset, from either of the two envelopes that produce one.

    ⛔ TWO SHAPES, AND THE UPLOAD ONE USED TO RAISE. Measured 2026-10-22 against the
    live account: ``POST /api/transcriptomics/upload`` answers ``202`` with
    ``{datasetId, status, estimatedTime, fileName, datasetType}`` — no ``id``, no
    ``name``, no ``type``. So ``upload_dataset`` performed the write (the server
    created dataset 52 and queued it for processing) and THEN raised
    ``ValidationError`` on 3 missing fields. A caller that retried on that error
    would upload the same file again, which is the write-then-raise shape this
    SDK has hit before. ``GET /api/transcriptomics/datasets`` answers the
    ``{id, name, type}`` shape, which is why only the upload path was affected.

    ``name`` and ``type`` are therefore optional and populated from whichever key
    the envelope carries. ``status`` / ``estimated_time`` are the 202's own fields:
    an upload is ACCEPTED, not finished, and the caller needs them to drive
    :meth:`~ligandai.resources.discovery.Discovery.wait_for_import`.
    """

    id: str | int
    name: str | None = None
    type: str | None = None
    status: str | None = None
    estimated_time: str | None = Field(default=None, alias="estimatedTime")
    sample_count: int | None = Field(default=None, alias="sampleCount")
    cell_count: int | None = Field(default=None, alias="cellCount")
    uploaded_at: datetime | None = Field(default=None, alias="uploadedAt")

    # Each tuple is (target field, the keys that may carry it) — the upload
    # envelope's key first where the two disagree, since that is the shape that
    # was failing. Alias= is not enough: pydantic allows ONE alias per field.
    _ENVELOPE_KEYS: ClassVar[tuple[tuple[str, tuple[str, ...]], ...]] = (
        ("id", ("id", "datasetId", "dataset_id")),
        ("name", ("name", "fileName", "file_name", "sourceFile")),
        ("type", ("type", "datasetType", "dataset_type")),
    )

    @model_validator(mode="before")
    @classmethod
    def _accept_either_envelope(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        out = dict(data)
        for field, keys in cls._ENVELOPE_KEYS:
            if out.get(field) is None:
                for key in keys:
                    if out.get(key) is not None:
                        out[field] = out[key]
                        break
        return out


# -- Signed H-SWAE capability boundary ---------------------------------------


HSWAE_CAPABILITY_CONTRACT_VERSION = "1.0.0"
HSWAE_ANNOTATION_PUBLIC_REFERENCE = "HSWAE-2 cell_type_global_specificity (HPA v25 surfaceome)"

HswaeCapabilityOperation: TypeAlias = Literal[
    "hswae.v1.semantic_search",
    "hswae.v1.surface_markers",
    "hswae.v2.resolve_cell_type",
    "hswae.v2.compare_cell_types",
    "hswae.v2.isoform_expression",
    "hswae.v2.annotate_dataset",
    "predictive_interactomics.complex_specificity",
]

# SHA-256 over each reviewed 1.0.0 definition after recursively sorting JSON
# object keys.  The server does not carry a separate definition-version field,
# so accepting metadata or schema drift under the same contractVersion would
# silently change the public SDK contract.
HSWAE_CAPABILITY_DEFINITION_HASHES: Mapping[HswaeCapabilityOperation, str] = MappingProxyType(
    {
        "hswae.v1.semantic_search": (
            "059b7dabfe64b95df6db302e19c41db4140b21eeb17c1fee1d601942ab1e4530"
        ),
        "hswae.v1.surface_markers": (
            "4f19e07593de43bc4daecf02229c7033e35248cf639b4fb9d96ddc2b6d8dadc9"
        ),
        "hswae.v2.resolve_cell_type": (
            "78ff3122da897fab5d1e49ec2a0f01cd8e90e76e9c0ec8c6ca35d88c03b8ca40"
        ),
        "hswae.v2.compare_cell_types": (
            "cd752bf2c9284510cbabbbcd89f8be4be2cf0817c0f676aa73f12e53f0e90881"
        ),
        "hswae.v2.isoform_expression": (
            "1716acbd629ea944bebd9fc33df97a1f721211e8776bacfec455b38df0164ad2"
        ),
        "hswae.v2.annotate_dataset": (
            "53ba38fff81386bc01c6b98d24e17237c9e7156cf546654efe317acc273b43af"
        ),
        "predictive_interactomics.complex_specificity": (
            "26938db63750694e25c0ba1b6583a3dc90354f24441072e2f5d64ea3a88b0a06"
        ),
    }
)

HswaeJsonSchemaType: TypeAlias = Literal[
    "object",
    "array",
    "string",
    "integer",
    "number",
    "boolean",
    "null",
]
HswaeJsonSchemaScalar: TypeAlias = str | int | float | bool | None

_HswaePublicString: TypeAlias = Annotated[str, Field(min_length=1, max_length=2_000)]
_HswaeNullablePublicString: TypeAlias = Annotated[str, Field(max_length=2_000)] | None
_HswaeGene: TypeAlias = Annotated[str, Field(min_length=1, max_length=100)]
#: One normalized query/canonical token in the resolver's provenance lists.
_HswaeQueryToken: TypeAlias = Annotated[str, Field(min_length=1, max_length=200)]
#: A free-text explanatory note. Longer than a label: the platform puts whole
#: "this empty list is a filter artefact, not an absence" paragraphs in these.
_HswaeNullableNote: TypeAlias = Annotated[str, Field(max_length=10_000)] | None

_HSWAE_COMPLEX_SUBUNIT_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9._-]*$")
_HSWAE_COMPLEX_URI_SCHEME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_HSWAE_COMPLEX_INTERNAL_LABELS = (
    "cell_context_expression",
    "cell_type_expression_agg",
    "gene_context_expression",
    "transcript_context_expression",
    "sqlite_master",
    "source_dataset",
    "internal_db_path",
    "database_url",
    ".sqlite",
    ".sqlite3",
    ".db",
)


def _validate_hswae_complex_public_label(value: str, *, allow_empty: bool = False) -> str:
    normalized = value.strip()
    lowered = normalized.lower()
    if (
        value != normalized
        or (not allow_empty and not normalized)
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
        or "/" in normalized
        or "\\" in normalized
        or ".." in normalized
        or normalized.startswith("~")
        or _HSWAE_COMPLEX_URI_SCHEME_PATTERN.match(normalized) is not None
        or any(encoded in lowered for encoded in ("%25", "%2e", "%2f", "%5c"))
        or any(identifier in lowered for identifier in _HSWAE_COMPLEX_INTERNAL_LABELS)
    ):
        raise ValueError("complex public label is invalid")
    return value


class _HswaeContractModel(BaseModel):
    """Type discipline shared by every H-SWAE capability-contract model.

    Declared fields are strictly typed, non-finite floats are rejected, and a
    parsed object is immutable. This class sets NO ``extra`` policy — that is
    the single decision made by the two subclasses below, and nothing else in
    this module sets ``model_config`` on an H-SWAE model. One rule, one place:
    the defect this replaced existed because the same rule had three
    implementations (server TypeScript, capability boundary, this SDK).
    """

    model_config = ConfigDict(
        strict=True,
        allow_inf_nan=False,
        frozen=True,
    )


class _HswaeClosedModel(_HswaeContractModel):
    """Closed model for the parts of the contract the SDK itself asserts.

    Used for CALLER INPUT — a caller's typo must fail before HTTP rather than
    travel the wire and be silently dropped server-side — and for the
    hash-pinned signed registry and its JSON-schema subset, where an unknown
    key changes what the advertised contract MEANS (an unenforced keyword the
    SDK quietly ignored would make it report a validation guarantee it is not
    applying), so drift must fail.

    ⛔ NOT for server response bodies. See :class:`_HswaeOpenModel`.
    """

    model_config = ConfigDict(extra="forbid")


class _HswaeOpenModel(_HswaeContractModel):
    """Server-response model for the capability surface.

    Identical type discipline to :class:`_HswaeClosedModel` — strict types on
    every declared field, no non-finite floats, frozen — but a field the
    server sent and this module does not declare is KEPT (reachable through
    ``model_extra`` and preserved by ``model_dump``) instead of raising.

    ⛔ ``extra="forbid"`` here was a live regression. The platform added
    resolution/ranking bookkeeping (bd-dre-pjhrp.10, bd-dre-pjhrp.11) and
    every H-SWAE capability call through this SDK then raised
    ``ValidationError`` against well-formed production payloads — measured
    2026-10-15: 57 errors on ``hswae.v1.surface_markers``, 47 on
    ``hswae.v2.compare_cell_types``, 15 on ``hswae.v2.resolve_cell_type``, 2
    on ``hswae.v1.semantic_search``, every one of them ``extra_forbidden``,
    with zero type errors and zero missing fields. Refusing a field the
    server added protects nothing — the bytes already arrived in this
    process; it only denies the caller their result. A wrong type on a
    declared field, a missing required field, and a non-object where a model
    belongs all still raise. [bd-dre-pjhrp.17]
    """

    model_config = ConfigDict(extra="allow")


class HswaeJsonSchemaNode(_HswaeClosedModel):
    """Closed recursive JSON-schema subset returned by capability discovery."""

    type: HswaeJsonSchemaType | list[HswaeJsonSchemaType]
    additional_properties: bool | None = Field(default=None, alias="additionalProperties")
    properties: dict[str, HswaeJsonSchemaNode] | None = Field(default=None, max_length=100)
    required: list[Annotated[str, Field(min_length=1, max_length=200)]] | None = Field(
        default=None,
        max_length=100,
    )
    any_of: list[HswaeJsonSchemaNode] | None = Field(
        default=None,
        alias="anyOf",
        max_length=20,
    )
    items: HswaeJsonSchemaNode | None = None
    enum: list[HswaeJsonSchemaScalar] | None = Field(default=None, max_length=100)
    default: HswaeJsonSchemaScalar = None
    minimum: float | None = None
    maximum: float | None = None
    min_length: int | None = Field(default=None, alias="minLength", ge=0, le=1_000_000)
    max_length: int | None = Field(default=None, alias="maxLength", ge=0, le=1_000_000)
    min_items: int | None = Field(default=None, alias="minItems", ge=0, le=1_000_000)
    max_items: int | None = Field(default=None, alias="maxItems", ge=0, le=1_000_000)
    unique_items: bool | None = Field(default=None, alias="uniqueItems")
    pattern: Annotated[str, Field(max_length=2_000)] | None = None
    description: Annotated[str, Field(max_length=10_000)] | None = None

    @field_validator("type")
    @classmethod
    def _validate_type_list(
        cls,
        value: HswaeJsonSchemaType | list[HswaeJsonSchemaType],
    ) -> HswaeJsonSchemaType | list[HswaeJsonSchemaType]:
        if isinstance(value, list) and (
            not value or len(value) > 7 or len(set(value)) != len(value)
        ):
            raise ValueError("schema type list must be non-empty and unique")
        return value

    @field_validator("properties")
    @classmethod
    def _validate_property_names(
        cls,
        value: dict[str, HswaeJsonSchemaNode] | None,
    ) -> dict[str, HswaeJsonSchemaNode] | None:
        if value is not None and any(not key or len(key) > 200 for key in value):
            raise ValueError("schema property name is invalid")
        return value

    @model_validator(mode="after")
    def _validate_ranges(self) -> HswaeJsonSchemaNode:
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("schema numeric bounds are inverted")
        if (
            self.min_length is not None
            and self.max_length is not None
            and self.min_length > self.max_length
        ):
            raise ValueError("schema string bounds are inverted")
        if (
            self.min_items is not None
            and self.max_items is not None
            and self.min_items > self.max_items
        ):
            raise ValueError("schema array bounds are inverted")
        return self


class HswaeJsonObjectSchema(HswaeJsonSchemaNode):
    """Closed object-schema root required for advertised inputs and outputs."""

    type: Literal["object"]
    additional_properties: Literal[False] = Field(alias="additionalProperties")
    properties: dict[str, HswaeJsonSchemaNode] = Field(max_length=100)


class HswaeCapabilityDefinition(_HswaeClosedModel):
    operation: HswaeCapabilityOperation
    title: Annotated[str, Field(min_length=1, max_length=500)]
    description: Annotated[str, Field(min_length=1, max_length=10_000)]
    domain: Literal["transcriptomics", "predictive_interactomics"]
    effect: Literal["read_local"]
    execution_mode: Literal["immediate"] = Field(alias="executionMode")
    required_entitlements: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        alias="requiredEntitlements",
        min_length=1,
        max_length=20,
    )
    required_approval_scopes: list[
        Literal[
            "geo.download",
            "dataset.ingest",
            "hswae.finetune",
            "artifact.register",
        ]
    ] = Field(alias="requiredApprovalScopes", max_length=0)
    input_schema: HswaeJsonObjectSchema = Field(alias="inputSchema")
    output_schema: HswaeJsonObjectSchema = Field(alias="outputSchema")
    backend_family: Literal["hswae_v1", "hswae_v2"] = Field(alias="backendFamily")

    @model_validator(mode="before")
    @classmethod
    def _pin_reviewed_definition(cls, value: Any) -> Any:
        if type(value) is not dict:
            raise ValueError("capability definition must be a plain object")
        operation = value.get("operation")
        expected = (
            HSWAE_CAPABILITY_DEFINITION_HASHES.get(cast(HswaeCapabilityOperation, operation))
            if isinstance(operation, str)
            else None
        )
        if expected is None:
            raise ValueError("capability definition operation is unsupported")
        try:
            canonical = json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("capability definition is not canonical JSON") from exc
        if hashlib.sha256(canonical).hexdigest() != expected:
            raise ValueError("capability definition digest mismatch")
        return value


class HswaeCapabilityDiscovery(_HswaeClosedModel):
    contract_version: Literal["1.0.0"] = Field(alias="contractVersion")
    capabilities: list[HswaeCapabilityDefinition] = Field(max_length=7)

    @field_validator("capabilities")
    @classmethod
    def _unique_operations(
        cls,
        value: list[HswaeCapabilityDefinition],
    ) -> list[HswaeCapabilityDefinition]:
        operations = [entry.operation for entry in value]
        if len(operations) != len(set(operations)):
            raise ValueError("capability operations must be unique")
        return value


class HswaeSemanticSearchInput(TypedDict):
    query: str
    k: NotRequired[int]


class HswaeSurfaceMarkersInput(TypedDict):
    context: str
    top_n: NotRequired[int]
    exclude_contexts: NotRequired[list[str]]


class HswaeResolveCellTypeInput(TypedDict):
    query: str
    marker_genes: NotRequired[list[str]]
    top_k: NotRequired[int]


class HswaeCompareCellTypesInput(TypedDict):
    target_cell_type: str
    reference_cell_types: NotRequired[list[str]]
    comparator_scope: NotRequired[
        Literal[
            "topical",
            "tissue_resident",
            "systemic",
            "cluster_only",
        ]
    ]
    delivery_context: NotRequired[str]
    top_n: NotRequired[int]


class HswaeIsoformExpressionInput(TypedDict):
    gene: str
    top_n_isoforms: NotRequired[int]
    min_mean_tpm: NotRequired[float]


class HswaeAnnotateDatasetInput(TypedDict):
    dataset_ref: str
    top_n: NotRequired[int]


class HswaeComplexSpecificityInput(TypedDict):
    subunits: list[str]
    complex_name: NotRequired[str]
    min_subunit_tpm: NotRequired[float]
    limit: NotRequired[int]


class _HswaeSemanticSearchInput(_HswaeClosedModel):
    query: Annotated[str, Field(min_length=1, max_length=2_000)]
    k: int | None = Field(default=None, ge=1, le=50)


class _HswaeSurfaceMarkersInput(_HswaeClosedModel):
    context: Annotated[str, Field(min_length=1, max_length=2_000)]
    top_n: int | None = Field(default=None, ge=1, le=500)
    exclude_contexts: list[Annotated[str, Field(max_length=2_000)]] | None = Field(
        default=None,
        max_length=100,
    )


class _HswaeResolveCellTypeInput(_HswaeClosedModel):
    query: Annotated[str, Field(min_length=1, max_length=2_000)]
    marker_genes: list[Annotated[str, Field(max_length=100)]] | None = Field(
        default=None,
        max_length=500,
    )
    top_k: int | None = Field(default=None, ge=1, le=20)


class _HswaeCompareCellTypesInput(_HswaeClosedModel):
    target_cell_type: Annotated[str, Field(min_length=1, max_length=2_000)]
    reference_cell_types: list[Annotated[str, Field(max_length=2_000)]] | None = Field(
        default=None,
        max_length=200,
    )
    comparator_scope: (
        Literal[
            "topical",
            "tissue_resident",
            "systemic",
            "cluster_only",
        ]
        | None
    ) = None
    delivery_context: Annotated[str, Field(max_length=500)] | None = None
    top_n: int | None = Field(default=None, ge=1, le=200)


class _HswaeIsoformExpressionInput(_HswaeClosedModel):
    gene: Annotated[str, Field(min_length=1, max_length=64)]
    top_n_isoforms: int | None = Field(default=None, ge=1, le=50)
    min_mean_tpm: float | None = Field(default=None, ge=0)


class _HswaeAnnotateDatasetInput(_HswaeClosedModel):
    dataset_ref: Annotated[str, Field(min_length=1, max_length=2_000)]
    top_n: int | None = Field(default=None, ge=1, le=10)


class _HswaeComplexSpecificityInput(_HswaeClosedModel):
    subunits: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(
        min_length=2,
        max_length=32,
    )
    complex_name: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    min_subunit_tpm: float | None = Field(default=None, ge=0)
    limit: int | None = Field(default=None, ge=1, le=200)

    @field_validator("complex_name")
    @classmethod
    def _validate_complex_name(cls, value: str | None) -> str | None:
        if value is not None and (
            not value.strip()
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError("complex name is invalid")
        return value

    @field_validator("subunits")
    @classmethod
    def _normalize_unique_subunits(cls, value: list[str]) -> list[str]:
        normalized = [subunit.strip().upper() for subunit in value]
        if (
            any(_HSWAE_COMPLEX_SUBUNIT_PATTERN.fullmatch(subunit) is None for subunit in normalized)
            or len(normalized) != len(set(normalized))
        ):
            raise ValueError("complex subunits must be unique after normalization")
        return normalized


_HSWAE_INPUT_MODELS: dict[HswaeCapabilityOperation, type[_HswaeClosedModel]] = {
    "hswae.v1.semantic_search": _HswaeSemanticSearchInput,
    "hswae.v1.surface_markers": _HswaeSurfaceMarkersInput,
    "hswae.v2.resolve_cell_type": _HswaeResolveCellTypeInput,
    "hswae.v2.compare_cell_types": _HswaeCompareCellTypesInput,
    "hswae.v2.isoform_expression": _HswaeIsoformExpressionInput,
    "hswae.v2.annotate_dataset": _HswaeAnnotateDatasetInput,
    "predictive_interactomics.complex_specificity": _HswaeComplexSpecificityInput,
}


def validate_hswae_capability_input(
    operation: HswaeCapabilityOperation,
    input_data: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate and copy one exact signed capability input before HTTP."""

    model_type = _HSWAE_INPUT_MODELS.get(operation)
    if model_type is None:
        raise ValueError("unsupported H-SWAE capability operation")
    validated = model_type.model_validate(input_data)
    return cast(dict[str, Any], validated.model_dump(mode="json", exclude_none=True))


class HswaeSemanticMatch(_HswaeOpenModel):
    tissue: _HswaePublicString
    score: float = Field(ge=-1, le=1)
    match_type: Literal["exact", "alias", "cell_to_tissue", "fuzzy", "embedding"]
    source_entity: _HswaePublicString
    cell_type: _HswaeNullablePublicString


class HswaeSearchedVocabulary(_HswaeOpenModel):
    """One vocabulary the search actually looked in, and its size.

    An empty ``matches`` list is absence FROM THESE VOCABULARIES, not absence
    from biology — this is the field that makes the two distinguishable.
    """

    name: _HswaePublicString
    size: int | None = Field(default=None, ge=0)


class HswaeSemanticSearchResult(_HswaeOpenModel):
    query: _HswaePublicString
    matches: list[HswaeSemanticMatch] = Field(max_length=50)
    count: int = Field(ge=0, le=50)
    #: Why nothing matched, when ``matches`` is empty. [bd-dre-pjhrp.17]
    unmatched_reason: _HswaeNullablePublicString = None
    searched_vocabularies: list[HswaeSearchedVocabulary] | None = Field(
        default=None,
        max_length=20,
    )


class HswaeSurfaceMarker(_HswaeOpenModel):
    gene: _HswaeGene
    target_context: _HswaePublicString = Field(alias="targetContext")
    method: Annotated[str, Field(min_length=1, max_length=200)]
    similarity: float | None = Field(ge=-1, le=1)
    mean_tpm: float | None = Field(alias="meanTpm", ge=0)
    specificity_index: float | None = Field(alias="specificityIndex", ge=0)
    rank: int | None = Field(ge=1)
    ensembl_gene: _HswaeNullablePublicString = Field(alias="ensemblGene")
    uniprot_id: _HswaeNullablePublicString = Field(alias="uniprotId")
    protein_name: _HswaeNullablePublicString = Field(alias="proteinName")
    #: Embedding-space scores, reported alongside ``specificity_index`` so the
    #: ranked list can be re-derived from the key that actually sorted it.
    #: [bd-dre-pjhrp.10]
    embedding_specificity: float | None = Field(default=None, alias="embeddingSpecificity")
    embedding_combined_score: float | None = Field(default=None, alias="embeddingCombinedScore")
    #: Fraction of samples in which the marker clears the floor.
    consistency: float | None = None


class HswaeSurfaceMarkersResult(_HswaeOpenModel):
    """SI-ranked surface markers, with the ranking and filter accounting.

    ⛔ A list shorter than ``markers_requested`` is a FILTER result, not a
    measured absence: read ``markers_eligible`` against the three
    ``markers_*_excluded`` counts and ``floor_note`` before concluding a gene
    is not expressed. ``ranking_key`` names the field the list is sorted by —
    do not assume it. [bd-dre-pjhrp.10]
    """

    query: _HswaePublicString
    matched_tissues: list[HswaeSemanticMatch] = Field(max_length=5)
    primary_tissue: _HswaePublicString
    markers: list[HswaeSurfaceMarker] = Field(max_length=500)
    timing_ms: float = Field(ge=0)
    method: Literal["semantic_search_markers"]
    #: The marker-selection backend behind this list (e.g. ``hswae_differential``).
    marker_method: _HswaeNullablePublicString = None
    #: The field ``markers`` is ordered by, and the raw sort key behind it.
    ranking_key: _HswaeNullablePublicString = None
    sort_key_field: _HswaeNullablePublicString = None
    #: Expression floor applied, and the tissue its TPM was taken in.
    #:
    #: ⛔ ``expression_floor_tpm`` is NOT applied to a marker's ``mean_tpm``. It
    #: is evaluated on the basis named by ``expression_floor_basis`` — for a
    #: context resolving to more than one tissue, max-across-tissues — so a
    #: marker can correctly report a ``mean_tpm`` BELOW this floor. Measured:
    #: context ``colon`` resolves to colon_sigmoid + colon_transverse, and
    #: MRGPRD clears a 1.0 floor on its max of 1.38 while reporting mean 0.97.
    #: Read ``expression_floor_basis`` before calling such a row a filter bug.
    expression_floor_tpm: float | None = Field(default=None, ge=0)
    expression_floor_tissue: _HswaeNullablePublicString = None
    #: What the floor is evaluated ON (e.g. ``max_tpm_across_target_tissues``) —
    #: the field that reconciles a sub-floor ``mean_tpm`` with the stated floor.
    #:
    #: The backend computed this from the beginning and the capability boundary
    #: projected it out, so the receipt contradicted itself with nothing in it
    #: to explain why [bd-dre-pjhrp.19]. Live as of 2026-10-15 (server commit
    #: 90aeca34ff) and confirmed against production, where ``colon`` reports
    #: ``max_tpm_across_target_tissues``. Optional so a deployment older than
    #: that fix still parses, reading ``None`` rather than raising.
    expression_floor_basis: _HswaeNullablePublicString = None
    #: Truncation / exclusion accounting. ``markers_eligible`` is the candidate
    #: pool before ``top_n`` truncation.
    markers_eligible: int | None = Field(default=None, ge=0)
    markers_requested: int | None = Field(default=None, ge=0)
    markers_returned: int | None = Field(default=None, ge=0)
    markers_below_floor_excluded: int | None = Field(default=None, ge=0)
    markers_unknown_expression_excluded: int | None = Field(default=None, ge=0)
    markers_below_consistency_excluded: int | None = Field(default=None, ge=0)
    floor_note: _HswaeNullableNote = None


class HswaeSupportingGeneEvidence(_HswaeOpenModel):
    gene: _HswaeGene
    csi: float
    rank_for_gene: int = Field(alias="rankForGene", ge=1)


class HswaeMatchDetail(_HswaeOpenModel):
    """What the resolver stage actually measured to produce a candidate.

    Every member is optional because WHICH ones appear is determined by the
    stage named in :attr:`HswaeResolveCellTypeCandidate.match_type`: ``{}`` for
    the stages that measure nothing, ``matched_term`` for ``ontology_synonym``,
    the Jaccard/containment group for ``token_set``, and ``ratio`` for
    ``fuzzy``. The backend emits only the members its stage computed —
    filling the others with nulls would invent precision never measured — so
    ``None`` here means NOT MEASURED, not "measured as zero".

    ⛔ This model SERIALISES back to exactly the members it received, which is
    the one place in these types that departs from "every declared field
    appears in ``model_dump``". The signed schema permits a real ``null`` for
    each member, so a materialised ``None`` would be indistinguishable from a
    measured null and would state that a stage reported a number it never
    computed. A receipt is an audit object; inventing that is worse than
    omitting it, and the backend omits for the same reason.

    Live as of 2026-10-15, server commit 90aeca34ff [bd-dre-pjhrp.19]. The
    boundary had been dropping it because a union of four stage shapes was
    awkward to declare, which is not a reason to withhold a measurement.

    Inherits ``extra="allow"``, so a fifth stage shape added later is kept
    rather than refused — the member set is deliberately NOT closed on this
    side, even though the backend's is, because a closed member set is the
    defect this whole release exists to undo.
    """

    matched_term: _HswaeNullablePublicString = None
    jaccard: float | None = None
    containment_canonical: float | None = None
    containment_query: float | None = None
    ratio: float | None = None
    shared_tokens: list[_HswaeQueryToken] | None = Field(default=None, max_length=50)

    @model_serializer(mode="wrap")
    def _omit_members_the_stage_never_measured(
        self,
        handler: SerializerFunctionWrapHandler,
    ) -> dict[str, Any]:
        """Project back the member set that arrived, and nothing else.

        Declared members that were never sent are dropped; members that WERE
        sent are kept even when their value is ``None`` (a measured null), and
        anything undeclared is kept because this model is ``extra="allow"``.
        """
        serialized = handler(self)
        declared = type(self).model_fields
        return {
            key: value
            for key, value in serialized.items()
            if key not in declared or key in self.model_fields_set
        }


class HswaeResolveCellTypeCandidate(_HswaeOpenModel):
    cell_type: _HswaePublicString
    confidence: float = Field(ge=0, le=1)
    top_tissue: _HswaeNullablePublicString
    top_organ_system: _HswaeNullablePublicString
    source: _HswaePublicString
    proxy_for: _HswaeNullablePublicString
    proxy_note: _HswaeNullablePublicString
    alias_query: _HswaeNullablePublicString
    alias_marker_genes: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(
        max_length=500,
    )
    requires_geo_ingest: list[Annotated[str, Field(min_length=1, max_length=2_000)]] = Field(
        max_length=50,
    )
    supporting_genes: list[HswaeSupportingGeneEvidence] = Field(
        alias="supportingGenes",
        max_length=10,
    )
    vote_count: int | None = Field(ge=0)
    marker_score: float | None
    #: Per-candidate resolution provenance [bd-dre-pjhrp.11]. ``match_type``
    #: is the resolver stage that produced this candidate
    #: (``exact`` / ``substring`` / ``ontology_synonym`` / ``token_set`` /
    #: ``fuzzy`` / ``specialized_alias`` / ``marker_overlap``). Deliberately a
    #: plain string, not a ``Literal``: a stage added later must not become a
    #: hard parse failure, which is the defect this field shipped with.
    match_type: _HswaeNullablePublicString = None
    #: True when the canonical cell type is a PROXY for the query, not the
    #: query itself — read ``proxy_note`` before treating it as the same cell.
    resolution_is_proxy: bool | None = None
    #: Tokens of the query this match did not account for, and the qualifier
    #: subset of them that nothing in the atlas corroborates. A non-empty
    #: ``uncorroborated_qualifiers`` means the match is broader than asked.
    dropped_query_tokens: list[_HswaeQueryToken] | None = Field(default=None, max_length=50)
    uncorroborated_qualifiers: list[_HswaeQueryToken] | None = Field(default=None, max_length=50)
    #: The OTHER half of the qualifier evidence: dropped tokens the atlas does
    #: corroborate. Reading ``uncorroborated_qualifiers`` without this one
    #: makes a well-corroborated match look as doubtful as an unsupported one.
    #: Live as of 2026-10-15, server commit 90aeca34ff [bd-dre-pjhrp.19];
    #: optional so an older deployment still parses.
    corroborated_qualifiers: list[_HswaeQueryToken] | None = Field(default=None, max_length=50)
    #: What the resolver stage measured; see :class:`HswaeMatchDetail`. ``None``
    #: means the stage reported nothing, not that the match was unmeasured.
    match_detail: HswaeMatchDetail | None = None
    #: Tokens the canonical label adds beyond the query, and specializations
    #: the caller did not ask for.
    added_canonical_tokens: list[_HswaeQueryToken] | None = Field(default=None, max_length=50)
    unrequested_specializations: list[_HswaeQueryToken] | None = Field(
        default=None,
        max_length=50,
    )


class HswaeCellTypeAlternative(_HswaeOpenModel):
    """A competing canonical cell type an ambiguous query also matched."""

    cell_type: _HswaePublicString
    score: float
    match_type: _HswaeNullablePublicString = None


class HswaeNearestCandidate(_HswaeOpenModel):
    """A nearest atlas label, emitted even when nothing resolved.

    ``accepted`` is False for the rows that explain an UNRESOLVED query —
    they are why-not evidence, not results. [bd-dre-pjhrp.11]
    """

    cell_type: _HswaePublicString
    score: float
    match_type: _HswaeNullablePublicString = None
    accepted: bool | None = None
    shared_tokens: list[_HswaeQueryToken] | None = Field(default=None, max_length=50)
    dropped_query_tokens: list[_HswaeQueryToken] | None = Field(default=None, max_length=50)


class HswaeResolutionSynonymTables(_HswaeOpenModel):
    """Sizes of the synonym tables the resolver consulted."""

    specialized_alias_keys: int | None = Field(default=None, ge=0)
    specialized_alias_entries: int | None = Field(default=None, ge=0)
    ontology_synonym_keys: int | None = Field(default=None, ge=0)
    ontology_synonym_entries: int | None = Field(default=None, ge=0)


class HswaeResolutionVocabulary(_HswaeOpenModel):
    """The FIXED vocabulary this resolution ran against.

    ⛔ Bounds the claim: an unresolved query is unresolved against ``size``
    labels from ``source_datasets``, which is not the same as absent from the
    biology. [bd-dre-pjhrp.11]
    """

    size: int | None = Field(default=None, ge=0)
    source_datasets: list[_HswaePublicString] | None = Field(default=None, max_length=50)
    tissue_count: int | None = Field(default=None, ge=0)
    scope: _HswaeNullableNote = None
    specificity_row_count: int | None = Field(default=None, ge=0)
    synonym_tables: HswaeResolutionSynonymTables | None = None


class HswaeCellTypeResolution(_HswaeOpenModel):
    """How one query string became (or failed to become) a canonical label.

    ``resolution_status`` and ``resolution_stage`` are plain strings rather
    than ``Literal``s on purpose — a status or stage added later must not
    become a hard parse failure. [bd-dre-pjhrp.11]
    """

    query: _HswaePublicString
    canonical: _HswaeNullablePublicString = None
    resolution_status: _HswaeNullablePublicString = None
    resolution_stage: _HswaeNullablePublicString = None
    resolution_is_proxy: bool | None = None
    resolution_is_ambiguous: bool | None = None
    ambiguous_alternatives: list[HswaeCellTypeAlternative] | None = Field(
        default=None,
        max_length=20,
    )
    stages_tried: list[_HswaeQueryToken] | None = Field(default=None, max_length=50)
    unresolved_reason: _HswaeNullableNote = None


class HswaeReferenceResolution(_HswaeOpenModel):
    """How one requested comparator resolved, and whether it counted.

    ⛔ ``contributes_to_differential`` False means this reference was NOT in
    the comparison — a selectivity margin that silently dropped comparators
    is not the margin the caller asked for. [bd-dre-pjhrp.11]
    """

    requested: _HswaePublicString
    canonical: _HswaeNullablePublicString = None
    resolution_status: _HswaeNullablePublicString = None
    resolution_stage: _HswaeNullablePublicString = None
    resolution_is_proxy: bool | None = None
    specificity_row_count: int | None = Field(default=None, ge=0)
    contributes_to_differential: bool | None = None
    unresolved_reason: _HswaeNullableNote = None


class HswaeDeliveryContextHint(_HswaeOpenModel):
    default_scope: Literal["topical", "tissue_resident", "systemic", "cluster_only"]
    rationale: Annotated[str, Field(min_length=1, max_length=10_000)]
    alternatives: list[Literal["topical", "tissue_resident", "systemic", "cluster_only"]] = Field(
        max_length=4
    )

    @field_validator("alternatives")
    @classmethod
    def _unique_alternatives(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("delivery alternatives must be unique")
        return value


class HswaeResolveCellTypeResult(_HswaeOpenModel):
    """A query resolved against the single-cell cell-type vocabulary.

    ⛔ ``canonical`` alone does not say whether the resolution was exact, a
    proxy, or ambiguous. Read ``resolution_status`` / ``resolution_stage`` /
    ``resolution_is_proxy`` / ``resolution_is_ambiguous`` before treating
    ``canonical`` as the cell type you asked for, and ``vocabulary`` to bound
    what an unresolved query means. [bd-dre-pjhrp.11]
    """

    query: _HswaePublicString
    canonical: _HswaeNullablePublicString
    candidates: list[HswaeResolveCellTypeCandidate] = Field(max_length=20)
    delivery_context_hint: HswaeDeliveryContextHint | None
    #: ``resolved`` / ``unresolved``, and the stage that got there.
    resolution_status: _HswaeNullablePublicString = None
    resolution_stage: _HswaeNullablePublicString = None
    resolution_is_proxy: bool | None = None
    resolution_is_ambiguous: bool | None = None
    ambiguous_alternatives: list[HswaeCellTypeAlternative] | None = Field(
        default=None,
        max_length=20,
    )
    #: Resolver stages attempted, in order — populated even on success.
    stages_tried: list[_HswaeQueryToken] | None = Field(default=None, max_length=50)
    #: Nearest labels with ``accepted`` False: why-not evidence for an
    #: unresolved query, not results.
    nearest_candidates: list[HswaeNearestCandidate] | None = Field(default=None, max_length=20)
    unresolved_reason: _HswaeNullableNote = None
    vocabulary: HswaeResolutionVocabulary | None = None


class HswaeDifferentialCellTypeRow(_HswaeOpenModel):
    gene_name: _HswaeGene
    target_cell_type: _HswaePublicString
    target_csi: float
    target_rank_global: int | None = Field(ge=1)
    target_mean_expression: float | None = Field(ge=0)
    max_reference_csi: float
    delta_csi: float
    is_surface_annotated: bool
    top_tissue: _HswaeNullablePublicString
    top_organ_system: _HswaeNullablePublicString
    #: The comparators ``max_reference_csi`` was actually taken over — a
    #: shorter list than the request means this row's margin is against
    #: FEWER references than asked for. [bd-dre-pjhrp.11]
    reference_cell_types_matched: list[_HswaePublicString] | None = Field(
        default=None,
        max_length=200,
    )


class HswaeCompareCellTypesResult(_HswaeOpenModel):
    """Target-vs-reference differential specificity.

    ⛔ An empty ``differential`` is not "nothing is differential": read
    ``differential_status`` (``computed`` / ``target_unresolved`` /
    ``no_references_resolved`` / ``no_rows_above_threshold``) and
    ``differential_status_detail`` first, and check
    ``reference_resolution`` for comparators that did not contribute.
    ``differential_truncated_at_top_n`` distinguishes a truncated list from a
    short one. [bd-dre-pjhrp.11]
    """

    target_cell_type: _HswaePublicString
    target_query: _HswaePublicString
    target_proxy_note: _HswaeNullablePublicString
    reference_cell_types: list[_HswaePublicString] = Field(max_length=200)
    differential: list[HswaeDifferentialCellTypeRow] = Field(max_length=200)
    comparator_scope: (
        Literal[
            "topical",
            "tissue_resident",
            "systemic",
            "cluster_only",
        ]
        | None
    )
    comparator_scope_source: Literal["request", "delivery_context", "none"]
    delivery_context: _HswaeNullablePublicString
    #: Whether a differential was computed at all, and why not when it was not.
    #: A plain string, not a ``Literal``: a new status must not break parsing.
    differential_status: _HswaeNullablePublicString = None
    differential_status_detail: _HswaeNullableNote = None
    #: Per-side resolution provenance for the target and every comparator.
    target_resolution: HswaeCellTypeResolution | None = None
    reference_resolution: list[HswaeReferenceResolution] | None = Field(
        default=None,
        max_length=200,
    )
    #: What the caller asked for, before resolution and scope auto-population
    #: — compare against ``reference_cell_types`` to see what was dropped.
    reference_cell_types_requested: list[_HswaePublicString] | None = Field(
        default=None,
        max_length=200,
    )
    comparator_scope_note: _HswaeNullableNote = None
    #: True when ``differential`` was cut at the requested ``top_n``.
    differential_truncated_at_top_n: bool | None = None


class HswaeIsoformExpressionContext(_HswaeOpenModel):
    tissue: _HswaePublicString
    cell_type: _HswaeNullablePublicString
    context_name: _HswaePublicString
    mean_tpm: float = Field(ge=0)
    median_tpm: float = Field(ge=0)
    max_tpm: float = Field(ge=0)
    prevalence_tpm1: float = Field(ge=0, le=1)
    sample_count: int = Field(ge=0)
    #: Whether THIS row is tissue- or cell-type-resolved. ⛔ Per-row, because
    #: the result-level ``resolution`` can claim ``cell_type`` while every
    #: returned row is tissue-only. [bd-dre-pjhrp.11]
    context_resolution: _HswaeNullablePublicString = None


class HswaeIsoformSpecificityContext(_HswaeOpenModel):
    tissue: _HswaePublicString
    cell_type: _HswaeNullablePublicString
    transcript_si: float
    usage_ratio: float
    isi: float
    isoform_usage: float
    target_mean_tpm: float = Field(ge=0)
    gene_mean_tpm: float = Field(ge=0)
    rank_expression: int = Field(ge=1)
    rank_usage: int = Field(ge=1)
    context_resolution: _HswaeNullablePublicString = None


class HswaeIsoformCellTypeExpressionContext(_HswaeOpenModel):
    """A genuinely cell-type-resolved isoform row, from the narrower
    single-cell transcript vocabulary. [bd-dre-pjhrp.11]"""

    tissue: _HswaePublicString
    cell_type: _HswaeNullablePublicString = None
    cluster: _HswaeNullablePublicString = None
    context_resolution: _HswaeNullablePublicString = None
    transcript_expression: float | None = None
    gene_expression: float | None = None
    isoform_usage: float | None = None
    expression_unit: _HswaeNullablePublicString = None
    cell_count: int | None = Field(default=None, ge=0)
    cell_transcript_si: float | None = None
    cell_usage_ratio: float | None = None
    cisi: float | None = None
    rank_expression: int | None = Field(default=None, ge=1)
    rank_usage: int | None = Field(default=None, ge=1)
    competitor_cell_type: _HswaeNullablePublicString = None
    competitor_tissue: _HswaeNullablePublicString = None
    evidence_level: _HswaeNullablePublicString = None
    platform: _HswaeNullablePublicString = None
    source_dataset: _HswaeNullablePublicString = None


class HswaeIsoformResultRow(_HswaeOpenModel):
    transcript_id: Annotated[str, Field(min_length=1, max_length=200)]
    transcript_id_versioned: Annotated[str, Field(min_length=1, max_length=200)]
    transcript_name: Annotated[str, Field(min_length=1, max_length=500)]
    transcript_type: Annotated[str, Field(min_length=1, max_length=200)]
    expression_by_context: list[HswaeIsoformExpressionContext] = Field(max_length=500)
    specificity_by_context: list[HswaeIsoformSpecificityContext] = Field(max_length=500)
    cell_type_expression_by_context: list[HswaeIsoformCellTypeExpressionContext] | None = Field(
        default=None,
        max_length=500,
    )


class HswaeIsoformResolutionScope(_HswaeOpenModel):
    """Size of the cell-type transcript vocabulary this gene was looked up in.

    ⛔ A SEPARATE, much narrower vocabulary than the cell-type resolution
    layer: ``populated`` True with ``gene_row_count`` 0 means the atlas
    exists but has no row for this gene. [bd-dre-pjhrp.11]
    """

    populated: bool | None = None
    row_count: int | None = Field(default=None, ge=0)
    gene_row_count: int | None = Field(default=None, ge=0)
    cell_type_count: int | None = Field(default=None, ge=0)
    tissue_count: int | None = Field(default=None, ge=0)
    source_datasets: list[_HswaePublicString] | None = Field(default=None, max_length=50)
    note: _HswaeNullableNote = None


class HswaeIsoformReturnedRowCounts(_HswaeOpenModel):
    """How the returned rows actually resolved, independent of the claim."""

    tissue_resolved: int | None = Field(default=None, ge=0)
    cell_type_resolved: int | None = Field(default=None, ge=0)
    with_cell_type_label: int | None = Field(default=None, ge=0)
    cell_type_rows_below_threshold: int | None = Field(default=None, ge=0)


class HswaeIsoformExpressionResult(_HswaeOpenModel):
    """Per-isoform expression and specificity for one gene.

    ⛔ ``resolution`` is the backend's CLAIM; ``observed_resolution`` is what
    the returned rows actually are (e.g. claim ``cell_type`` vs observed
    ``tissue_only_in_returned_records``). Trust the observed value, and read
    ``returned_row_counts`` for the split. [bd-dre-pjhrp.11]
    """

    gene: Annotated[str, Field(min_length=1, max_length=64)]
    isoform_count: int = Field(ge=0, le=50)
    isoforms: list[HswaeIsoformResultRow] = Field(max_length=50)
    resolution: Literal["none", "tissue", "cell_type"]
    note: Annotated[str, Field(min_length=1, max_length=10_000)]
    #: What the returned rows ARE, vs what the backend claimed. Plain strings:
    #: the two vocabularies already differ (``no_records`` vs
    #: ``no_rows_returned``) and may grow.
    observed_resolution: _HswaeNullablePublicString = None
    backend_observed_resolution: _HswaeNullablePublicString = None
    cell_type_resolution_available: bool | None = None
    cell_type_resolution_scope: HswaeIsoformResolutionScope | None = None
    returned_row_counts: HswaeIsoformReturnedRowCounts | None = None


class HswaeAnnotationCandidate(_HswaeOpenModel):
    cell_type: _HswaePublicString = Field(alias="cellType")
    confidence: float = Field(ge=0, le=1)
    supporting_genes: list[HswaeSupportingGeneEvidence] = Field(
        alias="supportingGenes",
        max_length=10,
    )
    vote_count: int = Field(alias="voteCount", ge=0)
    marker_score: float = Field(alias="markerScore")
    top_tissue: _HswaeNullablePublicString = Field(alias="topTissue")
    top_organ_system: _HswaeNullablePublicString = Field(alias="topOrganSystem")


class HswaeAnnotationAssignment(_HswaeOpenModel):
    cluster_id: Annotated[str, Field(min_length=1, max_length=500)] = Field(alias="clusterId")
    marker_count: int = Field(alias="markerCount", ge=0, le=50)
    candidates: list[HswaeAnnotationCandidate] = Field(max_length=10)
    top_assignment: _HswaeNullablePublicString = Field(alias="topAssignment")
    confidence: float = Field(ge=0, le=1)


class HswaeAnnotateDatasetResult(_HswaeOpenModel):
    dataset_ref: Annotated[str, Field(min_length=1, max_length=2_000)] = Field(alias="datasetRef")
    cluster_assignments: list[HswaeAnnotationAssignment] = Field(
        alias="clusterAssignments",
        max_length=500,
    )
    method: Literal["marker_overlap_vote"]
    reference: Literal["HSWAE-2 cell_type_global_specificity (HPA v25 surfaceome)"]
    source: Literal["authorized_user_dataset"] | None
    note: _HswaeNullablePublicString


class HswaeComplexSubunitExpression(_HswaeOpenModel):
    subunit: Annotated[str, Field(min_length=1, max_length=100)]
    expression: float = Field(ge=0)

    @field_validator("subunit")
    @classmethod
    def _canonical_subunit(cls, value: str) -> str:
        if value != value.strip().upper() or _HSWAE_COMPLEX_SUBUNIT_PATTERN.fullmatch(value) is None:
            raise ValueError("complex subunit is not canonical")
        return value


class HswaeComplexSpecificityContext(_HswaeOpenModel):
    tissue: _HswaePublicString
    cell_type: _HswaePublicString
    cluster: Annotated[str, Field(max_length=500)]
    subunit_expression: list[HswaeComplexSubunitExpression] = Field(
        min_length=2,
        max_length=32,
    )
    expressed_subunits: int = Field(ge=0, le=32)
    required_subunits: int = Field(ge=2, le=32)
    limiting_subunit: Annotated[str, Field(max_length=100)] | None
    limiting_expression: float = Field(ge=0)
    max_subunit_expression: float = Field(ge=0)
    geometric_mean_expression: float = Field(ge=0)
    balance_score: float = Field(ge=0, le=1)
    complex_signal: float = Field(ge=0)
    heteromer_si: float = Field(ge=0)
    heteromer_si_denominator: float = Field(ge=0)
    competitor_below_floor: bool
    competitor_tissue: _HswaeNullablePublicString
    competitor_cell_type: _HswaeNullablePublicString
    competitor_signal: float = Field(ge=0)
    rank: int = Field(ge=1)
    source_dataset: Literal[
        "single_cell_expression",
        "hpa_v25_single_cell_cluster",
        "ligandai_specialized_reference",
    ]

    @field_validator("tissue", "cell_type")
    @classmethod
    def _public_nonempty_labels(cls, value: str) -> str:
        return _validate_hswae_complex_public_label(value)

    @field_validator("cluster")
    @classmethod
    def _public_cluster_label(cls, value: str) -> str:
        return _validate_hswae_complex_public_label(value, allow_empty=True)

    @field_validator("competitor_tissue", "competitor_cell_type")
    @classmethod
    def _public_nullable_labels(cls, value: str | None) -> str | None:
        if value is not None:
            _validate_hswae_complex_public_label(value)
        return value

    @model_validator(mode="after")
    def _validate_subunit_counts(self) -> HswaeComplexSpecificityContext:
        expression_subunits = [entry.subunit for entry in self.subunit_expression]
        if len(expression_subunits) != len(set(expression_subunits)):
            raise ValueError("complex context subunit expressions must be unique")
        if len(expression_subunits) != self.required_subunits:
            raise ValueError("complex context required-subunit count mismatch")
        if self.expressed_subunits > self.required_subunits:
            raise ValueError("complex context expressed-subunit count is invalid")
        if self.limiting_expression > self.max_subunit_expression:
            raise ValueError("complex context limiting expression exceeds its maximum")
        if self.limiting_subunit is not None and self.limiting_subunit not in expression_subunits:
            raise ValueError("complex context limiting subunit is not required")
        return self


class HswaeComplexSpecificityResult(_HswaeOpenModel):
    complex_name: Annotated[str, Field(min_length=1, max_length=200)] = Field(alias="complexName")
    subunits: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(
        min_length=2,
        max_length=32,
    )
    contexts: list[HswaeComplexSpecificityContext] = Field(max_length=200)

    @field_validator("complex_name")
    @classmethod
    def _public_complex_name(cls, value: str) -> str:
        if not value.strip() or any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("complex name is invalid")
        return value

    @model_validator(mode="after")
    def _validate_result_bindings(self) -> HswaeComplexSpecificityResult:
        if any(
            subunit != subunit.strip().upper()
            or _HSWAE_COMPLEX_SUBUNIT_PATTERN.fullmatch(subunit) is None
            for subunit in self.subunits
        ) or len(self.subunits) != len(set(self.subunits)):
            raise ValueError("complex result subunits are not canonical and unique")
        for context in self.contexts:
            expression_subunits = [entry.subunit for entry in context.subunit_expression]
            if expression_subunits != self.subunits:
                raise ValueError("complex context subunits do not match the result")
            if context.required_subunits != len(self.subunits):
                raise ValueError("complex context required-subunit count mismatch")
        return self


class HswaeCapabilityOwner(_HswaeOpenModel):
    user_id: Annotated[str, Field(min_length=1, max_length=500)] = Field(alias="userId")
    org_id: Annotated[str, Field(max_length=500)] | None = Field(alias="orgId")


class _HswaeCapabilityReceiptBase(_HswaeOpenModel):
    receipt_version: Literal["1"] = Field(alias="receiptVersion")
    contract_version: Literal["1.0.0"] = Field(alias="contractVersion")
    request_id: Annotated[str, Field(min_length=1, max_length=500)] = Field(alias="requestId")
    owner: HswaeCapabilityOwner
    status: Literal["executed"]
    effect: Literal["read_local"]
    created_at: Annotated[str, Field(min_length=1, max_length=100)] = Field(alias="createdAt")


class HswaeSemanticSearchReceipt(_HswaeCapabilityReceiptBase):
    operation: Literal["hswae.v1.semantic_search"]
    result: HswaeSemanticSearchResult


class HswaeSurfaceMarkersReceipt(_HswaeCapabilityReceiptBase):
    operation: Literal["hswae.v1.surface_markers"]
    result: HswaeSurfaceMarkersResult


class HswaeResolveCellTypeReceipt(_HswaeCapabilityReceiptBase):
    operation: Literal["hswae.v2.resolve_cell_type"]
    result: HswaeResolveCellTypeResult


class HswaeCompareCellTypesReceipt(_HswaeCapabilityReceiptBase):
    operation: Literal["hswae.v2.compare_cell_types"]
    result: HswaeCompareCellTypesResult


class HswaeIsoformExpressionReceipt(_HswaeCapabilityReceiptBase):
    operation: Literal["hswae.v2.isoform_expression"]
    result: HswaeIsoformExpressionResult


class HswaeAnnotateDatasetReceipt(_HswaeCapabilityReceiptBase):
    operation: Literal["hswae.v2.annotate_dataset"]
    result: HswaeAnnotateDatasetResult


class HswaeComplexSpecificityReceipt(_HswaeCapabilityReceiptBase):
    operation: Literal["predictive_interactomics.complex_specificity"]
    result: HswaeComplexSpecificityResult


HswaeCapabilityReceipt: TypeAlias = Annotated[
    HswaeSemanticSearchReceipt
    | HswaeSurfaceMarkersReceipt
    | HswaeResolveCellTypeReceipt
    | HswaeCompareCellTypesReceipt
    | HswaeIsoformExpressionReceipt
    | HswaeAnnotateDatasetReceipt
    | HswaeComplexSpecificityReceipt,
    Field(discriminator="operation"),
]


class BBBReceptor(_LGModel):
    """A BBB transcytosis shuttle receptor returned by
    :meth:`~ligandai.resources.discovery.Discovery.transport_vasculome`.

    Beyond the transport suitability ``score``, this now carries the
    BBB-specificity / shared-expression overlay [bd-LIGANDAI_ALPHA_V2-k5usa]:
    ``specificity_index`` (higher = more CNS-selective), the peripheral GTEx
    tissues the shuttle is ALSO expressed in (``top_peripheral_tissues``), and a
    ``broadly_shared`` flag (with ``broadly_shared_reason``) so you can tell a
    brain-selective shuttle from a broadly-expressed, off-target-prone one. When
    a ``specificity_weight`` > 0 was passed, ``combined_score`` is the blended
    transport + specificity ranking score.
    """

    gene: str
    score: float
    risk_factors: list[str] | None = Field(default=None, alias="riskFactors")
    notes: str | None = None
    # -- BBB specificity / shared-expression overlay -------------------------
    specificity_index: float | None = Field(default=None, alias="specificityIndex")
    enrichment: float | None = None
    gtex_max_tpm: float | None = Field(default=None, alias="gtexMaxTpm")
    top_peripheral_tissues: list[Any] | None = Field(default=None, alias="topPeripheralTissues")
    broadly_shared: bool | None = Field(default=None, alias="broadlyShared")
    broadly_shared_reason: str | None = Field(default=None, alias="broadlySharedReason")
    combined_score: float | None = Field(default=None, alias="combinedScore")
    transport_score: float | None = Field(default=None, alias="transportScore")

    @model_validator(mode="before")
    @classmethod
    def _coerce_receptor_row(cls, data: Any) -> Any:
        """Accept BOTH the SDK-alias ``{gene, score}`` shape and a raw
        ``transport_receptors`` row (``uniprot_gene`` + ``monovalent_score`` /
        ``multivalent_score`` + ``top_gtex_tissues``) so the platform's
        ``/receptors`` response validates without a server-side transform."""
        if not isinstance(data, dict):
            return data
        d = dict(data)
        if "gene" not in d:
            if d.get("uniprot_gene"):
                d["gene"] = d["uniprot_gene"]
            elif d.get("ensembl_gene"):
                d["gene"] = d["ensembl_gene"]
        if "score" not in d:
            score = (
                d.get("combined_score")
                if d.get("combined_score") is not None
                else d.get("monovalent_score")
                if d.get("monovalent_score") is not None
                else d.get("multivalent_score")
                if d.get("multivalent_score") is not None
                else d.get("suitability_score")
            )
            if score is not None:
                d["score"] = score
        # peripheral tissues land under top_gtex_tissues from the server
        if d.get("top_peripheral_tissues") is None and d.get("top_gtex_tissues") is not None:
            d["top_peripheral_tissues"] = d["top_gtex_tissues"]
        return d


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

    # -- Cavaco half-life regression -----------------------------------------
    # Cavaco et al. 2021 (PMID 33641212):
    #   ln(t½) = 2.226 + 0.053·NP% − 1.515·W(0,1) + 1.290·Y(0,1) − 1.052·pI(0,1)
    # A SECOND, INDEPENDENT estimator from ``predicted_halflife_min`` (which is a
    # trained SeqConvMLP head). The paper's (0,1) normalisation is not recorded in
    # our source, so W/Y are presence indicators and pI is min-max scaled over
    # 3-12: ``cavaco_ln_t_half`` is INDICATIVE and valid for RANKING only, never
    # as a quantitative t½. The raw components are surfaced so the ordering — the
    # part that depends only on the unambiguous coefficient signs — can be checked.
    cavaco_ln_t_half: float | None = Field(
        default=None, alias="cavaco_ln_t_half",
        description="ln(t½) from the Cavaco regression. RANKING ONLY — not a quantitative half-life.",
    )
    cavaco_halflife_score: float | None = Field(
        default=None, alias="cavaco_halflife_score",
        description="Monotone 0-1 rank transform of cavaco_ln_t_half (higher = longer-lived).",
    )
    cavaco_np_percent: float | None = Field(
        default=None, alias="cavaco_np_percent",
        description="Nonpolar residue percentage (AVILMFPGW). Extends half-life.",
    )
    cavaco_n_trp: int | None = Field(default=None, alias="cavaco_n_trp")
    cavaco_has_trp: float | None = Field(
        default=None, alias="cavaco_has_trp",
        description="Trp presence indicator used for the W(0,1) term. Trp SHORTENS half-life.",
    )
    cavaco_n_tyr: int | None = Field(default=None, alias="cavaco_n_tyr")
    cavaco_has_tyr: float | None = Field(
        default=None, alias="cavaco_has_tyr",
        description="Tyr presence indicator used for the Y(0,1) term. Tyr EXTENDS half-life.",
    )
    cavaco_isoelectric_point: float | None = Field(
        default=None, alias="cavaco_isoelectric_point",
        description="Computed pI (EMBOSS pKa table). High pI SHORTENS half-life (renal clearance).",
    )
    cavaco_pI_norm: float | None = Field(
        default=None, alias="cavaco_pI_norm",
        description="pI min-max scaled over 3-12 for the pI(0,1) term.",
    )
    cavaco_contributions: dict[str, Any] | None = Field(
        default=None, alias="cavaco_contributions",
        description="Signed per-term contributions to ln(t½): intercept, np_percent, trp, tyr, pI.",
    )
    cavaco_caveat: str | None = Field(
        default=None, alias="cavaco_caveat",
        description="Normalisation caveat carried from the server. Read it before quoting an absolute t½.",
    )


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


class QualityTier(_LGModel):
    """Acceptance-gate verdict for one fold result, from the ONE shared server-side
    evaluator (``shared/acceptance-gate.json`` / ``fastapi_backend/core/acceptance_gate.py``
    / ``shared/acceptance-gate.ts``). The SDK never re-derives this judgement --
    re-implementing the gate client-side is exactly the failure this system was built to
    end (six divergent copies existed before it, one numerically wrong: pLDDT 0.80 where
    canonical is 88/100). This model only surfaces what the server already decided.

    Populated on :class:`Peptide` (from :meth:`~ligandai.resources.peptides.Peptides.search`
    and friends) as the STRONGEST criterion the design actually clears -- independent of
    whatever ``is_elite`` / ``super_elite`` / ``acceptance_criterion`` filter was applied to
    the request. If the design clears none of the four criteria, it reports the result
    against the weakest one (``great_plus``), so :attr:`summary` explains why even the floor
    bar was missed rather than surfacing a stronger criterion's less-informative failure.

    ⛔ UNIT TRAPS carried by the underlying gate -- get any of these wrong and the verdict
    is silently meaningless:
      - ``plddt`` is on the 0-100 scale. A threshold of ``0.88`` is a silent no-op that lets
        every fold through.
      - ``predicted_kd`` (the affinity leg, only on ``super_elite_affinity``) is NANOMOLAR.
        Compare to ``100``, never ``100e-9``.
      - The structural leg gates ``peptide_ipsae`` -- the peptide<->receptor interface
        score -- and NEVER whole-complex ``ipsae``. On the 2026-07-30 corpus, gating on the
        whole-complex value admitted 248 genuine false positives.

    ⛔ :attr:`verdict` is one of ``'pass' | 'fail' | 'indeterminate'``. ``'indeterminate'``
    means a leg could not be evaluated at all -- the metric was never computed, the
    construct frame was unverified, or (only relevant when ensemble co-validation was
    requested) a required engine had no comparable result in the same frame. It is NEVER
    the same claim as ``'fail'`` and must never be treated as one: "we did not measure
    this" is not "this design is bad".

    THE DUAL GATE (``require_ensemble=True``)

    A high-recall ranker followed by a high-precision filter, not two scores averaged.
    Boltz-2 must clear the structural criterion you selected, AND the LigandIQ Pre-Fold
    confirmatory screen must independently clear its own calibrated threshold, both on
    the SAME construct frame. Each engine clears its own bar; neither borrows the
    other's.

    ⛔ IT TRADES RECALL FOR PRECISION. The confirmatory screen discards a large share of
    true binders by design, so far fewer designs survive the dual gate than the
    single-engine bar. A design the screen does not confirm is reported as not
    co-validated -- that is NOT evidence the design is bad, and it must never be
    presented as a failed design.

    ⛔ :attr:`co_validated` is ``True`` ONLY when both engines independently cleared
    their own bar on one frame. When the confirmatory screen has no result for a design,
    the verdict is ``'indeterminate'`` and :attr:`co_validated` is ``False`` -- never a
    silent downgrade to the single-engine answer wearing a dual-gate label. If no design
    in a set has a confirmatory result, that is a COVERAGE statement about the screen not
    having been run, not a finding of "zero co-validated designs".
    """

    criterion: str | None = Field(
        default=None,
        description="Which of the four canonical criteria this verdict is against: "
                    "'great_plus' | 'elite' | 'super_elite' | 'super_elite_affinity'.",
    )
    verdict: str | None = Field(
        default=None,
        description="'pass' | 'fail' | 'indeterminate'. 'indeterminate' means a leg was "
                    "never measured -- treat it as 'not measured', never as 'fail'.",
    )
    co_validated: bool | None = Field(
        default=None, alias="coValidated",
        description="True only when ensemble co-validation was REQUESTED and genuinely "
                    "satisfied by every required engine in the same fold frame. NOT "
                    "persistable on the platform today -- see the class docstring; never "
                    "read this as evidence of cross-engine agreement until "
                    "bd-LIGANDAI_ALPHA_V2-xv5ih closes.",
    )
    summary: str | None = Field(
        default=None,
        description="Human-readable one-liner, safe to show directly in a table cell or an "
                    "AI answer -- e.g. \"Passes Super-elite (structural).\" or \"Cannot be "
                    "judged against Elite (peptide_ipsae_missing) -- reported as "
                    "indeterminate, not as a pass or a failure.\"",
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
    # The platform emits foldId as an INTEGER on the peptide-listing routes
    # (/api/v1/peptides, /peptides/{id}) and as a string elsewhere. Declaring it
    # str-only made every real `peptides.list()` / `.get()` raise
    # ValidationError("Input should be a valid string ... input_value=61811").
    # Accept both and normalise to str, so downstream uses like
    # f"/api/v1/peptides/{p.fold_id}" keep working regardless of which shape the
    # server sent. Caught by the live integration suite, not by unit tests.
    fold_id: str | None = Field(default=None, alias="foldId")

    @field_validator("fold_id", mode="before")
    @classmethod
    def _coerce_fold_id(cls, v: object) -> object:
        return str(v) if isinstance(v, int) else v

    @model_validator(mode="before")
    @classmethod
    def _normalize_server_row_keys(cls, data: Any) -> Any:
        """``GET /api/v1/peptides/search`` (and ``/v1/peptides/list``) key the
        target as ``gene`` and the DeltaForge energy as ``deltaG`` — this model
        names them ``targetGene`` / ``deltaforgeDg``, so on search rows
        ``p.target_gene`` and ``p.deltaforge_dg`` read None while ``ipsae`` /
        ``iptm`` / ``predictedKd`` on the SAME row populated fine. Tom's GUI
        testing saw exactly that: Target and deltaG blank across 20 TNF
        designs. This fills the typed field from the server's real key when
        (and only when) the canonical alias is absent — no value is invented,
        and a row that carries ``targetGene``/``deltaforgeDg`` is untouched.
        [bd-LIGANDAI_ALPHA_V2-tom-D4]
        """
        if not isinstance(data, dict):
            return data
        out = data
        if out.get("targetGene") is None and isinstance(out.get("gene"), str):
            out = {**out, "targetGene": out["gene"]}
        if out.get("deltaforgeDg") is None:
            for key in ("deltaG", "delta_g", "dg"):
                if isinstance(out.get(key), (int, float)):
                    out = {**out, "deltaforgeDg": out[key]}
                    break
        return out
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
        description="Legacy single-metric server flag -- binder (peptide) peptide_ipsae >= 0.66 "
                    "(ELITE_PEPTIDE_IPSAE_THRESHOLD); source of truth is the platform's "
                    "shared/peptide-ipsae.ts, mirrored as "
                    "ligandai._constants.IS_ELITE_PEPTIDE_IPSAE_THRESHOLD. Earlier releases "
                    "described this as 0.80, which the gate has never enforced. "
                    "NOT the 0.85 threshold of the canonical 'elite' criterion in "
                    "shared/acceptance-gate.json. Kept for back-compat only. Prefer "
                    ":attr:`quality_tier`, which reports the strongest criterion this design "
                    "actually clears against the one shared ladder (great_plus / elite / "
                    "super_elite / super_elite_affinity).",
    )
    quality_tier: QualityTier | None = Field(
        default=None, alias="qualityTier",
        description="Server-computed acceptance-gate verdict -- the strongest criterion this "
                    "design clears (or, if none, the result against the weakest, "
                    "'great_plus'). None on responses from before this field existed. See "
                    ":class:`QualityTier` for the unit traps and the ensemble "
                    "co-validation caveat.",
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
    # ── PDE (predicted distance error), migration 170 ────────────────────────
    # Returned by /api/v1/peptides/{list,search,:id} and /api/v1/structures/list.
    # ⛔ Never read one of these numbers without pde_engine / pde_kind /
    # pde_scale — use the bundled `pde` accessor below. See PdeScores.
    peptide_ipsae: float | None = Field(
        default=None, alias="peptideIpsae",
        description="Peptide<->receptor interface iPSAE — THE binder metric. None means "
                    "not computed; never falls back to the complex-level `ipsae`.",
    )
    peptide_ipsae_status: str | None = Field(
        default=None, alias="peptideIpsaeStatus",
        description="'ok' when peptide_ipsae is present, 'not_computed' when it is NULL.",
    )
    gpde: float | None = Field(
        default=None,
        description="Complex-level contact-probability-weighted mean PDE, Angstroms.",
    )
    interface_gpde: float | None = Field(
        default=None, alias="interfaceGpde",
        description="gPDE restricted to binder<->receptor token pairs, Angstroms.",
    )
    interface_pde_min: float | None = Field(
        default=None, alias="interfacePdeMin",
        description="MIN interface PDE, Angstroms. LOWER IS BETTER. Never back-filled "
                    "from gpde/interface_gpde. Comparable only within one engine.",
    )
    pde_rankable: float | None = Field(
        default=None, alias="pdeRankable",
        description="Cross-engine-safe PDE — equals interface_pde_min only when "
                    "pde_kind='native' AND pde_scale='angstrom', else None. Rank on this.",
    )
    pde_engine: str | None = Field(
        default=None, alias="pdeEngine",
        description="FoldEngineId that produced the PDE value.",
    )
    pde_kind: str | None = Field(
        default=None, alias="pdeKind",
        description="'native' (engine's token_pair_pde head) or 'proxy' (PAE-derived).",
    )
    pde_scale: str | None = Field(
        default=None, alias="pdeScale",
        description="'angstrom' (verified) or 'unverified' (Boltz-2).",
    )
    pde_interpretation: str | None = Field(
        default=None, alias="pdeInterpretation",
        description="'lower_is_better' — the direction, stated rather than inferred.",
    )
    pde_units: str | None = Field(
        default=None, alias="pdeUnits",
        description="'angstrom' | 'unverified' | None.",
    )

    @property
    def pde(self) -> PdeScores | None:
        """PDE bundled with its provenance tags, or ``None`` if none reported.

        Mirrors :attr:`FoldResult.pde`. Use
        ``peptide.pde.cross_engine_rank_value`` to rank a search result set that
        may span fold engines; it returns ``None`` for proxy / unverified-scale
        rows rather than letting them into the ordering.
        """
        block = PdeScores(
            gpde=self.gpde,
            interfaceGpde=self.interface_gpde,
            interfacePdeMin=self.interface_pde_min,
            pdeRankable=self.pde_rankable,
            pdeEngine=self.pde_engine,
            pdeKind=self.pde_kind,
            pdeScale=self.pde_scale,
            pdeInterpretation=self.pde_interpretation,
            pdeUnits=self.pde_units,
        )
        return block if block else None


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
    """A chain to fold: a sequence plus the flags that classify it.

    ⛔ A DE NOVO BINDER MUST BE FLAGGED. [bd-dre-yt8kx]

    ``fold()`` treats an unflagged ``protein`` chain as a RECEPTOR, and a
    receptor is entitled to a multiple-sequence alignment. A LigandForge design
    (or any de novo binder) has no homologs — that is the point of designing it
    — so the platform runs a full mmseqs search that can only come back
    degenerate, and when the MSA service cannot answer at all the fold is
    REFUSED for an alignment that cannot exist. Set ``is_peptide=True`` (or
    ``use_msa=False``, or ``role="peptide"``) on every designed chain; the
    receptor chain keeps its MSA.

    Any ONE of ``is_peptide`` / ``use_msa=False`` / ``role`` is sufficient — the
    server's MSA prefetch guard accepts them interchangeably.
    """

    sequence: str
    name: str | None = None
    target_gene: str | None = Field(default=None, alias="targetGene")
    target_chain: str | None = Field(default=None, alias="targetChain")
    #: Boltz-2 entity type. ``"protein"`` (the default) covers peptides — a
    #: peptide is a short protein chain, not a separate entity type.
    type: str | None = None
    #: Mark this chain a de novo peptide / binder. Emits ``isPeptide`` +
    #: ``is_peptide`` on the wire and suppresses the MSA search for the chain.
    is_peptide: bool | None = Field(default=None, alias="isPeptide")
    #: Explicit MSA opt-out. ``False`` is itself a peptide signal to the guard.
    use_msa: bool | None = Field(default=None, alias="useMsa")
    #: ``"peptide"`` | ``"binder"`` | ``"ligand_peptide"`` — a third accepted
    #: spelling of the same signal, for callers who model chains by role.
    role: str | None = None
    #: DEPRECATED alias for :attr:`use_msa`. This field existed for several
    #: releases and was NEVER READ by the SDK, so ``Sequence(msa=False)``
    #: silently folded the chain as a receptor. It is now honored as
    #: ``use_msa``; prefer ``use_msa=`` in new code.
    msa: bool | None = None

    @property
    def msa_disabled(self) -> bool:
        """True when this chain has opted out of an MSA by any spelling.

        Precedence, matching the server guard:

        1. ``is_peptide=True`` or a peptide ``role`` is AUTHORITATIVE — a chain
           declared a design never demands an MSA, even alongside a stray
           ``use_msa=True``. That direction is the safe one: the cost of
           skipping an MSA for a real receptor is a worse model, the cost of
           demanding one for a design is a REFUSED fold.
        2. Otherwise an explicit ``use_msa`` decides, and only when it is unset
           does the deprecated ``msa`` alias apply.
        """
        if self.is_peptide is True:
            return True
        if isinstance(self.role, str) and self.role.strip().lower() in (
            "peptide",
            "binder",
            "ligand_peptide",
        ):
            return True
        effective = self.use_msa if self.use_msa is not None else self.msa
        return effective is False


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


class PdeScores(_LGModel):
    """PDE (predicted distance error) bundled WITH the provenance that makes it
    interpretable.

    PDE is a distinct signal from PAE, not a derivative of it: PDE is symmetric
    and frame-free where PAE is asymmetric. On the 199-fold labelled holdout,
    :attr:`interface_pde_min` scored AUROC 0.806 [0.743, 0.862] — statistically
    indistinguishable from iPSAE's 0.811 — while the interface MEAN scored only
    0.755. So the MIN is the ranking scalar, and **lower is better**: the
    opposite direction to iPSAE / iPTM / pLDDT.

    ⛔ **A bare PDE number is uninterpretable.** It is comparable to another PDE
    number only when both came from engines on the same scale. Boltz-2's
    ``complex_pde``/``complex_ipde`` has never been verified against the
    Angstrom-scale gPDE that OpenDDE and Protenix emit, and a PAE-derived
    surrogate (``kind='proxy'``) is a different quantity from a native
    ``token_pair_pde`` read. That is why this model exists: it is the only shape
    in the SDK where a PDE value and its :attr:`engine` / :attr:`kind` /
    :attr:`scale` tags cannot be separated.

    Rank, sort and filter across engines on :attr:`cross_engine_rank_value`
    (equivalently: the server's DB-generated ``pdeRankable``). Reading
    :attr:`interface_pde_min` directly is valid only when the comparison set is
    pinned to a single engine — pass ``pde_engine=`` to
    :meth:`~ligandai.resources.peptides.Peptides.search` to do that.
    """

    gpde: float | None = None
    """Complex-level contact-probability-weighted mean PDE, Angstroms."""
    interface_gpde: float | None = Field(default=None, alias="interfaceGpde")
    """Same statistic restricted to binder<->receptor token pairs, Angstroms."""
    interface_pde_min: float | None = Field(default=None, alias="interfacePdeMin")
    """MIN interface PDE, Angstroms. LOWER IS BETTER. The ranking scalar.

    ``None`` means "not computed" — never back-filled from :attr:`gpde` or
    :attr:`interface_gpde`, which measure the whole complex rather than the
    interface."""
    rankable: float | None = Field(default=None, alias="pdeRankable")
    """DB-generated cross-engine gate: equals :attr:`interface_pde_min` only when
    ``kind == 'native'`` and ``scale == 'angstrom'``, else ``None``. Prefer
    :attr:`cross_engine_rank_value`, which falls back to recomputing this from
    the tags for payloads that predate the generated column."""
    engine: str | None = Field(default=None, alias="pdeEngine")
    """FoldEngineId that produced the value (e.g. ``'boltz2'``, ``'protenix'``)."""
    kind: str | None = Field(default=None, alias="pdeKind")
    """``'native'`` — read from the engine's own token_pair_pde head.
    ``'proxy'`` — reconstructed from PAE or another surrogate."""
    scale: str | None = Field(default=None, alias="pdeScale")
    """``'angstrom'`` — verified Angstrom scale. ``'unverified'`` — scale not
    established (Boltz-2 lands here)."""
    interpretation: str | None = Field(default=None, alias="pdeInterpretation")
    """Always ``'lower_is_better'`` when the server states a direction."""
    units: str | None = Field(default=None, alias="pdeUnits")
    """``'angstrom'`` | ``'unverified'`` | ``None``."""

    @property
    def is_cross_engine_comparable(self) -> bool:
        """True only for a native read on a verified Angstrom scale."""
        return self.kind == "native" and self.scale == "angstrom"

    @property
    def cross_engine_rank_value(self) -> float | None:
        """The ONLY value safe to rank/sort/filter across fold engines.

        ``None`` when this fold's PDE is a proxy or sits on an unverified scale
        — which is a refusal to compare, not a missing measurement. Read
        :attr:`interface_pde_min` if you have pinned the comparison to one
        engine.
        """
        if self.rankable is not None:
            return self.rankable
        return self.interface_pde_min if self.is_cross_engine_comparable else None

    def __bool__(self) -> bool:
        """Falsy when no PDE number was reported at all."""
        return any(
            v is not None
            for v in (self.gpde, self.interface_gpde, self.interface_pde_min, self.rankable)
        )


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
    when ``pae_matrix_uri`` has not been hydrated yet.

    ⛔ To fetch it, call ``client.folds.download_pae(peptide.fold_id)``. It takes
    ``ptf_fold_results.id`` — the fold ROW primary key — NOT this object's
    :attr:`job_id`. ``FoldResult`` carries no integer PK, so look the design up
    via ``client.peptides.search(...)`` and use the ``fold_id`` from there."""
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
    """Complex-level iPSAE, dominated by receptor-self confidence on multimeric
    targets — it stays high even when the peptide is not engaging. NOT the binder
    metric; prefer :attr:`peptide_ipsae`. Retained for backward compatibility."""
    peptide_ipsae: float | None = Field(default=None, alias="peptideIpsae")
    """Peptide-receptor interface iPSAE — THE binder metric for Boltz-2 peptide
    binders. ``None`` means the value was never computed (common on the
    predict-batch path), NOT zero — check :attr:`peptide_ipsae_status` and do not
    fall back to :attr:`ipsae`, which measures a different thing."""
    peptide_ipsae_status: str | None = Field(default=None, alias="peptideIpsaeStatus")
    """'ok' when :attr:`peptide_ipsae` is present; 'not_computed' when it is NULL."""
    quality_tier: QualityTier | None = Field(default=None, alias="qualityTier")
    """Server-computed acceptance-gate verdict for this fold, when the endpoint that
    produced this :class:`FoldResult` includes it. ``None`` on endpoints that don't
    (this field is populated on the ``peptides.search()`` / ``peptides.list()`` path
    today; treat it as forward-compatible elsewhere). See :class:`QualityTier` for the
    unit traps -- and, in particular, that ``require_ensemble=True`` does NOT currently
    yield a co-validated pass on this platform (bd-LIGANDAI_ALPHA_V2-xv5ih)."""
    plddt: float | None = None
    ptm: float | None = None
    ipae: float | None = None
    """Per-interface predicted aligned error (chain-pair). Populated when the
    fold writer ran with schema_version >= 2; nullable for older runs."""

    # ── PDE (predicted distance error), migration 170 ────────────────────────
    # Flat wire fields, mirroring how the server emits them. ⛔ Do not read one
    # of these numbers without the three tags below it — prefer the bundled
    # :attr:`pde` accessor, which cannot hand you a value without its
    # provenance. See :class:`PdeScores` for why an untagged PDE is meaningless.
    gpde: float | None = None
    """Complex-level contact-probability-weighted mean PDE, Angstroms.
    Interpretable only alongside :attr:`pde_engine` / :attr:`pde_kind` /
    :attr:`pde_scale`."""
    interface_gpde: float | None = Field(default=None, alias="interfaceGpde")
    """gPDE restricted to binder<->receptor token pairs, Angstroms."""
    interface_pde_min: float | None = Field(default=None, alias="interfacePdeMin")
    """MIN interface PDE, Angstroms — LOWER IS BETTER, and the PDE scalar to
    rank on (AUROC 0.806 on the 199-fold labelled holdout vs 0.755 for the
    interface mean). ``None`` means "not computed" and is NEVER back-filled from
    :attr:`gpde` or :attr:`interface_gpde` — those measure the whole complex, so
    substituting one would report a different quantity under this name."""
    pde_rankable: float | None = Field(default=None, alias="pdeRankable")
    """Cross-engine-safe PDE: equals :attr:`interface_pde_min` only when
    ``pde_kind == 'native'`` and ``pde_scale == 'angstrom'``, else ``None``.
    Any ranking that mixes fold engines must key on THIS field."""
    pde_engine: str | None = Field(default=None, alias="pdeEngine")
    """FoldEngineId that produced the PDE value."""
    pde_kind: str | None = Field(default=None, alias="pdeKind")
    """``'native'`` (engine's own token_pair_pde head) or ``'proxy'``
    (reconstructed from PAE or another surrogate)."""
    pde_scale: str | None = Field(default=None, alias="pdeScale")
    """``'angstrom'`` (verified) or ``'unverified'`` (Boltz-2's
    complex_pde/complex_ipde — scale never established against Angstrom-scale
    gPDE, so it must not be compared to another engine's PDE)."""
    pde_interpretation: str | None = Field(default=None, alias="pdeInterpretation")
    """``'lower_is_better'`` — stated on the wire so the direction never has to
    be inferred from the field name."""
    pde_units: str | None = Field(default=None, alias="pdeUnits")
    """``'angstrom'`` | ``'unverified'`` | ``None``."""

    @property
    def pde(self) -> PdeScores | None:
        """PDE bundled with its provenance tags, or ``None`` if no PDE was
        reported.

        This is the accessor to reach for: it is impossible to obtain a PDE
        number from it without also holding the engine / kind / scale that say
        whether the number may be compared to anything.

        >>> fold.pde.cross_engine_rank_value   # safe across engines, may be None
        >>> fold.pde.interface_pde_min         # raw; single-engine use only
        """
        block = PdeScores(
            gpde=self.gpde,
            interfaceGpde=self.interface_gpde,
            interfacePdeMin=self.interface_pde_min,
            pdeRankable=self.pde_rankable,
            pdeEngine=self.pde_engine,
            pdeKind=self.pde_kind,
            pdeScale=self.pde_scale,
            pdeInterpretation=self.pde_interpretation,
            pdeUnits=self.pde_units,
        )
        return block if block else None

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
    # score-pdb when the caller passes fold_*).
    #
    # ⛔ METRIC GUIDANCE for peptide binders:
    #   peptide_ipsae  — peptide-receptor interface iPSAE. THIS is the binder metric.
    #                    NULL means the column was not computed (fold_batch path); check
    #                    peptide_ipsae_status for 'ok' vs 'not_computed'.
    #   ipsae          — complex-level iPSAE dominated by receptor-self confidence.
    #                    Stays high even when the peptide is not engaging. NOT a binder
    #                    metric; retained for backward compatibility only.
    #   iptm / ptm     — whole-complex confidence dominated by the (large) receptor chain.
    #                    Not a reliable binder-quality metric on its own.
    iptm: float | None = None
    ptm: float | None = None
    ipsae: float | None = None
    peptide_ipsae: float | None = Field(default=None, alias="peptide_ipsae")
    """Peptide-receptor interface iPSAE — THE binder metric for Boltz-2 peptide binders.
    NULL when not yet computed (fold_batch path); check ``peptide_ipsae_status``."""
    peptide_ipsae_status: str | None = Field(default=None, alias="peptide_ipsae_status")
    """'ok' when ``peptide_ipsae`` is present; 'not_computed' when the column is NULL
    (common for folds run via predict-batch, which skips the peptide iPSAE step)."""
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


class DevelopabilityResult(_LGModel):
    """Three developability grades for one peptide sequence.

    Returned by :meth:`ligandai.resources.peptides.Peptides.score_developability`
    — a standalone rescore (NO regen / NO refold) of already-generated peptides.

    The nested ``stability_scores`` / ``immuno_scores`` dicts carry the full
    grade detail (cleavage sites, MHC epitope counts, half-life, etc.); the
    top-level convenience fields mirror the headline grades. Half-life lives
    inside ``stability_scores`` and is also surfaced as ``predicted_halflife_*``.
    """

    sequence: str
    stability_grade: str | None = Field(default=None, alias="stability_grade")
    immuno_grade: str | None = Field(default=None, alias="immuno_grade")
    predicted_halflife_hours: float | None = Field(default=None, alias="predicted_halflife_hours")
    predicted_halflife_min: float | None = Field(default=None, alias="predicted_halflife_min")
    stability_scores: dict[str, Any] | None = Field(default=None, alias="stability_scores")
    immuno_scores: dict[str, Any] | None = Field(default=None, alias="immuno_scores")


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
    """One bivalent design session.

    ``target1`` / ``target2`` / ``linker`` are ``None`` when the server did not
    send them — a ``GET /api/ligandforge/bivalent`` list row carries only
    ``target1_gene`` / ``target2_gene`` (and ``target2_gene`` is ``null`` until the
    second target is chosen) and no linker. They used to be required and were
    back-filled with a gene named ``"unknown"`` and a default linker. Read
    ``get_session(id)`` for the full record. [bd-dre-3imtv.4]
    """

    id: str
    target1: BivalentTarget | None = None
    target2: BivalentTarget | None = None
    linker: LinkerConfig | None = None
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
    """``GET /api/protein-info/{gene}``.

    ⛔ ``gene`` was required under that name only, and the route answers
    ``geneName`` (``{"geneName": "EGFR", "uniprotId": "P00533", "source":
    "receptor_database", "proteinName": ..., "tmHelices": 1}``), so
    ``proteins.info()`` raised ``ValidationError: gene Field required`` on every
    gene. Measured 2026-10-02 against blue. [bd-dre-3imtv.4]
    """

    gene: str = Field(validation_alias=AliasChoices("gene", "geneName", "gene_name"))
    protein_name: str | None = Field(default=None, alias="proteinName")
    source: str | None = None
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
    """``GET /api/receptor-topology/{gene}``.

    ⛔ The typed fields below were authored against a flat shape the route does
    not send. It answers ``{"success": true, "gene": "EGFR", "uniprotId":
    "P00533", "has_topology": true, "chain_topologies": {"A": {"extracellular":
    [[25, 645]], "transmembrane": [[646, 668]], "cytoplasmic": [[669, 1210]],
    "signal_peptide": []}}}``, so ``topology.extracellular`` was ``None`` for a
    receptor whose extracellular span the server had just reported — the data
    was on the object, only under ``chain_topologies``. The flat fields are now
    derived from it (every chain, each range tagged with its chain; the first
    signal peptide); ``chain_topologies`` stays readable verbatim. When the
    server reports ``has_topology: false`` they stay ``None`` — unknown, not
    empty. Measured 2026-10-02 against blue. [bd-dre-3imtv.4]
    """

    gene: str
    tm_regions: list[ResidueRange] | None = Field(default=None, alias="tmRegions")
    extracellular: list[ResidueRange] | None = None
    intracellular: list[ResidueRange] | None = None
    signal_peptide: ResidueRange | None = Field(default=None, alias="signalPeptide")

    @model_validator(mode="before")
    @classmethod
    def _derive_from_chain_topologies(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        topo = data.get("chain_topologies")
        if not isinstance(topo, dict) or not topo or data.get("has_topology") is False:
            return data
        flat_keys = (
            "tmRegions", "tm_regions", "extracellular", "intracellular",
            "signalPeptide", "signal_peptide",
        )
        if any(k in data for k in flat_keys):
            return data

        def ranges(*names: str) -> list[dict[str, Any]]:
            out: list[dict[str, Any]] = []
            for chain, spans in topo.items():
                if not isinstance(spans, dict):
                    continue
                for name in names:
                    for pair in spans.get(name) or []:
                        if isinstance(pair, (list, tuple)) and len(pair) == 2:
                            out.append({"chain": str(chain), "start": pair[0], "end": pair[1]})
            return out

        derived = dict(data)
        derived["tm_regions"] = ranges("transmembrane")
        derived["extracellular"] = ranges("extracellular")
        derived["intracellular"] = ranges("cytoplasmic", "intracellular")
        signal = ranges("signal_peptide")
        derived["signal_peptide"] = signal[0] if signal else None
        return derived


class ReceptorIntelligence(_LGModel):
    gene: str
    endocytosis: dict[str, Any] | None = None
    internalization: dict[str, Any] | None = None
    biased_agonism: dict[str, Any] | None = Field(default=None, alias="biasedAgonism")


class ReceptorProfile(_LGModel):
    """Decision-oriented Receptor Intelligence profile
    (``GET /api/receptor-intelligence/profile/{gene}``).

    Returned by :meth:`Proteins.receptor_profile`. Requires an
    **academia / pro / enterprise** plan or higher.

    This is the managed *output* view: where :class:`ReceptorAtlas` hands back the
    raw curated blocks, this reorganises them around the two decisions you actually
    make, and is the surface the website and both SDKs share.

    ``receptor_intelligence`` carries:

    * ``available`` — ``False`` when the target has no row in the receptor-SIGNALING
      atlas (GPCRs, ion channels, nuclear receptors, transporters). That is a
      coverage boundary, **not** an invalid target: ``known_target`` and
      ``receptordb_complexes`` tell you the platform still knows it and it remains
      designable. UMOD is the worked example.
    * ``target_triage`` — BEFORE generation: ``druggability``, ``surface_class``,
      ``signaling_state``, ``modality_hint`` (+ ``modality_rule``, the plain-text
      rule behind it), the resolved ``structure`` (source/pdb_code/tier/chains),
      and ``flags`` — each with a ``code`` and human ``detail``.
    * ``binder_context`` — AFTER folding: ``internalization`` (rate class, fate,
      arrestin/clathrin dependence, PMIDs), ``coupling`` (G proteins, arrestins,
      curation + citations), and ``hotspot_residues``.
    * ``provenance`` — atlas build metadata and coverage count.

    Absent blocks mean *no data*, never *no risk*. Only ``modality_hint`` and
    ``flags`` are derived; both ship the rule text so they can be audited.
    """

    gene: str
    receptor_intelligence: dict[str, Any] | None = None
    structure: dict[str, Any] | None = None
    suggestions: list[str] | None = None


class ReceptorAtlas(_LGModel):
    """Deterministic Receptor Intelligence atlas overlay
    (``GET /api/receptor-intelligence/atlas/{gene}`` and ``.../full``).

    Returned by :meth:`Proteins.receptor_atlas`. Every block is optional — the
    atlas silently omits any block it has no curated data for, so a sparse target
    yields a sparse model. ``extra="allow"`` (inherited from :class:`_LGModel`)
    preserves any additional server blocks as raw attributes.

    Two tiers map to one model:

    * ``full=False`` → free/any-authenticated hard-data overlay: ``identity``,
      ``signaling_state``, ``cell_surface``, ``coupling``, ``bias_profile``,
      ``engagements``, ``internalization``, ``rotamer_summary``,
      ``atlas_coverage_count``.
    * ``full=True`` → SUPERADMIN overlay: everything above PLUS the ndLF /
      nanoGPT supervised-head ``model_predictions`` (22+ heads), the full
      ``intracellular_partners`` list, ``trigger_profile``, ``phospho_bias_proxy``,
      ``residue_functional_annotation``, ``rotamer_full`` chi1 rows,
      ``tumbler_signature``, the atlas-vs-model ``disagreements`` panel, and raw
      ``atlas_coverage`` provenance.

    bd-LIGANDAI_ALPHA_V2-q3z1b.
    """

    gene: str
    # --- Free / hard-data blocks (omitted when not curated) ---
    identity: dict[str, Any] | None = None
    signaling_state: str | None = None
    cell_surface: dict[str, Any] | None = None
    coupling: dict[str, Any] | None = None
    bias_profile: dict[str, Any] | None = None
    engagements: dict[str, Any] | None = None
    internalization: dict[str, Any] | None = None
    rotamer_summary: dict[str, Any] | None = None
    atlas_coverage_count: int | None = None
    # --- Superadmin (full=True) ndLF / nanoGPT model + deep-atlas blocks ---
    model_predictions: dict[str, Any] | None = None
    intracellular_partners: list[dict[str, Any]] | None = None
    intracellular_partners_count: int | None = None
    trigger_profile: dict[str, Any] | None = None
    phospho_bias_proxy: dict[str, Any] | None = None
    # The live full-atlas endpoint returns this as a dict wrapper
    # ({"annotations": [...], "keyed_by": ..., "source_atlas": ...}); older
    # builds emit a bare list of rows. Accept both so the SDK validates against
    # any backend revision.
    residue_functional_annotation: list[dict[str, Any]] | dict[str, Any] | None = None
    rotamer_full: dict[str, Any] | None = None
    tumbler_signature: dict[str, Any] | None = None
    atlas_coverage: list[Any] | None = None
    disagreements: list[str] | None = None
    has_disagreements: bool | None = None


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
    """A DiseaseViewer row.

    ⛔ ``name`` and ``variant``-style required fields were authored against a
    GUESSED row shape. Measured live 2026-10-02:
    ``GET /api/disease-viewer/search`` rows carry ``condition`` (the human
    name), ``gene``, ``inheritance``, ``age_group`` and the organ/tissue match
    sets — and no ``name`` at all, so validating one raised
    ``ValidationError`` the moment the envelope fix let real rows through.
    ``name`` therefore reads ``condition`` as well, and is optional.
    Every unmodelled field is still reachable: this base allows extras.
    [bd-dre-3imtv.3]
    """

    id: int
    name: str | None = Field(
        default=None, validation_alias=AliasChoices("name", "condition")
    )
    gene: str | None = None
    category: str | None = None
    summary: str | None = None
    gene_count: int | None = Field(default=None, alias="geneCount")


class Mutation(_LGModel):
    """A ClinVar-derived mutation row.

    ⛔ ``variant`` was required and the live rows do not have it: measured
    2026-10-02, ``GET /api/disease-viewer/gene-mutations/EGFR`` answers rows
    keyed ``hgvs_genomic`` / ``hgvs_coding`` / ``hgvs_protein`` / ``rs_id`` /
    ``variant_type``, so ``diseases.gene_mutations()`` raised
    ``ValidationError`` on row 1 of 100 — the method was unusable, not merely
    lossy. It is optional now, and the HGVS/rsID fields stay readable as
    extras. ``consequence`` also accepts the server's
    ``molecular_consequence``. [bd-dre-3imtv.3]
    """

    gene: str
    variant: str | None = None
    consequence: str | None = Field(
        default=None,
        validation_alias=AliasChoices("consequence", "molecular_consequence"),
    )
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

    ``id`` and ``position`` are **optional** for callers — when a list of
    segments is assembled into a :class:`SegmentConfig` (or passed via the
    ``segments=`` convenience kwarg on :meth:`Peptides.generate`), any segment
    that left them unset is auto-assigned from its order in the list
    (``position = index``, ``id = f"seg{index}"``). Both stay overridable; if
    you set them explicitly they are preserved. This means a caller can write::

        PeptideSegment(type="premade", sequence="SNRFTCREGYL")

    without hand-numbering. ``type="generated"`` is accepted as an alias for
    ``"binding"`` (the most common diffusion-generated segment intent).
    """

    id: str | None = None
    type: Literal["binding", "linker", "stability", "premade"]
    position: int | None = None
    sequence: str | None = None
    length_range: tuple[int, int] | None = Field(default=None, alias="lengthRange")
    label: str | None = None
    locked: bool = False

    @model_validator(mode="before")
    @classmethod
    def _normalize_type(cls, data: Any) -> Any:
        """Accept ``type="generated"`` as an alias for ``"binding"``.

        ``"generated"`` is a natural word for "a diffusion-generated segment",
        but the platform's segment vocabulary is binding/linker/stability/premade.
        Map it to ``"binding"`` (the binding-objective generated segment) so the
        alias does not raise a ``ValidationError``.
        """
        if isinstance(data, dict):
            t = data.get("type")
            if t == "generated":
                data = {**data, "type": "binding"}
        return data


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

    @model_validator(mode="after")
    def _assign_segment_order(self) -> "SegmentConfig":
        """Auto-assign ``position``/``id`` from each segment's order in the list.

        Callers may build segments without hand-numbering::

            SegmentConfig(mode="custom", segments=[
                PeptideSegment(type="premade", sequence="SNRFTCREGYL"),
                PeptideSegment(type="binding", length_range=(9, 39)),
            ])

        Any segment that left ``position``/``id`` unset is filled from its index
        (``position = index``, ``id = f"seg{index}"``). Explicit values are kept,
        so existing hand-numbered callers are unaffected.
        """
        for index, segment in enumerate(self.segments):
            if segment.position is None:
                segment.position = index
            if segment.id is None:
                segment.id = f"seg{index}"
        return self


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
    """One episodic-memory row.

    ⛔ ``memory_type`` read ONLY ``memoryType``; the live rows
    (``/api/episodic-memory/search`` and ``/list``, measured 2026-10-02) key it
    ``type``, so a real row failed validation as soon as the envelope fix
    delivered one. ``memoryType`` is still accepted, and still used on the way
    out (``save()`` sends it). [bd-dre-3imtv.3]
    """

    id: str | int
    title: str | None = None
    content: str
    memory_type: str = Field(
        alias="memoryType",
        validation_alias=AliasChoices("memoryType", "memory_type", "type"),
    )
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

    @property
    def ref(self) -> int | str | None:
        """The identifier to pass back to ``client.programs.*``.

        ``GET /api/ptf/programs`` lists a program with only its string handle
        (``program_<ts>_<hash>``) when it has no numeric database id, so ``id`` is
        ``None`` for those rows. The server resolves either form on every
        ``/api/ptf/programs/{id}...`` route; this returns whichever the row has.
        """
        return self.id if self.id is not None else self.program_id


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
    """One generation/fold session row.

    ⛔ ``id`` was required under that name only, and the server never sends it:
    ``GET /api/ptf/sessions`` rows are keyed ``session_id`` (snake) with
    ``created_at``, ``GET /api/ptf/sessions/by-gene/{gene}`` rows are keyed
    ``sessionId`` with ``createdAt``, and ``GET /api/ptf/sessions/{id}`` is
    keyed ``session_id`` inside a ``session`` envelope. So ``list_sessions()``,
    ``get_session()`` and ``find_session_by_gene()`` all raised ``ValidationError
    : id Field required`` on every live row. Measured 2026-10-02 against blue.
    [bd-dre-3imtv.4]
    """

    id: str = Field(validation_alias=AliasChoices("id", "sessionId", "session_id"))
    gene: str | None = None
    program_id: int | str | None = Field(
        default=None, alias="programId", validation_alias=AliasChoices("programId", "program_id")
    )
    workstream_id: int | None = Field(default=None, alias="workstreamId")
    status: str | None = None
    created_at: datetime | None = Field(
        default=None, alias="createdAt", validation_alias=AliasChoices("createdAt", "created_at")
    )


class SessionDetail(Session):
    peptide_count: int | None = Field(default=None, alias="peptideCount")
    fold_count: int | None = Field(default=None, alias="foldCount")
    parameters: dict[str, Any] | None = None


# -- Program sharing -----------------------------------------------------------
#
# A collaborator (e.g. an external academic partner) can be granted access to
# a program you own without becoming its owner. Two mechanisms feed the same
# underlying grant: a direct/org **share** (you already know their account or
# organization) and an email **invite** (you only have an address; accepting
# it creates the share). :class:`ProgramAccess` is the single call that tells
# ANY grantee — owner, org member, or share recipient — what they can do.


#: Permission level grantable on a shared program, from least to most access.
ProgramPermission = Literal["view", "comment", "edit"]

#: The caller's own access rank on a program — :data:`ProgramPermission` plus
#: ``"owner"`` (the program's creator, or a platform admin).
EffectiveProgramAccess = Literal["view", "comment", "edit", "owner"]

#: How an :data:`EffectiveProgramAccess` grant was derived. ``org_owner`` is
#: membership in the program's own organization (pre-existing platform
#: semantics, not something :meth:`~ligandai.resources.programs.Programs.share`
#: creates); ``user_share`` / ``org_share`` come from an explicit grant.
ProgramAccessProvenance = Literal["owner", "org_owner", "user_share", "org_share", "admin"]


class ProgramShare(_LGModel):
    """One grant row, as returned by
    :meth:`~ligandai.resources.programs.Programs.shares` (list) and
    :meth:`~ligandai.resources.programs.Programs.share` /
    :meth:`~ligandai.resources.programs.Programs.update_share` (single row).

    Exactly one of :attr:`shared_with_user_id` / :attr:`shared_with_org_id` is
    set — the platform enforces this as a database constraint. Only the
    program's owner can list, create, or modify these rows; every method that
    returns a :class:`ProgramShare` raises
    :class:`~ligandai.errors.LigandAIForbidden` (HTTP 403) for anyone else.
    """

    id: int
    program_id: int | None = Field(default=None, alias="programId")
    shared_with_user_id: str | None = Field(default=None, alias="sharedWithUserId")
    shared_with_user_email: str | None = Field(default=None, alias="sharedWithUserEmail")
    shared_with_org_id: int | None = Field(default=None, alias="sharedWithOrgId")
    shared_with_org_name: str | None = Field(default=None, alias="sharedWithOrgName")
    permission: ProgramPermission | str
    shared_by: str | None = Field(default=None, alias="sharedBy")
    created_at: datetime | None = Field(default=None, alias="createdAt")
    updated_at: datetime | None = Field(default=None, alias="updatedAt")


class SharedProgram(Program):
    """A :class:`Program` returned by
    :meth:`~ligandai.resources.programs.Programs.shared_with_me`, annotated
    with the caller's own grant on it. Programs the caller owns outright are
    not included here — see :meth:`~ligandai.resources.programs.Programs.list`
    for those.
    """

    permission: ProgramPermission | str | None = None
    provenance: ProgramAccessProvenance | str | None = None
    shared_by: str | None = Field(default=None, alias="sharedBy")
    shared_by_email: str | None = Field(default=None, alias="sharedByEmail")
    shared_at: datetime | None = Field(default=None, alias="sharedAt")


class ProgramAccessCapabilities(_LGModel):
    """Convenience capability flags for a :class:`ProgramAccess` — the only
    thing a caller should branch on. A share recipient never gets
    :attr:`reshare`, :attr:`delete`, or :attr:`transfer` regardless of
    permission level; only the owner (or a platform admin) administers a
    program. ``edit`` means "edit content", not "administer the program".
    """

    read: bool = False
    comment: bool = False
    mutate: bool = False
    reshare: bool = False
    delete: bool = False
    spend_credits: bool = Field(default=False, alias="spendCredits")
    transfer: bool = False


class ProgramAccess(_LGModel):
    """The caller's own effective access on a program, from
    :meth:`~ligandai.resources.programs.Programs.access`.

    Works for ANY grantee — owner, org member, or share recipient — so it is
    the one call a collaborator can make to find out what they're allowed to
    do with a program before trying it. The server intentionally reports
    "not found" (:class:`~ligandai.errors.LigandAINotFoundError`) rather than
    "forbidden" when the caller has no grant at all, so a stranger can't use
    this call to confirm a program id exists.
    """

    program_id: int | None = Field(default=None, alias="programId")
    effective: EffectiveProgramAccess | str
    provenance: ProgramAccessProvenance | str
    share_id: int | None = Field(default=None, alias="shareId")
    granted_by: str | None = Field(default=None, alias="grantedBy")
    can: ProgramAccessCapabilities = Field(default_factory=ProgramAccessCapabilities)


class ProgramInvite(_LGModel):
    """A pending program-share invite row, from
    :meth:`~ligandai.resources.programs.Programs.invites`.

    Never carries the raw invite token or URL — only
    :class:`ProgramInviteCreated` does, and only once, immediately after
    :meth:`~ligandai.resources.programs.Programs.invite` creates it. The
    token is stored server-side only as a one-way hash, so there is no "show
    me the link again" call.
    """

    id: int
    program_id: int | None = Field(default=None, alias="programId")
    email: str
    permission: ProgramPermission | str
    invited_by: str | None = Field(default=None, alias="invitedBy")
    created_at: datetime | None = Field(default=None, alias="createdAt")
    expires_at: datetime | None = Field(default=None, alias="expiresAt")
    accepted_at: datetime | None = Field(default=None, alias="acceptedAt")
    revoked_at: datetime | None = Field(default=None, alias="revokedAt")


class ProgramInviteCreated(ProgramInvite):
    """Response of :meth:`~ligandai.resources.programs.Programs.invite` — the
    one and only time the invite URL (and the raw token embedded in it) is
    returned. Relay it to the invitee and discard it; it cannot be retrieved
    again via :meth:`~ligandai.resources.programs.Programs.invites`.
    """

    invite_url: str | None = Field(default=None, alias="inviteUrl")


class ProgramInviteAcceptResult(_LGModel):
    """Response of :meth:`~ligandai.resources.programs.Programs.accept_invite`."""

    success: bool = True
    program_id: int | None = Field(default=None, alias="programId")
    share_id: int | None = Field(default=None, alias="shareId")
    permission: ProgramPermission | str | None = None
    program: Program | None = None


# -- Charts / reports --------------------------------------------------------


class Chart(_LGModel):
    """One server-rendered chart.

    ⛔ Three different server surfaces emit this shape and NONE of them emits
    ``id``, ``imageUrl`` or ``chartType``: ``POST /api/charts/generate`` returns
    ``{success, chart_id, url, svg_url, file_path}``, ``GET /api/charts/{id}``
    returns ``{chartId, url, format}``, and a ``/api/charts/list`` row carries
    ``{chart_id, chart_type, title, formats, file_paths, program_id, created_at}``.
    The alias sets below cover all three; before they existed every
    ``client.charts.*`` call raised ``ValidationError`` on a perfectly good
    response. [bd-dre-mc8jm]
    """

    id: str = Field(validation_alias=AliasChoices("id", "chart_id", "chartId"))
    chart_type: str | None = Field(
        default=None, validation_alias=AliasChoices("chart_type", "chartType")
    )
    title: str | None = None
    image_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("image_url", "imageUrl", "url"),
    )
    """PNG (or, for a ``format="svg"`` render, SVG) URL. The server names this
    field ``url``; ``image_url`` is the SDK's stable name for it."""
    svg_url: str | None = Field(
        default=None, validation_alias=AliasChoices("svg_url", "svgUrl")
    )
    """Only populated when the chart was rendered with ``format="svg"`` or
    ``format="both"`` — a PNG-only render has no SVG to point at."""
    created_at: datetime | None = Field(
        default=None, validation_alias=AliasChoices("created_at", "createdAt")
    )


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


# -- Species / organism targeting ---------------------------------------------


class SpeciesEntitlement(_LGModel):
    """Result of ``GET /api/cross-species/entitlement``.

    Mirrors the server response shape. ``entitled`` is the single bit that
    decides whether a ``mouse`` species selection is honored; the client uses it
    to fail closed (coerce mouse -> human) when the key is not entitled.

    On any network/parse/HTTP error the SDK constructs a not-entitled instance
    (human only) so an unavailable endpoint never grants a non-default species.
    """

    success: bool = True
    capability: str = "species_targeting"
    entitled: bool = False
    is_super_admin: bool = Field(alias="isSuperAdmin", default=False)
    org_granted: bool = Field(alias="orgGranted", default=False)
    species: list[str] = Field(default_factory=lambda: ["human"])
    default_species: str = Field(alias="defaultSpecies", default="human")
