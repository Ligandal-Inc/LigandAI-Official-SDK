# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Top-level :class:`LigandAI` and :class:`AsyncLigandAI` clients.

Construction
------------
.. code-block:: python

    # Reads LIGANDAI_API_KEY env var by default
    client = LigandAI()

    # Or pass explicitly
    client = LigandAI(api_key="lgai_basic_AbC123...")

    # Custom base URL (enterprise, on-prem, local dev)
    client = LigandAI(api_key="...", base_url="http://localhost:8000")

Tier detection
--------------
The tier is inferred from the API-key prefix on construction — no network
round-trip. Use ``client.feature_allowed("generate_peptides")`` to check
client-side whether a feature is available before calling.
"""

from __future__ import annotations

import logging
import os
import threading
import warnings
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

if TYPE_CHECKING:
    from ligandai._dedupe import CreditLedger, SubmittedSet

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
from ligandai.diagnostics import Diagnostic
from ligandai.errors import (
    LigandAIAuthError,
    LigandAIDiagnosticWarning,
    LigandAIError,
    LigandAIInvalidConfig,
    LigandAILegalAcceptanceRequired,
    LigandAIPaidTierRequired,
    LigandAITierError,
)
from ligandai.key_wallet import WALLET_PATH, KeyWallet
from ligandai.resources.account import Account, AsyncAccount
from ligandai.resources.analysis import Analysis, AsyncAnalysis
from ligandai.resources.bivalent import AsyncBivalent, Bivalent
from ligandai.resources.campaigns import AsyncCampaigns, Campaigns
from ligandai.resources.charts import AsyncCharts, Charts
from ligandai.resources.deltaforge import AsyncDeltaForge, DeltaForge
from ligandai.resources.discovery import AsyncDiscovery, Discovery
from ligandai.resources.diseases import AsyncDiseases, Diseases
from ligandai.resources.ensemble import AsyncEnsemble, Ensemble
from ligandai.resources.evaluator import AsyncEvaluator, Evaluator  # [bd-LIGANDAI_ALPHA_V2-6bybp]
from ligandai.resources.folds import AsyncFolds, Folds
from ligandai.resources.goals import AsyncGoals, Goals
from ligandai.resources.jobs import AsyncJobs, Jobs
from ligandai.resources.legal import AsyncLegal, Legal
from ligandai.resources.ligands import AsyncLigands, Ligands
from ligandai.resources.linker_modifications import (
    AsyncLinkerModifications,
    LinkerModifications,
)
from ligandai.resources.memory import AsyncMemory, Memory
from ligandai.resources.msa import MSA, AsyncMSA
from ligandai.resources.peptides import AsyncPeptides, Peptides
from ligandai.resources.programs import AsyncPrograms, Programs
from ligandai.resources.proteins import AsyncProteins, Proteins
from ligandai.resources.receptors import AsyncReceptors, Receptors
from ligandai.resources.reports import AsyncReports, Reports
from ligandai.resources.structures import AsyncStructures, Structures
from ligandai.resources.synthesis import AsyncSynthesis, Synthesis
from ligandai.resources.validated_binder import AsyncValidatedBinder, ValidatedBinder
from ligandai.server_compat import COMPATIBILITY_PATH, compatibility_diagnostics
from ligandai.species import DEFAULT_SPECIES, Species, normalize_species
from ligandai.types import (
    ClientSessionUsage,
    Credits,
    SpeciesEntitlement,
    UnlimitedCredits,
    User,
)
from ligandai.version_check import emit_update_notice

_logger = logging.getLogger("ligandai")
_CANONICAL_AA = frozenset("ACDEFGHIKLMNPQRSTVWY")


def _log_client_init(cls_name: str, base_url: str, tier: str | None, api_key: str | None) -> None:
    """Emit one INFO line with host and tier, without leaking the API key."""
    key_hint = api_key[:8] + "..." if api_key else "<none>"
    _logger.info(
        "%s initialized: base_url=%s tier=%s api_key=%s",
        cls_name,
        base_url,
        tier or "anonymous",
        key_hint,
    )


def _looks_like_target_sequence(value: str | None) -> bool:
    if not value:
        return False
    cleaned = "".join(value.upper().split())
    return len(cleaned) >= 30 and all(ch in _CANONICAL_AA for ch in cleaned)


def _resolve_api_key(api_key: str | None) -> str | None:
    """Get API key from arg, then env vars.

    ``LIGANDAI_API_KEY`` is canonical. ``LIGANDAL_API_KEY`` is accepted for
    compatibility with older CLI-onboard snippets.
    """
    if api_key:
        return api_key
    return (
        os.environ.get("LIGANDAI_API_KEY")
        or os.environ.get("LIGANDAL_API_KEY")
        or os.environ.get("LIGANDAI_TEST_API_KEY")
    )


def classify_api_key(api_key: str | None) -> str:
    """Classify a credential by SHAPE alone: ``absent`` | ``malformed`` | ``well_formed``.

    ⛔ [bd-dre-rp11j.3] Three different problems used to produce ONE message.
    Verified live against https://ligandai.com on 2026-09-21:

    ======================  =====================================  ===============
    cause                   what the server did                    what you saw
    ======================  =====================================  ===============
    no key at all           200 with body ``null``                 identical
    malformed key           200 with body ``null``                 identical
    well-formed, invalid    401 ``{"message":"Unauthorized"}``     "Unauthorized"
    ======================  =====================================  ===============

    The first two were byte-identical, and none of the three named the
    environment variable to set or the prefix to use. The server rejects a key
    on SHAPE before it ever checks validity (``server/auth.ts`` gates on
    ``startsWith('lgai_')``), so a malformed key can be caught here — before a
    pointless round-trip that comes back as a generic failure.
    """
    if api_key is None or not str(api_key).strip():
        return "absent"
    return "well_formed" if str(api_key).strip().startswith("lgai_") else "malformed"


#: Where a user gets a key, quoted in every auth-shaped error.
API_KEY_HELP = (
    "Create one at https://ligandai.com/account/billing?tab=api-keys "
    "(available on every tier, including free), then "
    "`export LIGANDAI_API_KEY=lgai_...`"
)


def auth_guidance(api_key: str | None) -> str:
    """Actionable guidance for an auth failure, specific to the key's shape."""
    state = classify_api_key(api_key)
    if state == "absent":
        return (
            "No API key was supplied. The SDK reads LIGANDAI_API_KEY (also "
            "LIGANDAL_API_KEY, LIGANDAI_TEST_API_KEY), or takes api_key= "
            f"directly.\n  {API_KEY_HELP}"
        )
    if state == "malformed":
        return (
            "The API key does not have the expected shape: it must begin with "
            "'lgai_' (e.g. lgai_free_, lgai_basic_, lgai_edu_, lgai_pro_, "
            "lgai_ent_). The server rejects a key on shape BEFORE checking "
            f"validity, so this is not a permissions problem.\n  {API_KEY_HELP}"
        )
    return (
        "The API key is well-formed but was not accepted — it may be revoked, "
        f"expired, or for a different environment.\n  {API_KEY_HELP}"
    )


