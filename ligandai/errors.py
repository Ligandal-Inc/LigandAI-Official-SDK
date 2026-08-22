# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Typed exception hierarchy for the LIGANDAI SDK.

All errors raised by the SDK subclass :class:`LigandAIError`. Server-side errors
include a `code` (e.g. ``"E004"``), `message`, and `request_id` from the response.
"""

from __future__ import annotations

from typing import Any


class LigandAIError(Exception):
    """Base class for all SDK errors.

    Attributes
    ----------
    message : str
        Human-readable error message.
    code : str | None
        Server-defined error code (e.g. ``"E001"``, ``"E004"``).
    status_code : int | None
        HTTP status code, when applicable.
    request_id : str | None
        Server-issued request id from ``x-request-id`` header (if present).
    response : dict | None
        Raw response payload, when available.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status_code: int | None = None,
        request_id: str | None = None,
        response: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code
        self.request_id = request_id
        self.response = response

    def __repr__(self) -> str:
        parts = [f"message={self.message!r}"]
        if self.code:
            parts.append(f"code={self.code!r}")
        if self.status_code is not None:
            parts.append(f"status_code={self.status_code}")
        if self.request_id:
            parts.append(f"request_id={self.request_id!r}")
        return f"{type(self).__name__}({', '.join(parts)})"


class LigandAIAuthError(LigandAIError):
    """401 — invalid, expired, or revoked API key."""


