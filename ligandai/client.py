# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Top-level :class:`LigandAI` and :class:`AsyncLigandAI` clients.

Construction
------------
.. code-block:: python

    # Reads LIGANDAI_API_KEY env var by default
    client = LigandAI()

    # Or pass explicitly
    client = LigandAI(api_key="lgai_basic_AbC123...")

    # Custom base URL (enterprise, on-prem, dev)
    client = LigandAI(api_key="...", base_url="http://localhost:5050")

Tier detection
--------------
The tier is inferred from the API-key prefix on construction — no network
round-trip. Use ``client.feature_allowed("generate_peptides")`` to check
client-side whether a feature is available before calling.
"""

from __future__ import annotations

import logging
import os
from typing import Any
from uuid import uuid4

import httpx

_logger = logging.getLogger("ligandai")


def _log_client_init(cls_name: str, base_url: str, tier: str | None, api_key: str | None) -> None:
    """Emit a single INFO line so customers (and their AI agents) can confirm
    which host the SDK is talking to and which tier the key resolved to.

    Suppressible via standard logging config:
        logging.getLogger("ligandai").setLevel(logging.WARNING)
    """
    if api_key:
        # Show only the prefix so logs never leak the full secret.
        key_hint = api_key[:8] + "..."
    else:
        key_hint = "<none>"
    _logger.info(
        "%s initialized: base_url=%s tier=%s api_key=%s",
        cls_name,
        base_url,
        tier or "anonymous",
        key_hint,
    )

from ligandai._constants import (
    API_KEY_PREFIXES,
    DEFAULT_BASE_URL,
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT_SECS,
    FEATURE_MIN_TIER,
    TIER_FOLD_LIMITS,
    TIER_GENERATION_LIMITS,
    TIER_GPU_SLOTS,
    TIER_ORDER,
    TIER_RATE_LIMITS,
    TIER_TARGET_LIMITS,
    Tier,
)
from ligandai._http import AsyncHTTPTransport, HTTPTransport
from ligandai.errors import (
    LigandAIAuthError,
    LigandAIPaidTierRequired,
    LigandAITierError,
)
from ligandai.resources.account import Account, AsyncAccount
from ligandai.resources.bivalent import AsyncBivalent, Bivalent
from ligandai.resources.charts import AsyncCharts, Charts
from ligandai.resources.discovery import AsyncDiscovery, Discovery
from ligandai.resources.diseases import AsyncDiseases, Diseases
from ligandai.resources.folds import AsyncFolds, Folds
from ligandai.resources.goals import AsyncGoals, Goals
from ligandai.resources.jobs import AsyncJobs, Jobs
from ligandai.resources.memory import AsyncMemory, Memory
from ligandai.resources.msa import MSA, AsyncMSA
from ligandai.resources.peptides import AsyncPeptides, Peptides
from ligandai.resources.programs import AsyncPrograms, Programs
from ligandai.resources.proteins import AsyncProteins, Proteins
from ligandai.resources.receptors import AsyncReceptors, Receptors
from ligandai.resources.reports import AsyncReports, Reports
from ligandai.resources.structures import AsyncStructures, Structures
from ligandai.resources.synthesis import AsyncSynthesis, Synthesis
from ligandai.types import ClientSessionUsage, Credits, User
from ligandai.version_check import emit_update_notice


def _resolve_api_key(api_key: str | None) -> str | None:
    """Get API key from arg, then env vars."""
    if api_key:
        return api_key
    return os.environ.get("LIGANDAI_API_KEY") or os.environ.get("LIGANDAI_TEST_API_KEY")


def _detect_tier(api_key: str | None) -> Tier | None:
    """Infer tier from key prefix without a network call.

    Returns None when no key (anonymous mode) or unrecognized prefix.
    """
    if not api_key:
        return None
    for tier, prefix in API_KEY_PREFIXES.items():
        if api_key.startswith(prefix):
            return tier
    return None


def _tier_at_least(actual: Tier | None, required: Tier) -> bool:
    """Return True if ``actual`` tier is >= ``required``.

    None (anonymous) is below all tiers.
    """
    if actual is None:
        return False
    return TIER_ORDER.index(actual) >= TIER_ORDER.index(required)


class _ClientCommon:
    """Shared logic between sync and async clients (tier check, repr, etc.)."""

    _api_key: str | None
    _tier: Tier | None
    _base_url: str

    def __init__(self, api_key: str | None, base_url: str) -> None:
        self._api_key = api_key
        self._tier = _detect_tier(api_key)
        self._base_url = base_url

    @property
    def api_key(self) -> str | None:
        return self._api_key

    @property
    def tier(self) -> Tier | None:
        """Tier inferred from the API key prefix.

        Returns None for anonymous clients (no key).
        """
        return self._tier

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def rate_limit_per_minute(self) -> int | None:
        return TIER_RATE_LIMITS.get(self._tier) if self._tier else None

    @property
    def max_concurrent_gpu_slots(self) -> int | None:
        return TIER_GPU_SLOTS.get(self._tier) if self._tier else None

    @property
    def max_peptides_per_generation(self) -> int | None:
        return TIER_GENERATION_LIMITS.get(self._tier) if self._tier else None

    @property
    def max_folds_per_generation(self) -> int | None:
        return TIER_FOLD_LIMITS.get(self._tier) if self._tier else None

    @property
    def max_targets_per_generation(self) -> int | None:
        return TIER_TARGET_LIMITS.get(self._tier) if self._tier else None

    def feature_allowed(self, feature: str) -> bool:
        """Check whether the current tier can call a named feature."""
        if self._tier == "superadmin":
            return True
        required = FEATURE_MIN_TIER.get(feature)
        if required is None:
            # Unknown feature — assume allowed; server will gate.
            return True
        return _tier_at_least(self._tier, required)

    def _require_feature(self, feature: str) -> None:
        """Raise LigandAITierError client-side if the feature is unavailable."""
        if self.feature_allowed(feature):
            return
        required = FEATURE_MIN_TIER.get(feature, "pro")
        raise LigandAITierError(
            f"Feature '{feature}' requires {required} tier or higher.",
            current_tier=self._tier,
            required_tier=required,
        )

    def _require_paid_tier(self) -> None:
        """Fail fast if the API key resolves to a non-paid tier.

        Used by the v0.2.0 ``/api/v1/peptides/*`` surface, which is paid-only
        per platform policy. This raises :class:`LigandAIPaidTierRequired`
        before the request even reaches the wire — friendlier than waiting
        for the server's 402 response, especially when the tier can be read
        from the key prefix without a network call.

        When the tier cannot be determined locally (no key, unknown prefix),
        we let the request proceed and surface the server's 402 cleanly via
        the response error mapper.
        """
        # Locally inferred tier — None means anonymous OR unrecognized prefix;
        # both should fail to use the paid surface.
        if self._tier is None:
            # Anonymous / unknown prefix: don't pre-emptively reject (the user
            # may have set a base_url to a custom deployment that uses
            # different prefixes); let the server's 402 surface through the
            # error mapper.
            return
        if self._tier in ("basic", "academia", "pro", "enterprise", "superadmin"):
            return
        raise LigandAIPaidTierRequired(
            (
                "This SDK method requires a paid subscription "
                f"(your key resolves to '{self._tier}' tier). "
                "Visit https://ligandai.com/pricing to upgrade."
            ),
            current_tier=self._tier,
            required_tier="basic",
        )

    def __repr__(self) -> str:
        tier = self._tier or "anonymous"
        return f"{type(self).__name__}(tier={tier!r}, base_url={self._base_url!r})"


class LigandAI(_ClientCommon):
    """Synchronous LIGANDAI client.

    Parameters
    ----------
    api_key
        API key prefixed with ``lgai_<tier>_*``. Defaults to env var
        ``LIGANDAI_API_KEY`` (or ``LIGANDAI_TEST_API_KEY`` for tests).
    base_url
        Override the default ``https://ligandai.com``. Useful for dev
        (``http://localhost:5050``) or on-prem deployments. The platform
        responds on the apex domain — ``api.ligandai.com`` is **not** a
        published host, so do not point integrations there.
    timeout
        Per-request timeout in seconds (default 60).
    max_retries
        Retries on 429/5xx/network errors (default 5).
    impersonate_user
        Superadmin-only. Sets ``X-Impersonate-User`` header. Server-gated to
        localhost / VPN subnet 10.200.200.0/24.
    client_session_id
        Optional caller-provided run ID sent as
        ``X-LigandAI-Client-Session-Id`` for usage and credit attribution.
    http_client
        Inject a pre-configured :class:`httpx.Client` (e.g. for custom proxies,
        certificate auth). Optional.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT_SECS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        impersonate_user: str | None = None,
        client_session_id: str | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        resolved_key = _resolve_api_key(api_key)
        super().__init__(resolved_key, base_url)
        emit_update_notice()
        _log_client_init("LigandAI", base_url, self._tier, resolved_key)

        self._transport = HTTPTransport(
            api_key=resolved_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            impersonate_user=impersonate_user,
            client_session_id=client_session_id,
            client=http_client,
        )

        # Resource namespaces
        self.account: Account = Account(self._transport)
        self.bivalent: Bivalent = Bivalent(self._transport, client=self)
        self.charts: Charts = Charts(self._transport)
        self.diseases: Diseases = Diseases(self._transport)
        self.discovery: Discovery = Discovery(self._transport, client=self)
        self.folds: Folds = Folds(self._transport)
        self.jobs: Jobs = Jobs(self._transport)
        self.goals: Goals = Goals(self._transport, client=self)
        self.memory: Memory = Memory(self._transport)
        self.msa: MSA = MSA(self._transport)
        self.peptides: Peptides = Peptides(self._transport, client=self)
        self.programs: Programs = Programs(self._transport)
        self.proteins: Proteins = Proteins(self._transport)
        self.receptors: Receptors = Receptors(self._transport)
        self.reports: Reports = Reports(self._transport)
        self.structures: Structures = Structures(self._transport)
        self.synthesis: Synthesis = Synthesis(self._transport)

        # Cached on-demand
        self._user: User | None = None
        self._credits: Credits | None = None

    @property
    def transport(self) -> HTTPTransport:
        """Underlying HTTP transport — use this to call endpoints not yet
        wrapped by a typed namespace."""
        return self._transport

    @property
    def client_session_id(self) -> str | None:
        """Caller-provided SDK run/session ID sent on every request."""
        return self._transport.client_session_id

    def set_client_session_id(self, client_session_id: str | None) -> str | None:
        """Set or clear the SDK run/session ID header for subsequent requests."""
        return self._transport.set_client_session_id(client_session_id)

    def session(self, session_id: str | None = None) -> CreditSession:
        """Track one local SDK run with a stable session ID and credit delta."""
        return CreditSession(self, session_id=session_id)

    @property
    def user(self) -> User:
        """Currently authenticated user. Cached on first access."""
        if self._user is None:
            self._user = self.account.me()
        return self._user

    @property
    def credits(self) -> int:
        """Current credit balance. Lightweight refresh on each access.

        For unlimited / superadmin accounts the server may return a sentinel
        value; in that case :class:`~ligandai.types.Credits.is_unlimited`
        will be True and the integer returned here will be the raw sentinel.
        Use ``client.account.credits()`` and inspect ``.is_unlimited`` for a
        clean check.
        """
        c = self.account.credits()
        self._credits = c
        return c.balance

    def close(self) -> None:
        self._transport.close()

    def __enter__(self) -> LigandAI:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def health(self) -> dict[str, Any]:
        """Hit ``GET /api/healthz`` — useful for connectivity checks."""
        return self._transport.request("GET", "/api/healthz") or {}


class AsyncLigandAI(_ClientCommon):
    """Asynchronous LIGANDAI client.

    Use as an async context manager:

    .. code-block:: python

        async with AsyncLigandAI() as client:
            user = await client.account.me()
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT_SECS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        impersonate_user: str | None = None,
        client_session_id: str | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        resolved_key = _resolve_api_key(api_key)
        super().__init__(resolved_key, base_url)
        emit_update_notice()
        _log_client_init("AsyncLigandAI", base_url, self._tier, resolved_key)

        self._transport = AsyncHTTPTransport(
            api_key=resolved_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            impersonate_user=impersonate_user,
            client_session_id=client_session_id,
            client=http_client,
        )

        self.account: AsyncAccount = AsyncAccount(self._transport)
        self.bivalent: AsyncBivalent = AsyncBivalent(self._transport, client=self)
        self.charts: AsyncCharts = AsyncCharts(self._transport)
        self.diseases: AsyncDiseases = AsyncDiseases(self._transport)
        self.discovery: AsyncDiscovery = AsyncDiscovery(self._transport, client=self)
        self.folds: AsyncFolds = AsyncFolds(self._transport)
        self.jobs: AsyncJobs = AsyncJobs(self._transport)
        self.goals: AsyncGoals = AsyncGoals(self._transport, client=self)
        self.memory: AsyncMemory = AsyncMemory(self._transport)
        self.msa: AsyncMSA = AsyncMSA(self._transport)
        self.peptides: AsyncPeptides = AsyncPeptides(self._transport, client=self)
        self.programs: AsyncPrograms = AsyncPrograms(self._transport)
        self.proteins: AsyncProteins = AsyncProteins(self._transport)
        self.receptors: AsyncReceptors = AsyncReceptors(self._transport)
        self.reports: AsyncReports = AsyncReports(self._transport)
        self.structures: AsyncStructures = AsyncStructures(self._transport)
        self.synthesis: AsyncSynthesis = AsyncSynthesis(self._transport)

    @property
    def transport(self) -> AsyncHTTPTransport:
        return self._transport

    @property
    def client_session_id(self) -> str | None:
        return self._transport.client_session_id

    def set_client_session_id(self, client_session_id: str | None) -> str | None:
        return self._transport.set_client_session_id(client_session_id)

    def session(self, session_id: str | None = None) -> AsyncCreditSession:
        return AsyncCreditSession(self, session_id=session_id)

    async def close(self) -> None:
        await self._transport.close()

    async def __aenter__(self) -> AsyncLigandAI:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def health(self) -> dict[str, Any]:
        return await self._transport.request("GET", "/api/healthz") or {}

    async def me(self) -> User:
        return await self.account.me()


def _generated_session_id() -> str:
    return f"ligandai-sdk-{uuid4().hex[:16]}"


class CreditSession:
    """Synchronous context manager for local-run credit attribution."""

    def __init__(self, client: LigandAI, *, session_id: str | None = None) -> None:
        self.client = client
        self.session_id = session_id or _generated_session_id()
        self.start_credits: int | None = None
        self.end_credits: int | None = None
        self.credits_used: int | None = None
        self.usage: ClientSessionUsage | None = None
        self._previous_session_id: str | None = None

    def __enter__(self) -> CreditSession:
        self._previous_session_id = self.client.client_session_id
        self.client.set_client_session_id(self.session_id)
        self.start_credits = self.client.account.get_balance().credits
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        try:
            try:
                self.end_credits = self.client.account.get_balance().credits
                if self.start_credits is not None:
                    self.credits_used = max(0, self.start_credits - self.end_credits)
            except Exception:
                if exc_type is None:
                    raise
        finally:
            self.client.set_client_session_id(self._previous_session_id)

    def refresh_usage(self, period: str = "30d") -> ClientSessionUsage:
        self.usage = self.client.account.session_usage(self.session_id, period=period)  # type: ignore[arg-type]
        return self.usage


class AsyncCreditSession:
    """Async context manager for local-run credit attribution."""

    def __init__(self, client: AsyncLigandAI, *, session_id: str | None = None) -> None:
        self.client = client
        self.session_id = session_id or _generated_session_id()
        self.start_credits: int | None = None
        self.end_credits: int | None = None
        self.credits_used: int | None = None
        self.usage: ClientSessionUsage | None = None
        self._previous_session_id: str | None = None

    async def __aenter__(self) -> AsyncCreditSession:
        self._previous_session_id = self.client.client_session_id
        self.client.set_client_session_id(self.session_id)
        self.start_credits = (await self.client.account.get_balance()).credits
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        try:
            try:
                self.end_credits = (await self.client.account.get_balance()).credits
                if self.start_credits is not None:
                    self.credits_used = max(0, self.start_credits - self.end_credits)
            except Exception:
                if exc_type is None:
                    raise
        finally:
            self.client.set_client_session_id(self._previous_session_id)

    async def refresh_usage(self, period: str = "30d") -> ClientSessionUsage:
        self.usage = await self.client.account.session_usage(self.session_id, period=period)  # type: ignore[arg-type]
        return self.usage


def _check_authenticated(client: _ClientCommon) -> None:
    """Raise if the client has no API key."""
    if not client.api_key:
        raise LigandAIAuthError(
            "This operation requires an API key. Pass api_key=... to the "
            "constructor or set the LIGANDAI_API_KEY env var."
        )