def _validate_api_key_shape(api_key: str | None) -> None:
    """WARN — loudly, naming the defect — on a key that cannot authenticate.

    ⛔ THIS WARNS RATHER THAN RAISING, DELIBERATELY. The first version of this
    check raised, and broke three existing tests that encode a real contract:
    ``LigandAI(api_key="not_a_valid_prefix").tier is None``. Tier detection
    deliberately FAILS OPEN on an unrecognised prefix and lets the server —
    which is authoritative — decide. Raising here would also lock out any
    future key prefix the platform introduces before this constant is updated.
    Refusing to construct was a wall this SDK had deliberately not built, and
    inventing it would have been its own defect.

    So the caller still gets a client, and still gets told. The actionable
    payoff lives where the failure actually happens: the 401 and the
    ``200``-with-``null`` both now carry :func:`auth_guidance` computed from
    the key that was sent, which is the only place absent / malformed /
    well-formed-but-invalid remain distinguishable.

    An absent key warns about nothing: anonymous clients are legitimate
    (``health()``, public endpoints). [bd-dre-rp11j.3]
    """
    if classify_api_key(api_key) != "malformed":
        return
    warnings.warn(
        "The LigandAI API key does not begin with 'lgai_', so it cannot "
        "authenticate: the server rejects a key on SHAPE before it checks "
        "validity, and answers with a generic failure that looks identical to "
        "supplying no key at all.\n"
        f"  {auth_guidance(api_key)}",
        LigandAIDiagnosticWarning,
        stacklevel=3,
    )


def _resolve_base_url(base_url: str | None) -> str:
    """Resolve the API base URL from the argument, then ``LIGANDAI_BASE_URL``.

    ⛔ [bd-dre-rp11j.3] ``LIGANDAI_BASE_URL`` used to be read by exactly two
    CLI sub-commands and by NOTHING in the library. So a user who exported it
    — as the SDK's own DNS-diagnosis message in ``_http.py`` explicitly
    instructs them to ("Set LIGANDAI_BASE_URL=... to override") — got a client
    that silently ignored it and went on calling the public edge. That is the
    accepted-but-inert class in its purest form: a documented knob that reads
    ON in the caller's environment and OFF in the request, with nothing
    saying so. The advice printed on the error path is now true.

    An UNUSABLE value raises instead of falling back. Silently substituting
    the default for a base URL the caller deliberately set would send the
    request somewhere other than where they said — the same defect one layer
    down, and the reason ``_wrap_transport_error`` refuses to rewrite a
    caller's host even when it knows the host is wrong.
    """
    if base_url is not None:
        candidate, origin = base_url, "base_url argument"
    else:
        env = os.environ.get("LIGANDAI_BASE_URL")
        if env is None or not env.strip():
            return DEFAULT_BASE_URL
        candidate, origin = env.strip(), "LIGANDAI_BASE_URL environment variable"

    parsed = urlsplit(candidate)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise LigandAIInvalidConfig(
            f"Unusable API base URL from the {origin}: {candidate!r}.\n"
            f"  expected     : an absolute http(s) URL, e.g. {DEFAULT_BASE_URL!r}\n"
            f"  got          : scheme={parsed.scheme or '<none>'!r} "
            f"host={parsed.netloc or '<none>'!r}\n"
            f"  what to do   : fix the value, or unset it to use the default. "
            f"The SDK will NOT fall back to the default on your behalf — "
            f"sending your request to a host you did not name would be worse "
            f"than refusing.",
            field="base_url",
            value=candidate,
            allowed=f"absolute http(s) URL (default {DEFAULT_BASE_URL})",
        )
    return candidate