class LigandAITierError(LigandAIError):
    """403 — feature requires a higher subscription tier.

    Attributes
    ----------
    current_tier : str | None
        The tier of the API key that was used.
    required_tier : str | None
        The minimum tier required to access the feature.
    """

    def __init__(
        self,
        message: str,
        *,
        current_tier: str | None = None,
        required_tier: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.current_tier = current_tier
        self.required_tier = required_tier


class LigandAIRateLimitError(LigandAIError):
    """429 — rate limit exceeded.

    Attributes
    ----------
    retry_after : float | None
        Seconds to wait before retrying, parsed from ``Retry-After`` or
        ``X-RateLimit-Reset`` headers.
    """

    def __init__(
        self,
        message: str,
        *,
        retry_after: float | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.retry_after = retry_after


class LigandAICreditError(LigandAIError):
    """402 — insufficient credits for the operation.

    Attributes
    ----------
    required : int | None
        Credits needed for this operation.
    available : int | None
        Credits currently available on the account.
    shortfall : int | None
        Credits the user needs to top up (= max(0, required - available)).
        Surfaced from the server payload when present; otherwise computed
        client-side when both ``required`` and ``available`` are known.
    recovery_url : str | None
        Path/URL the caller should open in a browser to top up. Servers
        return a relative path (``/account/billing?recovery=...``); callers
        can prefix the base URL when displaying.
    top_up_usd : int | None
        Suggested top-up amount in US dollars (server-computed). 100 credits
        = $0.01, so a 1,000-credit shortfall rounds to a $0.10 top-up but the
        server typically suggests a higher round dollar amount to cover the
        next batch of jobs.
    upgrade_url : str | None
        Pricing page URL when an upgrade would be more cost-effective than
        a top-up. Optional — most insufficient-credit responses focus on
        top-up rather than tier change.

    Example — handling mid-loop credit exhaustion::

        try:
            job = client.peptides.fold_batch(peptides=peps, target_gene="EGFR")
        except LigandAICreditError as e:
            print(f"Need {e.shortfall:,} more credits (${e.top_up_usd}).")
            print(f"Top up at: {e.recovery_url}")
    """

    def __init__(
        self,
        message: str,
        *,
        required: int | None = None,
        available: int | None = None,
        shortfall: int | None = None,
        recovery_url: str | None = None,
        top_up_usd: int | None = None,
        upgrade_url: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.required = required
        self.available = available
        # Best-effort derivation: if the server didn't send a shortfall but
        # we know both required and available, compute it. Never negative.
        if shortfall is None and required is not None and available is not None:
            shortfall = max(0, int(required) - int(available))
        self.shortfall = shortfall
        self.recovery_url = recovery_url
        self.top_up_usd = top_up_usd
        self.upgrade_url = upgrade_url


class LigandAIPaidTierRequired(LigandAIError):
    """402 — the SDK requires a paid subscription.

    Distinct from :class:`LigandAICreditError` (which means "your tier is fine
    but you've run out of credits"). This error means the **API key itself**
    resolves to a tier that does not include the requested paid surface.

    The server may return this on `/api/v1/peptides/*` requests from a
    free-tier key with::

        HTTP 402 Payment Required
        {"error": "upgrade_required",
         "message": "...",
         "tier_required": "basic",
         "current_tier": "free"}

    Visit https://ligandai.com/pricing to upgrade.

    Attributes
    ----------
    current_tier : str | None
        The tier of the API key in use.
    required_tier : str | None
        The minimum tier that grants access to the requested paid surface.
    upgrade_url : str
        URL the user should visit to upgrade their plan.
    """

    def __init__(
        self,
        message: str = (
            "This SDK requires a paid subscription. "
            "Visit https://ligandai.com/pricing to upgrade."
        ),
        *,
        current_tier: str | None = None,
        required_tier: str | None = "basic",
        upgrade_url: str = "https://ligandai.com/pricing",
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.current_tier = current_tier
        self.required_tier = required_tier
        self.upgrade_url = upgrade_url


class LigandAIUpgradeRequired(LigandAIPaidTierRequired):
    """402 — alias for :class:`LigandAIPaidTierRequired` exposed in v0.5.0.

    Catches the same server contract (``error: "upgrade_required"``) but is
    named to match the documented public-API behavior. New code should catch
    :class:`LigandAIUpgradeRequired`; legacy code catching
    :class:`LigandAIPaidTierRequired` continues to work because this class
    inherits from it.
    """


class LigandAIForbidden(LigandAIError):
    """403 — request was authenticated but the resource is not accessible.

    Distinct from :class:`LigandAITierError` (which means "your subscription tier
    is below the required level"). This error is raised when access is restricted
    for non-tier reasons such as pilot allowlists, ownership checks, or
    feature flags. The server's ``error_code`` (when present) is exposed as
    :attr:`reason`.

    Attributes
    ----------
    reason : str | None
        Server-provided ``error_code`` (e.g. ``"pilot_restricted"``), if any.
    """

    def __init__(
        self,
        message: str,
        *,
        reason: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.reason = reason


class LigandAILegalAcceptanceRequired(LigandAIForbidden):
    """403 — the authenticated user must accept the Terms of Service and/or
    EULA before this request can proceed.

    Subclasses :class:`LigandAIForbidden` so existing ``except
    LigandAIForbidden`` handlers keep working unchanged. Catch this subclass
    specifically to drive the SDK's interactive resolution flow via
    ``client.legal.resolve_interactively(...)``
    (:meth:`ligandai.resources.legal.Legal.resolve_interactively`).

    Server contract (Contract C, ``docs/clickwrap_legal_acceptance.md``
    §3.6)::

        {
          "error": "Legal acceptance required",
          "code": "LEGAL_ACCEPTANCE_REQUIRED",
          "needsToS": true,
          "needsEULA": true,
          "acceptanceUrl": "https://ligandai.com/legal/accept?return_to=...&channel=sdk_redirect",
          "message": "..."
        }

    The legacy shape emitted by the API-key validators before Contract C
    still maps here for back-compat::

        {"error": "terms_not_accepted", "code": "E014", "action_url": "https://ligandai.com/terms"}

    Attributes
    ----------
    acceptance_url : str | None
        Absolute URL the caller should open in a browser to review and
        accept the current documents. ``None`` only if the server omitted
        it entirely.
    needs_tos : bool
        Whether the outstanding acceptance includes the Terms of Service.
    needs_eula : bool
        Whether the outstanding acceptance includes the EULA.

    Example — headless-safe handling with interactive resolution::

        try:
            job = client.folds.predict(...)
        except LigandAILegalAcceptanceRequired as e:
            # Explicit opt-in: nothing about this happens automatically.
            job = client.legal.resolve_interactively(
                e, retry=lambda: client.folds.predict(...), auto_open_browser=True,
            )
    """

    def __init__(
        self,
        message: str,
        *,
        acceptance_url: str | None = None,
        needs_tos: bool = True,
        needs_eula: bool = True,
        reason: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, reason=reason, **kwargs)
        self.acceptance_url = acceptance_url
        self.needs_tos = needs_tos
        self.needs_eula = needs_eula


class LigandAINotFoundError(LigandAIError):
    """404 — resource (gene, complex, job, etc.) not found."""


class LigandAIServerError(LigandAIError):
    """5xx — the LIGANDAI server hit an internal error.

    These are usually transient. The SDK retries automatically with exponential
    backoff up to ``max_retries`` before raising.
    """


class LigandAIConnectionError(LigandAIError):
    """The request never reached the server — DNS failure, refused connection,
    TLS handshake failure, or a connect/read/write timeout.

    Distinct from :class:`LigandAIServerError` (which means the server *did*
    respond, just with a 5xx). The SDK retries these transparently (same
    policy as 5xx/429) up to ``max_retries``; this is what's raised once
    retries are exhausted, wrapping the underlying ``httpx`` transport
    exception so callers never have to catch a raw ``httpx.TransportError``.

    Attributes
    ----------
    original_error : str | None
        ``repr()`` of the underlying ``httpx`` exception, for debugging.
    """

    def __init__(
        self,
        message: str,
        *,
        original_error: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.original_error = original_error


class LigandAIValidationError(LigandAIError):
    """400 — request payload failed server-side validation.

    Pydantic validation failures raised on the client side are also wrapped
    in this exception.

    Attributes
    ----------
    errors : list[dict] | None
        Per-field validation errors when available.
    """

    def __init__(
        self,
        message: str,
        *,
        errors: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.errors = errors


class LigandAIJobError(LigandAIError):
    """A long-running job (generation/folding) failed on the server.

    Attributes
    ----------
    job_id : str
        The id of the job that failed.
    job_status : str
        Final status, typically ``"failed"`` or ``"cancelled"``.
    """

    def __init__(
        self,
        message: str,
        *,
        job_id: str,
        job_status: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.job_id = job_id
        self.job_status = job_status


class LigandAITimeoutError(LigandAIError):
    """A `.wait()` call exceeded its timeout while polling a job."""


class LigandAIIncompleteResult(LigandAIError):
    """A job server-reported ``status='completed'`` but the result payload is missing
    structural data (no ``pdb_data``/``pdb_content``, ``has_structure=False``, or
    no per-fold result for a batch peptide).

    Raised when ``Job.wait(durable=True)`` is the default — the SDK refuses to
    silently return a "succeeded" Job whose fold-result payload never landed
    (call_id-only acknowledgement stored as result, result callback skipped,
    etc.). Callers can pass ``durable=False`` to opt back into the
    fast-but-permissive behavior.

    Attributes
    ----------
    job_id : str | None
        The id of the job whose result is incomplete.
    job_status : str | None
        Server-reported status (typically ``"completed"`` when this is raised).
    missing_fields : list[str] | None
        Names of fields the SDK expected to be populated. Common values:
        ``"pdb_data"``, ``"pdb_path"``, ``"iptm"``, ``"ipsae"``,
        ``"has_structure"``.
    call_id : str | None
        Compute call id from the partial result payload, if available.
        Callers can use this to manually re-fetch via the recovery endpoint.
    server_state : dict | None
        Snapshot of the last server response, for debugging.
    """

    def __init__(
        self,
        message: str,
        *,
        job_id: str | None = None,
        job_status: str | None = None,
        missing_fields: list[str] | None = None,
        call_id: str | None = None,
        server_state: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.job_id = job_id
        self.job_status = job_status
        self.missing_fields = list(missing_fields) if missing_fields else []
        self.call_id = call_id
        self.server_state = server_state


class LigandAIWaitTimeout(LigandAITimeoutError):
    """``Job.wait(durable=True)`` reached its timeout while still waiting for
    structural data to land — server reported a terminal status but the SDK
    refused to return because ``pdb_data`` / ``has_structure`` were missing.

    Distinct from :class:`LigandAITimeoutError` because the underlying job is
    *not* stuck on the platform side; the durable-success contract is what
    gated the return. The original server state is attached so users can
    inspect what was missing and (if needed) hit the recovery endpoint with
    the captured ``call_id``.

    Attributes
    ----------
    job_id : str | None
        The id of the job that was being waited on.
    job_status : str | None
        Last server-reported status (typically ``"completed"`` or ``"running"``).
    missing_fields : list[str] | None
        Names of fields that were still missing when the deadline elapsed.
    call_id : str | None
        Compute call id, when surfaced by the partial result.
    server_state : dict | None
        Final server response snapshot, for debugging.
    """

    def __init__(
        self,
        message: str,
        *,
        job_id: str | None = None,
        job_status: str | None = None,
        missing_fields: list[str] | None = None,
        call_id: str | None = None,
        server_state: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.job_id = job_id
        self.job_status = job_status
        self.missing_fields = list(missing_fields) if missing_fields else []
        self.call_id = call_id
        self.server_state = server_state


class NotSupportedOnReceptorDB(LigandAIError):
    """Feature is not exposed via the ReceptorDB-restricted client.

    Use :class:`ligandai.LigandAI` with a tier-appropriate API key instead of
    :class:`ligandai.ReceptorDBClient`.
    """


# ─── Wallet / rotating-key exceptions ──────────────────────────────


class WalletEmpty(LigandAIError):
    """The local key wallet has no JWTs remaining and cannot be refreshed.

    This occurs when:
    - All single-use JWTs in ``~/.ligandai/keys.json`` have been consumed,
    - The ``refresh_token`` is absent, expired, or the refresh call failed.

    Recovery: call ``client.mint_wallet(scope=..., target_seq=...)`` to obtain
    a fresh wallet, or pass a valid ``lgai_*_`` API key for the legacy flow.
    """


class WalletExpired(LigandAIError):
    """The wallet's refresh token has passed its 7-day TTL.

    The refresh token embedded in ``~/.ligandai/keys.json`` cannot be used to
    mint more single-use keys. Call ``client.mint_wallet(...)`` to start a new
    wallet chain.
    """


class KeyTargetMismatch(LigandAIError):
    """The cached wallet's ``target_hash`` does not match the requested target.

    The wallet was minted for a different target sequence. Either call
    ``client.mint_wallet(scope=..., target_seq=<new_target>)`` to replace the
    wallet, or pass the same target sequence that was used when the wallet was
    originally minted.

    Attributes
    ----------
    wallet_hash : str | None
        SHA-256 hex digest the wallet was minted for.
    request_hash : str | None
        SHA-256 hex digest computed from the target sequence in this request.
    """

    def __init__(
        self,
        message: str,
        *,
        wallet_hash: str | None = None,
        request_hash: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.wallet_hash = wallet_hash
        self.request_hash = request_hash


class LigandAIScopeError(LigandAIError):
    """The wallet's ``scope`` claim does not match the endpoint being called.

    For example, a wallet minted with ``scope="fold"`` cannot authorize a
    ``generate`` request. Mint a new wallet with the correct scope.

    Attributes
    ----------
    wallet_scope : str | None
        The scope the wallet was minted for.
    required_scope : str | None
        The scope required by the endpoint.
    """

    def __init__(
        self,
        message: str,
        *,
        wallet_scope: str | None = None,
        required_scope: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.wallet_scope = wallet_scope
        self.required_scope = required_scope


# ─── Local-enforcement exceptions ────────────────────


class LigandAIInvalidConfig(LigandAIError):
    """The caller passed a configuration the SDK refuses to forward.

    Raised CLIENT-SIDE before any HTTP call when:

    - ``gpu`` / ``gpu_type`` is anything other than ``"b200_plus"`` (the SDK
      never exposes 2x/4x/8x or smaller GPUs — see ``ALLOWED_GPU_TYPES`` in
      :mod:`ligandai._constants`).
    - ``diffusion_samples`` / ``trajectories`` outside the documented range.
    - Any other invariant the SDK enforces locally to prevent foot-guns.

    Attributes
    ----------
    field : str | None
        The name of the offending kwarg / field.
    value : Any | None
        The value the caller passed.
    allowed : Any | None
        The accepted values (when small / enumerable) or a short description.
    """

    def __init__(
        self,
        message: str,
        *,
        field: str | None = None,
        value: Any | None = None,
        allowed: Any | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.field = field
        self.value = value
        self.allowed = allowed


class LigandAIInsufficientCredits(LigandAIError):
    """Client-side credit pre-flight failed before submission.

    Distinct from :class:`LigandAICreditError`:

    - :class:`LigandAICreditError` is the SERVER's 402 INSUFFICIENT_CREDITS
      after a request reached the wire.
    - :class:`LigandAIInsufficientCredits` is the SDK's local pre-flight check
      — the SDK asks the server for the balance, computes the expected cost
      locally, and rejects the call BEFORE submitting it. This prevents
      cascading 402 responses across a batch loop.

    Attributes
    ----------
    required : int | None
        Estimated credits the batch will consume.
    available : int | None
        Balance read from the server immediately before the check.
    shortfall : int | None
        ``max(0, required - available)``.
    """

    def __init__(
        self,
        message: str,
        *,
        required: int | None = None,
        available: int | None = None,
        shortfall: int | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.required = required
        self.available = available
        if shortfall is None and required is not None and available is not None:
            shortfall = max(0, int(required) - int(available))
        self.shortfall = shortfall


class LigandAIDuplicateSubmission(LigandAIError):
    """A caller tried to re-submit an identical fold/generate call without
    ``force_resubmit=True``, and the local dedupe DB shows a recent submission.

    The SDK normally returns the cached :class:`~ligandai.jobs.Job` handle
    instead of raising — this exception fires only in strict modes (e.g.
    ``return_cached=False``) or when the cached job is missing critical
    metadata.

    Attributes
    ----------
    submission_hash : str | None
        SHA-256 of (peptide_seq + receptor_seq + gpu + params).
    previous_job_id : str | None
        Job ID of the earlier submission.
    previous_status : str | None
        Status the local DB recorded for that earlier submission.
    """

    def __init__(
        self,
        message: str,
        *,
        submission_hash: str | None = None,
        previous_job_id: str | None = None,
        previous_status: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.submission_hash = submission_hash
        self.previous_job_id = previous_job_id
        self.previous_status = previous_status


class LigandAIReusedSessionError(LigandAIError):
    """The SERVER returned an EXISTING session instead of running the generation
    that was asked for, and the request named settings the server's reuse key
    does not look at — so the peptides you would receive were produced by a
    DIFFERENT generator model and/or DIFFERENT guidance than you requested.

    ⛔ THIS IS NOT THE SDK's LOCAL DEDUPE (see
    :class:`LigandAIDuplicateSubmission`, which is about
    ``~/.ligandai/submitted.db``). This one is the platform's own reuse, and it
    has two independent layers, neither of which hashes the generator model or
    any guidance flag:

    * a 60-second idempotency cache keyed on
      ``userId | genes | peptidesPerTarget | conformations | regions |
      pocketIndices | segmentMode | additionalBatch | forceNewSubSession``, and
    * a 2-hour gene reuse keyed on ``computeVariantKey()``, which hashes ONLY the
      TARGETING config (gene, pdb, chains, hotspots, pocket residues, strategy,
      regions, avoid regions, conformations, segment preset, pocket indices).

    Because ``generatorModelId`` and every guidance flag are absent from BOTH
    keys, a second request that changes only the model or only the guidance is
    answered with the FIRST request's session — ``success: true``, a real
    ``sessionId``, real peptides, and a ``deduplicated``/``idempotent`` marker
    that nothing downstream reads. Measured on the deployed platform 2026-08-21:
    40 of 45 model x guidance cells against one gene were all served the SAME
    session, which had been generated by v6.5.21 with no guidance.

    The SDK raises this rather than hand back peptides under a label they did
    not come from. To deliberately accept the existing session, pass
    ``allow_reused_session=True``; to force a genuinely new run, change the
    TARGETING (e.g. ``hotspot_residues=``/``target_residues=``), which is what
    the reuse key actually hashes.

    Attributes
    ----------
    session_id : str | None
        The pre-existing session the server handed back.
    requested_model : str | None
        The generator model this call asked for, which the reuse key ignored.
    guidance_requested : bool
        Whether this call asked for any guidance head, which the reuse key
        also ignored.
    reuse_layer : str | None
        ``"gene_dedup"`` (2 h, variantKey) or ``"idempotency_cache"`` (60 s).
    skipped_genes : list[str]
        Genes the server declined to regenerate.
    """

    def __init__(
        self,
        message: str,
        *,
        session_id: str | None = None,
        requested_model: str | None = None,
        guidance_requested: bool = False,
        reuse_layer: str | None = None,
        skipped_genes: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.session_id = session_id
        self.requested_model = requested_model
        self.guidance_requested = guidance_requested
        self.reuse_layer = reuse_layer
        self.skipped_genes = skipped_genes or []


class LigandAIConcurrencyLimit(LigandAIError):
    """The caller's tier's local concurrency cap is full.

    The SDK tracks in-flight jobs in the local sqlite DB
    (``~/.ligandai/submitted.db``) and refuses to submit when the count of
    ``submitted`` rows in the last
    :data:`~ligandai._constants.DEFAULT_DEDUPE_WINDOW_SECS` for this
    ``api_key_hash`` has reached
    :data:`~ligandai._constants.TIER_GPU_SLOTS` for the caller's tier. The
    platform enforces the same cap; local enforcement just fails faster.

    Attributes
    ----------
    in_flight : int | None
        Count of in-flight submissions tracked locally.
    limit : int | None
        Local cap for the caller's tier.
    """

    def __init__(
        self,
        message: str,
        *,
        in_flight: int | None = None,
        limit: int | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.in_flight = in_flight
        self.limit = limit


# ─── Warning categories ─────────────────────────────────────────────────────


class LigandAIDiagnosticWarning(UserWarning):
    """Base category for every diagnostic the SDK raises on the warning channel
    (bd-LIGANDAI_ALPHA_V2-x4vt8.3.2).

    Filter on this to catch *all* platform diagnostics — client-side targeting
    warnings, server-sent ``diagnostic`` events, and SDK-version advisories —
    without enumerating subclasses::

        import warnings
        warnings.simplefilter("error", LigandAIDiagnosticWarning)

    The structured form of the same information is always available as data:
    ``job.diagnostics`` (a list of :class:`~ligandai.diagnostics.Diagnostic`)
    and ``TargetingValidationResult.diagnostics``. Prefer those when you want
    to branch programmatically; the warning channel exists so a caller who
    never looks still gets told.
    """


class LigandAITargetingWarning(LigandAIDiagnosticWarning):
    """Non-fatal issue with ``target_residues`` / ``avoid_residues`` /
    ``hotspot_residues`` / ``avoid_shell_angstrom`` (bd-LIGANDAI_ALPHA_V2-x4vt8).

    Raised via ``warnings.warn(..., LigandAITargetingWarning)`` for conditions
    that are legal but likely mistakes — e.g. a region given without ``chain``
    in a multi-chain request, ``avoid_shell_angstrom=0`` (packing shell
    disabled), an avoid span at least as large as the target span (likely
    degenerate pocket), or the server reporting a
    ``targeting_warning`` / ``degenerate_pocket_after_avoid`` SSE event mid-run.

    Catch it explicitly to fail CI on a degenerate targeting config::

        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("error", LigandAITargetingWarning)
            client.peptides.generate(gene="EGFR", avoid_residues=[...])
    """


class LigandAIVersionWarning(LigandAIDiagnosticWarning):
    """The server says this SDK build is outdated or below the minimum supported
    version (bd-LIGANDAI_ALPHA_V2-x4vt8.3.2).

    Raised at most once per process, from the ``sdk_outdated`` /
    ``sdk_unsupported`` diagnostics the platform advertises on every response
    via the ``X-LigandAI-SDK-Status`` header family. Set
    ``LIGANDAI_SKIP_VERSION_CHECK=1`` to opt out entirely.

    An outdated client is not cosmetic: older SDK builds are the ones that
    silently drop request parameters the current server expects, which changes
    what your job actually ran without telling you.
    """


def _message_already_surfaces_url(message: str, url: str) -> bool:
    """Whether ``message`` already puts ``url`` in front of the user.

    The server's human-readable ``message`` is REQUIRED to carry the absolute
    acceptance URL in plain text, permanently, because clients pinned to old
    SDK versions (0.8.3 and earlier), ``curl`` and bare ``requests`` only ever
    surface the error string — they know nothing about ``.acceptance_url``.
    Pinned installs persist indefinitely, so this is not a stopgap.

    Consequence: the fold below must be **idempotent**, or every
    current-server 403 shows the URL twice.

    ⛔ THE RULE IS EXACT SUBSTRING CONTAINMENT ON THE RAW URL. Nothing is
    normalised, trimmed, unescaped or canonicalised before the comparison.

    This is a **cross-SDK parity contract**, not a local implementation
    choice: ``docs/clickwrap_legal_acceptance.md`` §3.6 requires the Python
    and Node SDKs to be semantically identical, and the two mappers have been
    verified byte-identical by executing both over the same payload shapes.
    Node's guard is `!message.includes(acceptanceUrl)` — plain containment.
    Any "improvement" here that normalises first (unescaping ``&amp;``,
    stripping a trailing slash, canonicalising the query string) silently
    diverges from Node and reintroduces the doubling on one side only, which
    is the exact bug class the parity contract exists to prevent. Change this
    only by changing both SDKs together.
    """
    return not url or url in message


def error_from_response(
    status_code: int,
    payload: dict[str, Any] | None,
    request_id: str | None = None,
    *,
    retry_after: float | None = None,
) -> LigandAIError:
    """Build the right error subclass for an HTTP response.

    The server's error codes (``E001``-``E013``) map to specific subclasses.
    Falls back to status-code-based dispatch when no code is present.
    """
    payload = payload or {}
    message = (
        payload.get("message")
        or payload.get("error")
        or f"HTTP {status_code}"
    )
    code = payload.get("code")

    common: dict[str, Any] = {
        "code": code,
        "status_code": status_code,
        "request_id": request_id,
        "response": payload,
    }

    if status_code == 401:
        return LigandAIAuthError(message, **common)
    if status_code == 402:
        # Two flavors of 402:
        #   - upgrade_required (paid-tier gate on /api/v1/*) → LigandAIPaidTierRequired
        #   - insufficient credits (per-operation credit check) → LigandAICreditError
        # Discriminate by the `error` field so the SDK raises the most informative
        # subclass; both still satisfy `isinstance(e, LigandAIError)`.
        err_kind = (payload.get("error") or "").lower()
        if err_kind == "upgrade_required" or payload.get("tier_required"):
            return LigandAIUpgradeRequired(
                message,
                current_tier=payload.get("currentTier")
                or payload.get("current_tier"),
                required_tier=payload.get("requiredTier")
                or payload.get("required_tier")
                or payload.get("tier_required")
                or "pro",
                upgrade_url=payload.get("upgradeUrl")
                or payload.get("upgrade_url")
                or "https://ligandai.com/pricing",
                **common,
            )
        # surface the structured insufficient-credits
        # response so callers can render a top-off CTA instead of seeing a
        # bare "Insufficient credits" string. Server contract (matches
        # the platform and the patched /v1/folding/predict-batch
        # response):
        #   {
        #     "code": "INSUFFICIENT_CREDITS",
        #     "creditsRequired": int,
        #     "creditsAvailable": int,
        #     "shortfall": int,
        #     "recoveryUrl": "/account/billing?recovery=insufficient_credits&...",
        #     "upgradeUrl": "https://ligandai.com/pricing",
        #     "topup": <usd int>   // optional
        #   }
        # Older endpoints just send "required" + a free-text message; we
        # accept both shapes for compatibility.
        required_raw = (
            payload.get("creditsRequired")
            or payload.get("credits_required")
            or payload.get("required")
        )
        available_raw = (
            payload.get("creditsAvailable")
            or payload.get("credits_available")
            or payload.get("available")
            or payload.get("currentBalance")
            or payload.get("current_balance")
        )
        recovery_url_raw = (
            payload.get("recoveryUrl")
            or payload.get("recovery_url")
        )
        upgrade_url_raw = (
            payload.get("upgradeUrl")
            or payload.get("upgrade_url")
        )
        top_up_usd_raw = (
            payload.get("topup")
            or payload.get("top_up")
            or payload.get("topupUsd")
            or payload.get("top_up_usd")
        )
        shortfall_raw = (
            payload.get("shortfall")
            or payload.get("creditsShortfall")
            or payload.get("credits_shortfall")
        )
        return LigandAICreditError(
            message,
            required=int(required_raw) if required_raw is not None else None,
            available=int(available_raw) if available_raw is not None else None,
            shortfall=int(shortfall_raw) if shortfall_raw is not None else None,
            recovery_url=str(recovery_url_raw) if recovery_url_raw else None,
            top_up_usd=int(top_up_usd_raw) if top_up_usd_raw is not None else None,
            upgrade_url=str(upgrade_url_raw) if upgrade_url_raw else None,
            **common,
        )
    if status_code == 403:
        # Only synthesize a tier error if the server actually says it's a tier
        # problem. Otherwise (pilot allowlists, ownership checks, legal-acceptance
        # gates) raise an honest LigandAIForbidden carrying the server's message.
        err_kind_403 = (payload.get("error") or "").lower()
        is_tier_error = bool(
            payload.get("requiredTier")
            or payload.get("required_tier")
            or payload.get("tier_required")
            or payload.get("currentTier")
            or payload.get("current_tier")
            or payload.get("upgrade_required")
            or err_kind_403 in {"tier_required", "upgrade_required"}
            or (payload.get("code") in {"BASIC_TIER_REQUIRED", "PRO_TIER_REQUIRED", "ENTERPRISE_TIER_REQUIRED"})
        )
        if is_tier_error:
            return LigandAITierError(
                message,
                current_tier=payload.get("currentTier") or payload.get("current_tier"),
                required_tier=payload.get("requiredTier")
                or payload.get("required_tier")
                or payload.get("tier_required"),
                **common,
            )
        # Legal-acceptance gate (Contract C, docs/clickwrap_legal_acceptance.md
        # §3.6). Detected BEFORE the generic Forbidden fallthrough so callers
        # can catch the specific subclass and drive the browser-redirect +
        # resume flow. Two server shapes in the wild, both accepted:
        #   - Contract C:  code == 'LEGAL_ACCEPTANCE_REQUIRED'
        #                  or error == 'Legal acceptance required'
        #   - legacy:      error == 'terms_not_accepted' or code == 'E014'
        #                  (server/middleware/api-key-validator.ts:281,
        #                   server/enterprise-api-routes.ts:1343,1451)
        is_legal_error = bool(
            code == "LEGAL_ACCEPTANCE_REQUIRED"
            or err_kind_403 == "legal acceptance required"
            or err_kind_403 == "terms_not_accepted"
            or code == "E014"
        )
        if is_legal_error:
            acceptance_url_raw = (
                payload.get("acceptanceUrl")
                or payload.get("acceptance_url")
                # Legacy field name (api-key-validator.ts / enterprise-api-routes.ts).
                or payload.get("action_url")
            )
            acceptance_url = str(acceptance_url_raw) if acceptance_url_raw else None
            needs_tos_raw = payload.get("needsToS")
            if needs_tos_raw is None:
                needs_tos_raw = payload.get("needs_tos")
            needs_eula_raw = payload.get("needsEULA")
            if needs_eula_raw is None:
                needs_eula_raw = payload.get("needs_eula")
            if needs_tos_raw is None and needs_eula_raw is None:
                # Legacy payloads never distinguished which document was
                # outstanding — the legacy gate required both. Default both
                # True rather than silently under-reporting what's needed.
                needs_tos, needs_eula = True, True
            else:
                needs_tos = bool(needs_tos_raw)
                needs_eula = bool(needs_eula_raw)
            # Mirror LigandAICreditError's rich pattern: put the actionable
            # URL directly in the message so it shows up in tracebacks/logs
            # even for callers who never inspect `.acceptance_url`.
            #
            # ⛔ IDEMPOTENT FOLD. The server now emits the absolute acceptance
            # URL inside `message` itself (permanent contract — old pinned
            # clients and curl users only ever see the string). Appending
            # unconditionally would double it. We append ONLY when the URL is
            # absent, and we never rewrite, reorder or strip the server's own
            # wording — the server's phrasing wins and we add at most a
            # suffix. Legacy servers that omit the URL still get one.
            full_message = str(message)
            if acceptance_url and not _message_already_surfaces_url(
                full_message, acceptance_url
            ):
                full_message = f"{full_message} Accept at: {acceptance_url}"
            return LigandAILegalAcceptanceRequired(
                full_message,
                acceptance_url=acceptance_url,
                needs_tos=needs_tos,
                needs_eula=needs_eula,
                **common,
            )
        return LigandAIForbidden(
            message,
            reason=(
                payload.get("error_code")
                or payload.get("code")
                # Fall back to the `error` field (e.g. "payment_method_required",
                # "pilot_restricted") when no explicit code is provided. These are
                # short snake_case kinds, not user-facing prose.
                or payload.get("error")
            ),
            **common,
        )
    if status_code == 404:
        return LigandAINotFoundError(message, **common)
    if status_code == 422 or status_code == 400:
        return LigandAIValidationError(
            message,
            errors=payload.get("errors") or payload.get("details"),
            **common,
        )
    if status_code == 429:
        return LigandAIRateLimitError(
            message,
            retry_after=retry_after,
            **common,
        )
    if 500 <= status_code < 600:
        return LigandAIServerError(message, **common)
    return LigandAIError(message, **common)
