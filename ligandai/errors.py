# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Typed exception hierarchy for the LIGANDAI SDK.

All errors raised by the SDK subclass :class:`LigandAIError`. Server-side errors
include a `code` (e.g. ``"E004"``), `message`, and `request_id` from the response.
"""

from __future__ import annotations

from collections.abc import Mapping
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


class LigandAIEdgeBlocked(LigandAIForbidden):
    """403/503 produced by a CDN / WAF / bot-filter IN FRONT OF the API — the
    request never reached LigandAI at all. [bd-dre-3fduj, bd-dre-2d7za.9]

    Cloudflare Bot Fight Mode answers a non-browser client on a proxied
    hostname with an HTML challenge page carrying HTTP 403. Before this class
    existed the SDK decoded that body as "not JSON", discarded it, and raised
    ``LigandAIForbidden("HTTP 403")`` — byte-identical to a real
    authorization refusal from our own server. Three genuinely different
    causes were therefore indistinguishable at the call site:

    ==========================  =========================================
    cause                       what the caller must actually do
    ==========================  =========================================
    edge block (this class)     call a non-proxied origin, or allowlist
                                the client — the API key is irrelevant
    auth 403 (Forbidden)        fix the key's permissions / ownership
    wrong base_url              point the client at the real API host
    ==========================  =========================================

    Attributes
    ----------
    edge : str | None
        Which intermediary answered (e.g. ``"cloudflare"``), from the
        ``Server`` header or a signature in the body.
    ray_id : str | None
        The edge's own request identifier (``CF-RAY``) — the only id that
        identifies this request to the CDN. Our ``request_id`` is absent
        because our server never saw the request.
    content_type : str | None
        Content type of the challenge body.
    body_excerpt : str | None
        First bytes of the body that was served instead of our JSON. Kept
        verbatim: it is the evidence that an intermediary, not LigandAI,
        produced this response.
    """

    def __init__(
        self,
        message: str,
        *,
        edge: str | None = None,
        ray_id: str | None = None,
        content_type: str | None = None,
        body_excerpt: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.edge = edge
        self.ray_id = ray_id
        self.content_type = content_type
        self.body_excerpt = body_excerpt


class LigandAIUnreadableResponse(LigandAIError):
    """The server answered with a status the SDK accepts but a BODY it cannot
    read — and the SDK refuses to report that as "no data".

    ⛔ THIS IS THE DEFECT CLASS THIS EXCEPTION EXISTS TO MAKE IMPOSSIBLE.
    ``_decode_response`` returns ``None`` for an unparseable body. Every
    resource method in this SDK is written as
    ``... transport.request(...) or {}`` / ``or []``, so a ``None`` became an
    empty dict or an empty list — a measurement of ZERO. A 200 carrying an
    HTML login page, an SPA shell served by a misconfigured proxy, a
    truncated JSON body, or an empty 200 from a route that no longer exists
    ALL produced a clean, plausible, wrong answer: ``proposed: 0``,
    ``designs: []``, ``credits: 0``. A zero from a broken lookup is
    indistinguishable from a zero from an honest measurement, which is how
    a no-op survives unnoticed for weeks.

    Raising is the whole point: "I could not look" must never be spelled the
    same way as "I looked and there was nothing".

    Attributes
    ----------
    cause : str
        Named, machine-checkable reason. One of:

        ``"non_json_content_type"``
            The body is not ``application/json`` — usually an HTML error or
            login page from a proxy, or a ``base_url`` pointing at a web
            front end rather than the API.
        ``"malformed_json"``
            Declared as JSON and is not parseable — truncated or rewritten
            in transit.
        ``"empty_body"``
            Zero-length body where a JSON document was required.
    method : str | None
        HTTP method attempted.
    url : str | None
        Absolute URL attempted.
    content_type : str | None
        The ``Content-Type`` the server actually sent.
    body_excerpt : str | None
        First bytes of the body, verbatim, for diagnosis.
    """

    def __init__(
        self,
        message: str,
        *,
        cause: str,
        method: str | None = None,
        url: str | None = None,
        content_type: str | None = None,
        body_excerpt: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.cause = cause
        self.method = method
        self.url = url
        self.content_type = content_type
        self.body_excerpt = body_excerpt


class LigandAIMutationUnconfirmed(LigandAIError):
    """A ``-> bool`` mutation (cancel / archive / delete) could not be
    confirmed either way, so the SDK refuses to answer ``False``.

    These methods return a plain bool, and ``False`` is read by every caller
    as "the server was asked and it declined". It used to ALSO be what you
    got from a dropped connection, an expired key, a CDN block and a 500 —
    four situations in which the mutation's outcome is simply UNKNOWN and may
    well have been applied. ``job.cancel()`` answering ``False`` after a
    network blip told the user their GPU job was still running when it had in
    fact been cancelled, or vice versa; either way they acted on a fact the
    SDK had invented.

    ``False`` now means exactly one thing: the server was reached, understood
    the request, and declined it. Everything else raises — this class when
    the underlying cause carries no better type, otherwise the original
    typed error (``LigandAIAuthError``, ``LigandAIConnectionError``,
    ``LigandAIServerError``, ``LigandAIEdgeBlocked``, …) straight through.

    Attributes
    ----------
    operation : str | None
        Dotted name of the mutation attempted (e.g. ``"jobs.cancel"``).
    target : str | None
        The id it was attempted against.
    """

    def __init__(
        self,
        message: str,
        *,
        operation: str | None = None,
        target: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.operation = operation
        self.target = target


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


#: Where a receptor-MSA diagnosis lives inside a job's ``result`` payload.
#: Checked in order. The platform writes ``retry_msa_diagnosis`` from
#: ``/api/retry-fold`` (``fastapi_backend/routers/fold.py``); the others are the
#: shapes a nested or future writer may use. [bd-LIGANDAI_ALPHA_V2-7r1sb]
RECEPTOR_MSA_DIAGNOSIS_KEYS: tuple[str, ...] = (
    "retry_msa_diagnosis",
    "msa_diagnosis",
)

#: ``tag`` values the platform emits (``msa-diagnosis/1``). A diagnosis whose
#: tag is not the refusal means the fold was NOT blocked by MSA availability —
#: it either never needed help, or a lower rung supplied the alignment.
RECEPTOR_MSA_TAG_OK = "receptor_msa_complete"
RECEPTOR_MSA_TAG_SELF_HEALED = "receptor_msa_self_healed"
RECEPTOR_MSA_TAG_UNAVAILABLE = "receptor_msa_unavailable"


def receptor_msa_diagnosis(result: dict[str, Any] | None) -> dict[str, Any] | None:
    """Pull the receptor-MSA diagnosis out of a job ``result`` payload, or None.

    Tolerant by design: an older server sends nothing, and a newer one may add
    keys. Only a dict carrying a ``tag`` is accepted, so a truncated or
    half-written value reads as absent rather than as a malformed diagnosis.
    """
    if not isinstance(result, dict):
        return None
    for key in RECEPTOR_MSA_DIAGNOSIS_KEYS:
        candidate = result.get(key)
        if isinstance(candidate, dict) and candidate.get("tag"):
            return candidate
    return None


class LigandAIReceptorMsaUnavailable(LigandAIJobError):
    """No alignment could be found for a receptor chain, so the fold was refused.

    ⛔ WHY THIS IS A REFUSAL AND NOT A DEGRADED RESULT
    An MSA-free receptor fold does not fail. It returns a confident-looking
    structure whose ipSAE is meaningless and which downstream ranking cannot
    tell from a real one. The platform therefore refuses rather than degrade,
    and this error carries the machine-readable reason so a caller can act on
    it instead of parsing prose.

    The platform tried, in order: the local MSA service (forced endpoint
    re-probe), the Modal volume cache, and a UniProt canonical-parent alignment.
    :attr:`rungs` records each attempt — which endpoint, which cause, how long.

    Attributes
    ----------
    diagnosis : dict
        The whole ``msa-diagnosis/1`` object, verbatim. Prefer the typed
        attributes below; reach in here for a field a newer server added.
    tag : str
        ``"receptor_msa_unavailable"``. The stable machine identifier for THE
        FLAW, suitable for branching, metrics, and issue routing.
    flaw : str
        One sentence saying what is actually wrong.
    receptor_chains / missing_chains : list[str]
        Every chain that needed an alignment, and those that still have none.
    rungs : list[dict]
        Per-rung receipts: ``{rung, order, ok, cause, error, endpoint,
        elapsed_ms, chains_recovered, realized_depth, attempts, detail}``.
    required_fixes : list[dict]
        What would make this work, ordered, each ``{order, id, action, who,
        automatable, because, verify}``. Derived from what ACTUALLY failed — a
        healthy MSA service is never blamed for an orphan receptor.
    retryable : bool
        True only when at least one rung failed for a transient reason. False
        means the identical request cannot succeed and retrying burns budget.
    gene, uniprot_id : str | None
        The receptor identity the platform resolved, when it could.
    schema : str
        e.g. ``"msa-diagnosis/1"``. An unrecognised major means the typed
        attributes may be incomplete — fall back to :meth:`human_text`.

    Examples
    --------
    >>> try:
    ...     fold = job.wait()
    ... except LigandAIReceptorMsaUnavailable as exc:
    ...     print(exc.tag, exc.missing_chains, exc.retryable)
    ...     for fix in exc.required_fixes:
    ...         print(fix["order"], fix["id"], fix["action"])
    """

    def __init__(
        self,
        message: str,
        *,
        job_id: str,
        job_status: str,
        diagnosis: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, job_id=job_id, job_status=job_status, **kwargs)
        d = diagnosis or {}
        self.diagnosis = d
        self.schema = d.get("schema") or ""
        self.tag = d.get("tag") or RECEPTOR_MSA_TAG_UNAVAILABLE
        self.failure_class = d.get("failure_class") or self.tag
        self.severity = d.get("severity") or "error"
        self.flaw = d.get("flaw") or ""
        self.receptor_chains = list(d.get("receptor_chains") or [])
        self.missing_chains = list(d.get("missing_chains") or [])
        self.recovered_by = dict(d.get("recovered_by") or {})
        self.rungs = list(d.get("rungs") or [])
        self.required_fixes = list(d.get("required_fixes") or [])
        self.retryable = bool(d.get("retryable"))
        self.gene = d.get("gene")
        self.uniprot_id = d.get("uniprot_id")

    # -- convenience -------------------------------------------------------

    def human_text(self) -> str:
        """The platform's own prose block, including "FIXES, in order:".

        Always available, even when :attr:`schema` is a major this SDK does not
        recognise — which is exactly when a caller should read it instead of the
        typed fields.
        """
        return self.diagnosis.get("human") or self.message

    def rung(self, name: str) -> dict[str, Any] | None:
        """The receipt for one rung by name, or None if it never ran."""
        for r in self.rungs:
            if r.get("rung") == name:
                return r
        return None

    def fixes_text(self) -> str:
        """The required fixes as a numbered block, for logs and tickets."""
        lines = []
        for fix in self.required_fixes:
            who = "the platform retries this for you" if fix.get("automatable") \
                else f"{fix.get('who', 'caller')} must act"
            lines.append(f"{fix.get('order')}. {fix.get('action')} ({who})")
        return "\n".join(lines)

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(job_id={self.job_id!r}, tag={self.tag!r}, "
            f"missing_chains={self.missing_chains!r}, retryable={self.retryable!r}, "
            f"fixes={[f.get('id') for f in self.required_fixes]!r})"
        )


def job_error_from_info(
    message: str,
    *,
    job_id: str,
    job_status: str,
    result: dict[str, Any] | None = None,
) -> LigandAIJobError:
    """Build the most specific job-failure error the payload supports.

    ⛔ THE GAP THIS CLOSES. Every ``LigandAIJobError`` raise site used to pass
    only ``message``/``job_id``/``job_status``, so ``.response`` was always
    ``None`` for a failed job and the structured payload — which was sitting
    right there in ``self._info.result`` — was discarded into a string. The
    sibling timeout path (:class:`LigandAIWaitTimeout`) already kept it as
    ``server_state``; job failures did not. So a caller whose fold was refused
    for an unavailable receptor MSA could only regex the message.

    Now the whole result rides on ``.response``, and when it carries a
    ``msa-diagnosis/1`` refusal the caller gets
    :class:`LigandAIReceptorMsaUnavailable` with typed fields instead.
    A self-healed or complete diagnosis is NOT a refusal and does not change
    the class — the fold failed for some other reason and mislabelling it would
    send the caller after the wrong cause.
    """
    diag = receptor_msa_diagnosis(result)
    if diag is not None and diag.get("tag") == RECEPTOR_MSA_TAG_UNAVAILABLE:
        return LigandAIReceptorMsaUnavailable(
            diag.get("human") or message,
            job_id=job_id,
            job_status=job_status,
            diagnosis=diag,
            code=RECEPTOR_MSA_TAG_UNAVAILABLE,
            response=result,
        )
    return LigandAIJobError(
        message,
        job_id=job_id,
        job_status=job_status,
        response=result,
    )


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


class LigandAIParameterNotForwarded(LigandAIInvalidConfig):
    """A real, supported capability that the request path does not carry — yet.

    Raised CLIENT-SIDE, before any HTTP call, when the SDK knows a parameter
    would be accepted by the HTTP layer, silently dropped by an intermediate
    hop, and therefore reported as set while the run executes without it.

    This is NOT "the SDK is being cautious". It is the SDK refusing to
    manufacture the one defect the platform has paid for repeatedly: a knob
    that reads ON in the request and OFF in the run, with nothing in the
    response saying so. Neither the request schemas nor the response bodies
    reject unknown keys, so a silently-dropped parameter is indistinguishable
    from an honoured one at the wire level; the check has to live here.

    Attributes
    ----------
    parameter : str
        The kwarg the caller set.
    reached : str | None
        The last layer that DOES read the parameter.
    dropped_at : str | None
        The layer that drops it.
    tracking : str | None
        The tracking id for the plumbing work.
    override : str | None
        The kwarg that forces the request through anyway (for a caller running
        against a server where the gap is already closed).
    """

    def __init__(
        self,
        message: str,
        *,
        parameter: str | None = None,
        reached: str | None = None,
        dropped_at: str | None = None,
        tracking: str | None = None,
        override: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, field=parameter, **kwargs)
        self.parameter = parameter
        self.reached = reached
        self.dropped_at = dropped_at
        self.tracking = tracking
        self.override = override


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


BODY_EXCERPT_LIMIT = 600
"""How much of an unreadable body to keep on the exception.