def _resolve_impersonation() -> str | None:
    """Internal operator hook, resolved from the environment only.

    Not part of the public client API. The platform independently authorizes
    and scopes any privileged request, so this is inert for ordinary keys.
    """
    return os.environ.get("LIGANDAI_IMPERSONATE_USER") or None


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

    def __init__(
        self,
        api_key: str | None,
        base_url: str,
        *,
        default_organism: str | Species = DEFAULT_SPECIES,
        check_entitlement: bool = True,
    ) -> None:
        self._api_key = api_key
        self._tier = _detect_tier(api_key)
        self._base_url = base_url
        # lazy-initialized local dedupe + credit ledger.
        # Anonymous / un-authenticated clients never touch ~/.ligandai/.
        self._submitted_set: SubmittedSet | None = None
        self._credit_ledger: CreditLedger | None = None
        self._dedupe_lock = threading.Lock()
        self._api_key_hash: str | None = None
        # Client-level default species for species-aware calls (human | mouse),
        # normalized to the supported enum.
        self._default_species: Species = normalize_species(default_organism)
        self._check_entitlement = check_entitlement
        # Cached entitlement decision (None until first fetched). Sync + async
        # clients each populate it via their own ``entitlement()``.
        self._entitlement: SpeciesEntitlement | None = None

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

    # ─── Local dedupe + credit-ledger access ─────────

    @property
    def submitted_set(self) -> SubmittedSet:
        """Lazy ``~/.ligandai/submitted.db`` handle — created on first use.

        Used by the SDK's pre-submit dedupe and concurrency-cap checks. End
        users may inspect this directly to see in-flight submissions:

        .. code-block:: python

            for row in client.submitted_set.list_in_flight(client.api_key_hash):
                print(row['submission_hash'][:12], row['kind'], row['job_id'])
        """
        if self._submitted_set is None:
            with self._dedupe_lock:
                if self._submitted_set is None:
                    from ligandai._dedupe import SubmittedSet
                    self._submitted_set = SubmittedSet()
        return self._submitted_set

    @property
    def credit_ledger(self) -> CreditLedger:
        """Lazy ``~/.ligandai/credit_ledger.db`` handle — created on first use.

        Append-only audit trail of credit consumption. Use
        ``client.credit_ledger.recent_events(client.api_key_hash)`` to inspect.
        """
        if self._credit_ledger is None:
            with self._dedupe_lock:
                if self._credit_ledger is None:
                    from ligandai._dedupe import CreditLedger
                    self._credit_ledger = CreditLedger()
        return self._credit_ledger

    @property
    def api_key_hash(self) -> str:
        """SHA-256[:16] of the active api_key — safe to log, never invertible.

        Used as the partition key for the local sqlite dedupe + credit ledger
        so multiple api keys on the same machine don't collide.
        """
        if self._api_key_hash is None:
            from ligandai._dedupe import compute_api_key_hash
            self._api_key_hash = compute_api_key_hash(self._api_key)
        return self._api_key_hash

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

    # ─── Species / organism selection ────────────────────────────────────
    #
    # The species selector is an ENTITLED capability (superadmin or an org
    # granted ``species_targeting``). The client mirrors the server's
    # fail-closed rule: if the key is not entitled, ``mouse`` is coerced back to
    # ``human`` LOCALLY so the SDK never forces a species the server would
    # reject — and the SDK's own result labels match what the server does.
    #
    # ``entitlement()`` is the only network-bound piece and differs by client
    # flavor (sync vs async); it lives on the concrete ``LigandAI`` /
    # ``AsyncLigandAI`` subclasses. Everything below is pure and shared.

    @property
    def default_organism(self) -> Species:
        """The client-level default species (``"human"`` | ``"mouse"``)."""
        return self._default_species

    @staticmethod
    def _not_entitled_decision() -> SpeciesEntitlement:
        """Fail-closed entitlement: human only. Used on any lookup error."""
        return SpeciesEntitlement(
            success=False,
            entitled=False,
            is_super_admin=False,
            org_granted=False,
            species=["human"],
            default_species="human",
        )

    def _species_entitled(self) -> bool:
        """Whether the caller may select a non-default (mouse) species.

        Base behavior consults only the CACHED entitlement decision so it is
        safe for both sync and async clients. The sync client overrides this to
        lazily fetch ``GET /api/cross-species/entitlement`` on first use.

        When ``check_entitlement`` is disabled the client optimistically defers
        to the server (returns True) — the server remains the single source of
        truth and will still coerce a non-entitled caller.

        When no cached decision exists yet (async client that hasn't awaited
        ``entitlement()``), this returns False — fail-closed. The async caller
        should ``await client.entitlement()`` once to unlock a mouse selection;
        absent that, the client coerces to human locally and the server still
        enforces the real grant server-side.
        """
        if not self._check_entitlement:
            return True
        if self._entitlement is not None:
            return self._entitlement.entitled
        return False

    def _resolve_species(self, requested: str | Species | None) -> Species:
        """Resolve the EFFECTIVE species for a call (fail-closed).

        Mirrors the server's ``effectiveSpecies``: an entitled caller gets
        exactly the normalized species they asked for (or the client default); a
        non-entitled caller is coerced to ``"human"`` so a crafted
        ``organism="mouse"`` never takes effect. Applying this client-side keeps
        the SDK's own result labels consistent with what the server actually
        does.
        """
        species: Species = (
            normalize_species(requested)
            if requested is not None
            else self._default_species
        )
        if species == "mouse" and not self._species_entitled():
            return "human"
        return species

    def _apply_species(
        self, params: dict[str, Any], requested: str | Species | None
    ) -> Species:
        """Stamp the effective ``organism`` (+ ``species`` mirror) on a payload.

        "Flag mouse for mouse targets": when the effective species is mouse the
        request carries ``organism=mouse`` (and the mirror ``species=mouse``) so
        the server routes to the mouse atlas/ortholog path. Returns the
        effective species so the caller can surface it on the result.
        """
        species = self._resolve_species(requested)
        params["organism"] = species
        params["species"] = species
        return species

    def __repr__(self) -> str:
        tier = self._tier or "anonymous"
        return f"{type(self).__name__}(tier={tier!r}, base_url={self._base_url!r})"


class LigandAI(_ClientCommon):
    """Synchronous LIGANDAI client.

    Agents: call :meth:`guide` (``client.guide()`` or ``LigandAI.guide()``) for
    a concise map of the canonical workflows — including the built-in
    target-discovery (transcriptomics) funnel on ``client.discovery`` — and a
    pointer to the agent docs shipped in the package (``AGENTS.md`` and
    ``.claude/skills/ligandai/``). Reach for those native namespaces first
    rather than hand-stitching external data sources.

    Parameters
    ----------
    api_key
        API key prefixed with ``lgai_<tier>_*``. Defaults to env var
        ``LIGANDAI_API_KEY`` (or ``LIGANDAI_TEST_API_KEY`` for tests).
    base_url
        Override the default ``https://ligandai.com`` for local development or
        on-prem deployments. The platform responds on the apex domain —
        ``api.ligandai.com`` is **not** a published host, so do not point
        integrations there.
    timeout
        Per-request timeout in seconds (default 60).
    connect_timeout
        Explicit connect-phase timeout in seconds, distinct from and by
        default shorter than ``timeout`` — a dead TCP handshake fails fast
        independent of how long you're willing to wait for a slow-but-alive
        server. ``None`` (default) uses ``min(10, timeout)``.
        [bd-LIGANDAI_ALPHA_V2-x4vt8]
    max_retries
        Retries on 429/5xx/network errors (default 5). Only GET/HEAD/PUT/
        DELETE/OPTIONS are retried automatically; POST/PATCH are sent at
        most once unless the call passes an idempotency key, since retrying
        a POST whose response was merely lost (not its effect) risks a
        duplicate submission.
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
        # None = resolve from LIGANDAI_BASE_URL, else DEFAULT_BASE_URL.
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECS,
        # Explicit connect-phase timeout, distinct from (and by default
        # shorter than) `timeout` — a dead TCP handshake fails fast
        # independent of how long you're willing to wait for a slow-but-
        # alive server. None = min(10s, timeout). [bd-LIGANDAI_ALPHA_V2-x4vt8]
        connect_timeout: float | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        client_session_id: str | None = None,
        http_client: httpx.Client | None = None,
        default_organism: str | Species = DEFAULT_SPECIES,
        check_entitlement: bool = True,
    ) -> None:
        resolved_key = _resolve_api_key(api_key)
        _validate_api_key_shape(resolved_key)
        resolved_base_url = _resolve_base_url(base_url)
        super().__init__(
            resolved_key,
            resolved_base_url,
            default_organism=default_organism,
            check_entitlement=check_entitlement,
        )
        emit_update_notice()
        _log_client_init("LigandAI", resolved_base_url, self._tier, resolved_key)

        self._transport = HTTPTransport(
            api_key=resolved_key,
            base_url=resolved_base_url,
            timeout=timeout,
            connect_timeout=connect_timeout,
            max_retries=max_retries,
            impersonate_user=_resolve_impersonation(),
            client_session_id=client_session_id,
            client=http_client,
        )

        # Resource namespaces
        self.account: Account = Account(self._transport)
        # Structure/sequence analysis — interface residues, immunogenicity,
        # E. coli expression risk (POST /api/analysis/*, score-expression).
        self.analysis: Analysis = Analysis(self._transport, client=self)
        self.bivalent: Bivalent = Bivalent(self._transport, client=self)
        # 3dalk — linker_modifications + Mode B payload optimization.
        self.linker_modifications: LinkerModifications = LinkerModifications(
            self._transport, client=self,
        )
        # Recursive contrastive design loop: fold a target + off-target panel,
        # rank on delta ipSAE, replicate, feed survivors forward. [bd-dre-if562]
        self.campaigns: Campaigns = Campaigns(self._transport, client=self)
        self.charts: Charts = Charts(self._transport)
        self.diseases: Diseases = Diseases(self._transport)
        self.discovery: Discovery = Discovery(self._transport, client=self)
        self.folds: Folds = Folds(self._transport, client=self)
        # Ensemble co-fold: N peptides x M engines, priced before it runs,
        # returning a CONSENSUS rather than M leaderboards. Cofactors are a
        # first-class argument and an arm that cannot carry one is labelled,
        # never silently folded without it. [bd-dre-3jqok]
        self.ensemble: Ensemble = Ensemble(self._transport, client=self)
        self.jobs: Jobs = Jobs(self._transport)
        self.goals: Goals = Goals(self._transport, client=self)
        # Programmatic ToS/EULA review + acceptance (client.legal).
        self.legal: Legal = Legal(self._transport, client=self)
        # Small-molecule Kd scoring — free-tier accessible.
        self.ligands: Ligands = Ligands(self._transport, client=self)
        self.memory: Memory = Memory(self._transport)
        self.msa: MSA = MSA(self._transport)
        self.peptides: Peptides = Peptides(self._transport, client=self)
        # Singular alias: users (and AI assistants) reach for `client.peptide`.
        self.peptide: Peptides = self.peptides
        # DeltaForge scoring namespace (score_pdb / score_fold / batch_score_fold).
        self.deltaforge: DeltaForge = DeltaForge(self._transport, client=self)
        # Post-fold structural criteria filter + empirical evaluator + grounded chat.
        # [bd-LIGANDAI_ALPHA_V2-6bybp]
        self.evaluator: Evaluator = Evaluator(self._transport, client=self)
        self.programs: Programs = Programs(self._transport)
        self.proteins: Proteins = Proteins(self._transport)
        self.receptors: Receptors = Receptors(self._transport, client=self)
        self.reports: Reports = Reports(self._transport)
        self.structures: Structures = Structures(self._transport)
        self.synthesis: Synthesis = Synthesis(self._transport)
        self.validated_binder: ValidatedBinder = ValidatedBinder(self._transport)

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
        """Current credit balance — a **property**, not a method (no parens):
        ``client.credits`` (not ``client.credits()``). Does a lightweight
        ``GET /api/credits`` on each access.

        Returns
        -------
        int
            For finite accounts, the stored integer balance. For unlimited /
            superadmin accounts (where the server returns a sentinel ≥ 10
            billion), returns an :class:`~ligandai.types.UnlimitedCredits` —
            an ``int`` subclass that compares as effectively-infinite (so
            ``cost <= client.credits`` is ``True``) but **renders as
            ``"unlimited"``** instead of a misleading ``0`` or a giant
            sentinel. Test for it with ``client.credits.is_unlimited`` (always
            present on the unlimited variant) or
            ``getattr(client.credits, "is_unlimited", False)``.

        For the full balance object (raw ``balance``, ``is_unlimited``,
        ``monthly_allocation``, refill date) call the
        :meth:`~ligandai.resources.account.Account.credits` **method**:
        ``client.account.credits()``.

        Earlier releases returned a bare ``0`` here for unlimited accounts,
        which read as "out of credits" to users and agents. Returning an
        int-like ``"unlimited"`` value keeps arithmetic/comparison working
        while fixing the display.
        """
        c = self.account.credits()
        self._credits = c
        # Unlimited / superadmin sentinel: return an int-like value that
        # renders as "unlimited" rather than a bare 0 (misleading) or the raw
        # giant sentinel. The full Credits object on self._credits keeps the
        # raw balance + is_unlimited flag for callers who need them.
        if c.is_unlimited:
            return UnlimitedCredits(c.balance)
        return c.balance

    # ─── Rotating-JWT wallet methods ─────────────────────────────────────────

    def mint_wallet(
        self,
        scope: str,
        target_seq: str,
        count: int = 5,
        *,
        audience: str = "ligandai",
        wallet_path: Any = WALLET_PATH,
    ) -> KeyWallet:
        """Mint a wallet of ``count`` single-use JWTs from the server.

        Calls ``POST /api/auth/scoped-key/issue`` using the client's existing
        API key (legacy ``lgai_*_`` key) or session auth.  The resulting wallet
        is saved to ``~/.ligandai/keys.json`` (mode 0600) and returned.

        Parameters
        ----------
        scope
            ``"generate"``, ``"fold"``, or ``"score"``.
        target_seq
            Full amino-acid sequence of the target protein. Canonicalized and
            hashed server-side; the wallet is locked to this target.
        count
            Number of JWTs to mint (1-10). Default 5.
        audience
            ``"ligandai"`` (default), ``"biodefense"``, or ``"peptgames"``.
        wallet_path
            Override path for the wallet file. Defaults to
            ``~/.ligandai/keys.json``.

        Returns
        -------
        KeyWallet
            The freshly minted and saved wallet.
        """
        payload: dict[str, Any] = {
            "scope": scope,
            "target_seq": target_seq,
            "count": count,
            "audience": audience,
        }
        resp = self._transport.request("POST", "/api/auth/scoped-key/issue", json=payload) or {}
        wallet = KeyWallet.from_issue_response(resp, path=wallet_path)
        _logger.info(
            "Minted wallet: scope=%s count=%d target_hash=%s…",
            scope,
            wallet.remaining,
            wallet.target_hash[:12] if wallet.target_hash else "<none>",
        )
        return wallet

    def generate(
        self,
        target: str | None = None,
        method: str = "ligandforge",
        n_samples: int = 100,
        **kwargs: Any,
    ) -> Any:
        """Convenience wrapper for production LigandForge generation.

        This method intentionally routes through the mounted public SDK
        endpoint used by :meth:`client.peptides.generate`, not the experimental
        worker-invoke routes.  Pass a gene, PDB ID, or uploaded-variant target
        identifier here, or call ``client.peptides.generate(...)`` directly for
        the full typed surface.

        ``**kwargs`` are forwarded verbatim to
        :meth:`~ligandai.resources.peptides.Peptides.generate`, including
        ``model=`` — the generator model selector:

        * ``"v6.5.21"`` (default) — Receptor-conditioned. Designs respond to the
          receptor you select. Replaces v6.5, which was trained with all-zero
          receptor features and produced the same peptides regardless of target.
        * ``"v6.9.21"`` — A different, larger receptor feature space (107
          per-residue channels vs 48), with classifier-free guidance applied at
          inference. Validated for distinguishing sequence composition between
          receptors ONLY — not validated for WHERE on the structure the
          peptide binds (selection, not guidance, delivers placement), and NOT
          strictly better than v6.5 (measured contact_logits 0.6281 vs
          v6.5.21's 0.6719). The four guidance heads (immunogenicity,
          serum_stability, residue_composition, cellfree) ARE supported here —
          see :meth:`Peptides.generate` for the per-head defaults, ceilings and
          the residue_composition/proteolytic coupling. ``residue_composition``
          was called ``halflife`` through v0.11.x; both names are accepted and
          the old one warns.
        * ``"v6.5"`` — Legacy, deprecated. Trained with all-zero receptor
          features, so its output does not depend on the receptor you select.
          Retained only to reproduce runs made before August 2026.

        Omitting ``model`` sends no model key, so the server applies the
        platform default. See :meth:`Peptides.generate` for the full contract.
        A model known not to support a requested guidance head raises rather
        than silently dropping it; no model shipped with this SDK build
        currently triggers that.
        """
        method_key = method.lower().replace("_", "-")
        if method_key not in {"ligandforge", "ligandai", "ptf"}:
            raise NotImplementedError(
                "Top-level client.generate() currently supports production "
                "LigandForge generation only. Use client.peptides.generate(...) "
                "for the stable SDK API; experimental worker routes are not "
                "mounted on production."
            )

        gene = kwargs.pop("gene", None) or kwargs.pop("target_gene", None)
        if gene is None and target is not None:
            if _looks_like_target_sequence(target):
                raise ValueError(
                    "client.generate(target=<sequence>) would require the "
                    "experimental worker API, which is not mounted on "
                    "production. Use client.peptides.generate(gene=..., "
                    "variant_id=...) after registering or selecting a target."
                )
            gene = target
        if not gene:
            raise ValueError(
                "client.generate() requires a gene/PDB/target identifier. "
                "Pass target='EGFR' or call client.peptides.generate(gene='EGFR', ...)."
            )

        num_peptides = kwargs.pop("num_peptides", n_samples)
        return self.peptides.generate(gene=gene, num_peptides=num_peptides, **kwargs)

    def fold_batch(self, peptides: list[str], **kwargs: Any) -> Any:
        """Convenience wrapper for :meth:`client.peptides.fold_batch`.

        Submit N peptides against one fixed receptor for batch Boltz-2 folding.
        See :meth:`Peptides.fold_batch` for full parameter docs and examples.

        the SDK pre-validates ``gpu`` / ``gpu_type``
        kwargs and rejects anything other than ``"b200_plus"`` before
        delegating. Multi-GPU (2x/4x/8x), bare B200, H100, A100, etc. raise
        :class:`~ligandai.errors.LigandAIInvalidConfig` without a network
        round-trip.
        """
        # pre-flight GPU validation BEFORE any HTTP work. The
        # remaining kwargs are forwarded to Peptides.fold_batch which performs
        # its own dedupe + credit + concurrency checks.
        from ligandai._hardening import extract_and_validate_gpu
        extract_and_validate_gpu(kwargs)
        return self.peptides.fold_batch(peptides, **kwargs)

    def fold(
        self,
        target: str | list[Any] | None = None,
        peptide: str | None = None,
        **kwargs: Any,
    ) -> Any:
        """Convenience wrapper for production Boltz-2 folding.

        Supported forms:

        - ``client.fold(sequences=[...], target_gene="EGFR")``
        - ``client.fold(["SEQ1", "SEQ2"], target_gene="EGFR")``
        - ``client.fold(target_sequence, peptide_sequence)`` for a two-chain
          complex fold

        the SDK pre-validates ``gpu`` / ``gpu_type``
        kwargs and rejects anything other than ``"b200_plus"`` BEFORE
        delegating. Multi-GPU (2x/4x/8x), bare B200, H100, A100, etc. raise
        :class:`~ligandai.errors.LigandAIInvalidConfig`.
        """
        # pre-flight GPU validation BEFORE any HTTP work.
        from ligandai._hardening import extract_and_validate_gpu
        extract_and_validate_gpu(kwargs)
        sequences = kwargs.pop("sequences", None)
        if sequences is not None:
            if target is not None:
                raise ValueError("Pass either target positional data or sequences=, not both.")
            return self.peptides.fold(sequences=sequences, **kwargs)

        if peptide is not None:
            if target is None or not isinstance(target, str):
                raise ValueError("target must be the receptor sequence when peptide is provided.")
            # Flag the peptide chain so the server never prefetches an MSA for it.
            # De novo binders are orphan sequences — the local MSA service rejects
            # them as "Degenerate MSA: only 1 sequence" after a wasted mmseqs
            # search, which contributed to a concurrent-search saturation. The
            # receptor (chain A) still gets its MSA; the peptide is folded with
            # msa:empty by the worker. bd-LIGANDAI_ALPHA_V2-5319n.
            return self.peptides.fold(
                sequences=[
                    {"sequence": target, "chainId": "A", "name": "target"},
                    {
                        "sequence": peptide,
                        "chainId": "B",
                        "name": "peptide",
                        "type": "protein",
                        "isPeptide": True,
                        "is_peptide": True,
                        "use_msa": False,
                    },
                ],
                **kwargs,
            )

        if target is None:
            raise ValueError("client.fold() requires sequences=, a sequence list, or target+peptide.")
        if isinstance(target, list):
            return self.peptides.fold(sequences=target, **kwargs)
        return self.peptides.fold(sequences=[target], **kwargs)

    # ─── Species / organism entitlement (sync, network-bound) ────────────

    def entitlement(self, *, refresh: bool = False) -> SpeciesEntitlement:
        """Fetch (and cache) the caller's species-selector entitlement.

        Calls ``GET /api/cross-species/entitlement``. The result decides whether
        a ``mouse`` organism is honored by the server; the SDK caches it so the
        per-call fail-closed coercion is cheap. On any network/parse/HTTP error
        the SDK fails CLOSED — it returns a not-entitled decision (human only),
        exactly as the server does on a lookup error.

        Args:
            refresh: Re-fetch even if a cached decision exists.

        Returns:
            SpeciesEntitlement describing the grant.
        """
        if self._entitlement is not None and not refresh:
            return self._entitlement
        try:
            data = self._transport.request("GET", "/api/cross-species/entitlement") or {}
            self._entitlement = SpeciesEntitlement(**data)
        except LigandAILegalAcceptanceRequired:
            # ⛔ Fail-closed is right for an unavailable endpoint, wrong for the
            # legal gate. This runs on the PRIMARY generate/fold path (species
            # coercion), so swallowing it silently downgrades the user's
            # organism instead of telling them to accept the terms.
            raise
        except Exception:
            # Fail closed: an unavailable/erroring endpoint never grants mouse.
            self._entitlement = self._not_entitled_decision()
        return self._entitlement

    def _species_entitled(self) -> bool:
        """Sync override: lazily fetch the entitlement decision on first use."""
        if not self._check_entitlement:
            return True
        return self.entitlement().entitled

    def close(self) -> None:
        self._transport.close()

    def __enter__(self) -> LigandAI:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def health(self) -> dict[str, Any]:
        """Hit ``GET /api/healthz`` — useful for connectivity checks."""
        return self._transport.request("GET", "/api/healthz") or {}

    def check_compatibility(self) -> list[Diagnostic]:
        """Ask the platform whether this SDK build is still current.
        [bd-LIGANDAI_ALPHA_V2-x4vt8.3.2]

        Hits ``GET /api/sdk/compatibility`` and returns the resulting
        :class:`~ligandai.diagnostics.Diagnostic` list — ``[]`` when this client
        is current. Explicit and on demand; you normally do **not** need it,
        because the same advisory rides the ``X-LigandAI-SDK-*`` headers on
        every response the SDK already makes and surfaces itself as a
        :class:`~ligandai.errors.LigandAIVersionWarning`.

        Never upgrades anything and never raises on an older server that has no
        such endpoint — a 404 yields ``[]``.
        """
        try:
            payload = self._transport.request("GET", COMPATIBILITY_PATH) or {}
        except LigandAIError:
            return []
        return compatibility_diagnostics(payload)

    @staticmethod
    def guide(print_it: bool = True) -> str:
        """Print/return a concise map of the canonical LigandAI workflows.

        Start here if you're an agent poking the API instead of reading docs:
        it lists the target-discovery funnel (``client.discovery``), design,
        fold, score and synthesis chains, and points to the shipped
        ``AGENTS.md`` + ``.claude/skills/ligandai/`` docs. No network call.
        """
        from ligandai._guide import guide as _guide

        return _guide(print_it=print_it)


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
        # None = resolve from LIGANDAI_BASE_URL, else DEFAULT_BASE_URL.
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECS,
        # See LigandAI.__init__ — explicit connect-phase timeout.
        # [bd-LIGANDAI_ALPHA_V2-x4vt8]
        connect_timeout: float | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        client_session_id: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        default_organism: str | Species = DEFAULT_SPECIES,
        check_entitlement: bool = True,
    ) -> None:
        resolved_key = _resolve_api_key(api_key)
        _validate_api_key_shape(resolved_key)
        resolved_base_url = _resolve_base_url(base_url)
        super().__init__(
            resolved_key,
            resolved_base_url,
            default_organism=default_organism,
            check_entitlement=check_entitlement,
        )
        emit_update_notice()
        _log_client_init("AsyncLigandAI", resolved_base_url, self._tier, resolved_key)

        self._transport = AsyncHTTPTransport(
            api_key=resolved_key,
            base_url=resolved_base_url,
            timeout=timeout,
            connect_timeout=connect_timeout,
            max_retries=max_retries,
            impersonate_user=_resolve_impersonation(),
            client_session_id=client_session_id,
            client=http_client,
        )

        self.account: AsyncAccount = AsyncAccount(self._transport)
        # Structure/sequence analysis — interface residues, immunogenicity,
        # E. coli expression risk (POST /api/analysis/*, score-expression).
        self.analysis: AsyncAnalysis = AsyncAnalysis(self._transport, client=self)
        self.bivalent: AsyncBivalent = AsyncBivalent(self._transport, client=self)
        # 3dalk — linker_modifications + Mode B payload optimization.
        self.linker_modifications: AsyncLinkerModifications = AsyncLinkerModifications(
            self._transport, client=self,
        )
        self.campaigns: AsyncCampaigns = AsyncCampaigns(self._transport, client=self)
        self.charts: AsyncCharts = AsyncCharts(self._transport)
        self.diseases: AsyncDiseases = AsyncDiseases(self._transport)
        self.discovery: AsyncDiscovery = AsyncDiscovery(self._transport, client=self)
        self.folds: AsyncFolds = AsyncFolds(self._transport, client=self)
        self.ensemble: AsyncEnsemble = AsyncEnsemble(self._transport, client=self)
        self.jobs: AsyncJobs = AsyncJobs(self._transport)
        self.goals: AsyncGoals = AsyncGoals(self._transport, client=self)
        # Programmatic ToS/EULA review + acceptance (client.legal).
        self.legal: AsyncLegal = AsyncLegal(self._transport, client=self)
        # Small-molecule Kd scoring — free-tier accessible.
        self.ligands: AsyncLigands = AsyncLigands(self._transport, client=self)
        self.memory: AsyncMemory = AsyncMemory(self._transport)
        self.msa: AsyncMSA = AsyncMSA(self._transport)
        self.peptides: AsyncPeptides = AsyncPeptides(self._transport, client=self)
        # singular alias + DeltaForge scoring namespace.
        self.peptide: AsyncPeptides = self.peptides
        self.deltaforge: AsyncDeltaForge = AsyncDeltaForge(self._transport, client=self)
        # Post-fold structural criteria filter + empirical evaluator + grounded chat.
        # [bd-LIGANDAI_ALPHA_V2-6bybp]
        self.evaluator: AsyncEvaluator = AsyncEvaluator(self._transport, client=self)
        self.programs: AsyncPrograms = AsyncPrograms(self._transport)
        self.proteins: AsyncProteins = AsyncProteins(self._transport)
        self.receptors: AsyncReceptors = AsyncReceptors(self._transport, client=self)
        self.reports: AsyncReports = AsyncReports(self._transport)
        self.structures: AsyncStructures = AsyncStructures(self._transport)
        self.synthesis: AsyncSynthesis = AsyncSynthesis(self._transport)
        self.validated_binder: AsyncValidatedBinder = AsyncValidatedBinder(self._transport)

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

    async def fold_batch(self, peptides: list[str], **kwargs: Any) -> Any:
        """Async convenience wrapper for :meth:`AsyncPeptides.fold_batch`.

        GPU pre-validation runs before delegation —
        passing ``gpu="b200_2x"`` etc. raises
        :class:`~ligandai.errors.LigandAIInvalidConfig` without a round-trip.
        """
        from ligandai._hardening import extract_and_validate_gpu
        extract_and_validate_gpu(kwargs)
        return await self.peptides.fold_batch(peptides, **kwargs)

    # ─── Species / organism entitlement (async, network-bound) ───────────

    async def entitlement(self, *, refresh: bool = False) -> SpeciesEntitlement:
        """Async variant of :meth:`LigandAI.entitlement`.

        Fetches (and caches) ``GET /api/cross-species/entitlement``. Await this
        once to unlock a ``mouse`` selection on the async client; until then the
        client fails closed to ``human`` locally (the server still enforces the
        real grant regardless). Fails CLOSED (human only) on any error.
        """
        if self._entitlement is not None and not refresh:
            return self._entitlement
        try:
            data = await self._transport.request(
                "GET", "/api/cross-species/entitlement"
            ) or {}
            self._entitlement = SpeciesEntitlement(**data)
        except LigandAILegalAcceptanceRequired:
            raise  # see the sync twin — never downgrade species on a legal 403
        except Exception:
            self._entitlement = self._not_entitled_decision()
        return self._entitlement

    async def close(self) -> None:
        await self._transport.close()

    async def __aenter__(self) -> AsyncLigandAI:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def health(self) -> dict[str, Any]:
        return await self._transport.request("GET", "/api/healthz") or {}

    async def check_compatibility(self) -> list[Diagnostic]:
        """Async twin of :meth:`LigandAI.check_compatibility`."""
        try:
            payload = await self._transport.request("GET", COMPATIBILITY_PATH) or {}
        except LigandAIError:
            return []
        return compatibility_diagnostics(payload)

    async def me(self) -> User:
        return await self.account.me()

    @staticmethod
    def guide(print_it: bool = True) -> str:
        """Concise map of the canonical LigandAI workflows (incl. the
        ``discovery`` funnel) + pointer to the shipped ``AGENTS.md`` /
        ``.claude/skills/ligandai/`` docs. See :meth:`LigandAI.guide`. No I/O."""
        from ligandai._guide import guide as _guide

        return _guide(print_it=print_it)


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