Long enough that an HTML ``<title>`` and a CDN ray id survive (they are the
two things that identify WHO answered), short enough that a 200 KB challenge
page does not land in a log line.
"""


def body_excerpt(raw_body: str | None, limit: int = BODY_EXCERPT_LIMIT) -> str | None:
    """Collapse a raw body to a single-line excerpt, verbatim apart from
    whitespace folding. Never normalises content — see
    :func:`_message_already_surfaces_url` on why normalising bodies is unsafe.
    """
    if raw_body is None:
        return None
    text = " ".join(raw_body.split())
    if not text:
        return None
    if len(text) > limit:
        return text[:limit] + f"… [{len(raw_body)} bytes total]"
    return text


# Signatures that identify a response as authored by an intermediary rather
# than by our own server. [bd-dre-3fduj] Cloudflare Bot Fight Mode is the one
# actually in the path today; the others are listed because the *class* of
# failure (an HTML page where JSON was promised) is what must be named, and
# guessing "cloudflare" for an nginx page would be its own fabrication.
_EDGE_SERVER_HEADERS: dict[str, str] = {
    "cloudflare": "cloudflare",
    "cloudfront": "cloudfront",
    "akamaighost": "akamai",
    "awselb": "aws-elb",
    "nginx": "nginx",
    "envoy": "envoy",
}

_EDGE_BODY_MARKERS: tuple[tuple[str, str], ...] = (
    ("cf-ray", "cloudflare"),
    ("cloudflare ray id", "cloudflare"),
    ("attention required! | cloudflare", "cloudflare"),
    ("just a moment", "cloudflare"),
    ("checking your browser", "cloudflare"),
    ("enable javascript and cookies to continue", "cloudflare"),
    ("you have been blocked", "cloudflare"),
    ("generated by cloudfront", "cloudfront"),
    ("<center>nginx</center>", "nginx"),
    ("403 forbidden</title>", "proxy"),
)


def detect_edge(
    headers: Mapping[str, str] | None,
    raw_body: str | None,
    content_type: str | None,
) -> tuple[str | None, str | None]:
    """Identify the intermediary that produced a response, if any.

    Returns ``(edge_name, ray_id)``. ``edge_name`` is None when nothing
    indicates an intermediary — we do NOT guess. Detection requires a
    non-JSON body, because our own server always answers JSON: an edge
    signature on a JSON body means the CDN merely PROXIED our real response,
    which must not be reclassified as a block.
    """
    if content_type and "application/json" in content_type.lower():
        return None, None

    h: dict[str, str] = {}
    if headers:
        try:
            h = {str(k).lower(): str(v) for k, v in headers.items()}
        except Exception:
            h = {}

    ray_id = h.get("cf-ray") or h.get("x-amz-cf-id") or h.get("x-akamai-request-id")

    edge: str | None = None
    server = (h.get("server") or "").strip().lower()
    for token, name in _EDGE_SERVER_HEADERS.items():
        if token in server:
            edge = name
            break

    if edge is None and ray_id and h.get("cf-ray"):
        edge = "cloudflare"

    if edge is None and raw_body:
        lowered = raw_body[:4000].lower()
        for marker, name in _EDGE_BODY_MARKERS:
            if marker in lowered:
                edge = name
                break

    if edge is None:
        return None, ray_id
    return edge, ray_id


def error_from_response(
    status_code: int,
    payload: dict[str, Any] | None,
    request_id: str | None = None,
    *,
    retry_after: float | None = None,
    raw_body: str | None = None,
    content_type: str | None = None,
    headers: Mapping[str, str] | None = None,
    method: str | None = None,
    url: str | None = None,
    auth_guidance: str | None = None,
) -> LigandAIError:
    """Build the right error subclass for an HTTP response.

    The server's error codes (``E001``-``E013``) map to specific subclasses.
    Falls back to status-code-based dispatch when no code is present.

    ``raw_body`` / ``content_type`` / ``headers`` are the UNDECODED response.
    They are what makes "our server refused you" distinguishable from "a
    CDN refused you before our server saw the request" [bd-dre-3fduj] and
    stop a non-JSON error body from being discarded [bd-dre-2d7za.9]. All
    are optional and defaulted so existing two-argument callers (and the
    tests pinning them) are unaffected.
    """
    payload = payload or {}
    excerpt = body_excerpt(raw_body)
    # ⛔ [bd-dre-2d7za.9] A non-JSON error body used to be DISCARDED, so a
    # legal 403 rewritten by a proxy — or any text/plain refusal that still
    # carried its acceptance URL in plain text — degraded to the string
    # "HTTP 403" with the one actionable thing in it thrown away. When the
    # server gave us no parseable fields, the raw body IS the message: it is
    # strictly more informative than a status restatement, and it is the only
    # place a rewritten-but-readable explanation can survive.
    message = (
        payload.get("message")
        or payload.get("error")
        or excerpt
        or f"HTTP {status_code}"
    )
    code = payload.get("code")

    common: dict[str, Any] = {
        "code": code,
        "status_code": status_code,
        "request_id": request_id,
        "response": payload,
    }

    # ⛔ [bd-dre-3fduj] Was the responder ours at all? A Cloudflare Bot Fight
    # Mode challenge is an HTTP 403 with an HTML body; our own authorization
    # refusal is an HTTP 403 with a JSON body. Answering both with
    # LigandAIForbidden("HTTP 403") told a user to fix their API key when the
    # key was never read by anything. Checked BEFORE the status dispatch so
    # it also covers the 503 / 429 that bot-filters and load balancers emit.
    edge, ray_id = detect_edge(headers, raw_body, content_type)
    if edge is not None and not payload and status_code in (401, 403, 429, 503, 502, 520, 521, 522):
        where = f" {method.upper()} {url}" if method and url else ""
        ray = f"\n  {edge} ray id : {ray_id}" if ray_id else ""
        return LigandAIEdgeBlocked(
            f"Blocked by {edge} before reaching the LigandAI API "
            f"(HTTP {status_code} with a {content_type or 'non-JSON'} body on{where or ' this request'}).\n"
            f"Your API key was NOT the problem — our server never saw this request."
            f"{ray}\n"
            f"  what happened  : an intermediary answered instead of the API. "
            f"Bot-filtering on a proxied hostname 403s non-browser clients.\n"
            f"  what to do     : reach a non-proxied origin, or have the client "
            f"allowlisted for the API paths; retrying and rotating keys will not help.\n"
            f"  body served    : {excerpt or '<empty>'}",
            edge=edge,
            ray_id=ray_id,
            content_type=content_type,
            body_excerpt=excerpt,
            reason=f"{edge}_edge_block",
            **common,
        )

    if status_code == 401:
        # ⛔ A bare "Unauthorized" does not say WHICH auth problem this is, and
        # the caller cannot tell an absent key from a malformed one from a
        # revoked one. `auth_guidance` is computed from the key we actually
        # SENT, which is the only place that distinction survives.
        return LigandAIAuthError(
            f"{message}\n  {auth_guidance}" if auth_guidance else message,
            **common,
        )
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
    # [bd-LIGANDAI_ALPHA_V2-7r1sb] A receptor-MSA refusal, whatever status it
    # arrives under. /api/retry-fold answers 200 with status='refused' (the
    # daemon keys on the body, and a non-200 would reduce the diagnosis to a
    # `detail` string), but a route that chooses to surface it as an HTTP error
    # must still produce the typed class rather than a bare message.
    _msa_diag = receptor_msa_diagnosis(payload) or (
        payload if payload.get("tag") == RECEPTOR_MSA_TAG_UNAVAILABLE else None
    )
    if _msa_diag is not None and _msa_diag.get("tag") == RECEPTOR_MSA_TAG_UNAVAILABLE:
        return LigandAIReceptorMsaUnavailable(
            _msa_diag.get("human") or message,
            job_id=str(payload.get("job_id") or payload.get("jobId") or ""),
            job_status=str(payload.get("status") or "refused"),
            diagnosis=_msa_diag,
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


# ── mutation outcome policy ──────────────────────────────────────────────────

# Statuses that mean: the server was REACHED, it UNDERSTOOD the request, and it
# DECLINED (or the thing was already gone). Only these may be reported as a
# plain `False` by a `-> bool` mutation, because only these are an answer.
#
# Everything else — 401/403 (we never proved who we are), 429 (never processed),
# 5xx (it broke mid-flight), a dropped connection, a CDN block, an unreadable
# body — leaves the outcome UNKNOWN. The mutation may well have been applied.
SERVER_DECLINED_STATUSES: frozenset[int] = frozenset({
    400,  # malformed request — the server parsed it and said no
    404,  # no such resource — already gone, or never existed
    405,  # method not allowed on this resource
    409,  # conflict, e.g. "already finished" — the canonical honest False
    410,  # gone
    422,  # unprocessable — validated and rejected
})


def resolve_mutation_failure(
    exc: BaseException,
    *,
    operation: str,
    target: str | None = None,
) -> None:
    """Decide whether a failed ``-> bool`` mutation may answer ``False``.

    Returns normally when ``False`` is an HONEST answer — the server was
    reached and declined. RAISES otherwise, so an unknown outcome can never
    be spelled the same way as a refusal.

    ⛔ THE DEFECT THIS REPLACES. Ten mutation methods (``jobs.cancel``,
    ``programs.archive``, ``proteins.delete_variant``, ``memory.delete``,
    ``discovery.delete_dataset``, and their async twins) each ended in
    ``except Exception: return False``. A caller reading ``False`` concludes
    "the server refused". It was also what they got from a dropped
    connection, an expired key, a Cloudflare block and a 500 — four cases
    where the mutation's outcome is genuinely UNKNOWN and may have been
    applied. ``job.cancel()`` answering ``False`` after a network blip told
    a user their GPU job was still running when it had been cancelled; the
    user then acted on a fact the SDK invented. A previous pass recognised
    exactly this hazard and carved out ONE cause
    (:class:`LigandAILegalAcceptanceRequired`) while leaving the rest
    flattened; this generalises that carve-out to every cause.

    Parameters
    ----------
    exc : BaseException
        The exception the mutation raised.
    operation : str
        Dotted name of the mutation, e.g. ``"jobs.cancel"`` — quoted back in
        the error so the caller knows WHAT was attempted.
    target : str | None
        The id it was attempted against.
    """
    # Preserved verbatim from the original carve-outs: a legal gate must reach
    # the caller with its acceptance URL intact, never become `False`.
    if isinstance(exc, LigandAILegalAcceptanceRequired):
        raise exc

    status = getattr(exc, "status_code", None)
    if isinstance(exc, LigandAIError) and status in SERVER_DECLINED_STATUSES:
        return  # an honest False: asked, understood, declined.

    where = f" {target!r}" if target else ""
    if isinstance(exc, LigandAIError):
        raise LigandAIMutationUnconfirmed(
            f"{operation}{where} could not be confirmed — "
            f"the SDK will not report this as a refusal.\n"
            f"  attempted      : {operation}{where}\n"
            f"  what went wrong: {type(exc).__name__}: {exc}\n"
            f"  outcome        : UNKNOWN. The request may or may not have been "
            f"applied server-side; this was not a 'no' from the server.\n"
            f"  what to do     : resolve the error above, then re-read the "
            f"resource's state to find out whether it took effect. "
            f"Do NOT assume it did not.",
            operation=operation,
            target=target,
            status_code=status,
            request_id=getattr(exc, "request_id", None),
            code=getattr(exc, "code", None),
        ) from exc

    raise LigandAIMutationUnconfirmed(
        f"{operation}{where} failed with a non-SDK error — "
        f"the SDK will not report this as a refusal.\n"
        f"  attempted      : {operation}{where}\n"
        f"  what went wrong: {type(exc).__name__}: {exc}\n"
        f"  outcome        : UNKNOWN. This is not a 'no' from the server.\n"
        f"  what to do     : treat the resource's state as unknown and re-read it.",
        operation=operation,
        target=target,
    ) from exc


class LigandAISurfaceNotInThisPackage(LigandAIError, AttributeError):
    """A platform surface that EXISTS server-side has no client in this package.

    ⛔ [bd-dre-rp11j.9] ``client.campaigns`` is the live instance. The platform
    exposes a campaign/maturation API (``POST /api/campaign/start``,
    ``/mature``, ``GET /{id}/leaderboard``, ``/status``, ``/stop``,
    ``/stream``). The published ``ligandai`` package has no client for ANY of
    it — but it does have a ``campaigns`` namespace, an unrelated local
    panel-folding helper that happens to share the name.

    So a user who reads about campaigns and reaches for ``client.campaigns``
    gets a namespace that exists, imports cleanly, and does something else
    entirely. A plain ``AttributeError`` would be no better: "no attribute
    'mature'" reads as a typo, leaving "this package cannot do campaigns"
    indistinguishable from "I called it wrong" — the empty-vs-could-not-look
    confusion wearing a new costume.

    Subclasses :class:`AttributeError` as well as :class:`LigandAIError` so
    ``hasattr()`` and duck-typed probes keep behaving, while anything that
    surfaces the message gets the full explanation.

    Attributes
    ----------
    surface : str
        The attribute that was reached for (e.g. ``"mature"``).
    namespace : str
        The namespace it was reached for on (e.g. ``"client.campaigns"``).
    server_routes : tuple[str, ...]
        The endpoints that DO exist server-side, so the caller can drive them
        over plain HTTP in the meantime.
    """

    def __init__(
        self,
        message: str,
        *,
        surface: str,
        namespace: str,
        server_routes: tuple[str, ...] = (),
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.surface = surface
        self.namespace = namespace
        self.server_routes = server_routes
