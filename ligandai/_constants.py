# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Constants mirrored from the LIGANDAI platform.

These values mirror the platform's authoritative tier prefixes, tier
features, generation limits, and fold limits. The platform remains the
source of truth and is authoritative at request time; the values here are a
client-side convenience for pre-flight checks and informative error messages.
"""

from __future__ import annotations

from typing import Literal

# Tier identifiers inferred from API key prefixes.
Tier = Literal["free", "basic", "academia", "pro", "enterprise", "superadmin"]

API_KEY_PREFIXES: dict[Tier, str] = {
    "free": "lgai_free_",
    "basic": "lgai_basic_",
    "academia": "lgai_edu_",
    "pro": "lgai_pro_",
    "enterprise": "lgai_ent_",
    "superadmin": "lgai_sa_",
}

# Tier ordering, lowest to highest privilege.
TIER_ORDER: tuple[Tier, ...] = ("free", "basic", "academia", "pro", "enterprise", "superadmin")

# Rate limits per minute by API key tier.
TIER_RATE_LIMITS: dict[Tier, int] = {
    "free": 10,
    "basic": 20,
    "academia": 30,
    "pro": 60,
    "enterprise": 300,
    "superadmin": 1000,
}

# Feature → minimum tier mapping (mirrors TIER_FEATURES in the platform).
# This lets the SDK raise LigandAITierError client-side without a round-trip
# when a method is called against a key that doesn't have the required feature.
FEATURE_MIN_TIER: dict[str, Tier] = {
    # Free tier
    "search_receptors": "free",
    "view_structure": "free",
    "get_job_status": "free",
    "generate_peptides": "free",
    "predict_structure": "free",
    "predict_hotspots": "free",
    # Basic tier
    "standard_generation": "basic",
    # Academia tier
    "advanced_guidance": "academia",
    "logits_output": "academia",
    # PAE (Predicted Aligned Error) — academia+
    "pae_download": "academia",
    "pae_viewer": "academia",
    # pae_summary is open to all tiers (no gate)
    # Pro tier
    "analyze_binding": "pro",
    "bivalent_design": "pro",
    "transcriptomics_analysis": "pro",
    # Linker modifications + unnatural amino acids + payload optimization.
    # The platform enforces the pro-tier gate; the SDK mirrors it so calls
    # fail fast on non-pro keys without a round trip.
    "linker_modifications": "pro",
    "payload_optimization": "pro",
    # Enterprise-only
    "batch_operations": "enterprise",
    "priority_queue": "enterprise",
    "custom_models": "enterprise",
    "transport_vasculome": "enterprise",
}

# Generation limits by tier-visible design count (mirrors the platform tier
# limits). The SDK uses the tier-visible count; the platform is authoritative.
TIER_GENERATION_LIMITS: dict[Tier, int] = {
    "free": 10,
    "basic": 1000,
    "academia": 5000,
    "pro": 5000,
    "enterprise": 25000,
    "superadmin": 25000,
}

# Fold limits by tier. Paid tiers do not have a lower SDK-side fold cap; the
# platform remains authoritative for credits, generation allowance, and abuse
# guards.
TIER_FOLD_LIMITS: dict[Tier, int | None] = {
    "free": 10,
    "basic": None,
    "academia": None,
    "pro": None,
    "enterprise": None,  # unlimited
    "superadmin": None,
}

# Target count limits by tier.
TIER_TARGET_LIMITS: dict[Tier, int | None] = {
    "free": 3,
    "basic": None,
    "academia": None,
    "pro": None,
    "enterprise": None,
    "superadmin": None,
}

# Default fold trajectories (Boltz-2 diffusion samples).
DEFAULT_TRAJECTORIES: int = 4

# Folding GPU caps by tier. Generation is submitted as a one-GPU job; these caps
# apply to folding and parallel fold batches.
TIER_GPU_SLOTS: dict[Tier, int] = {
    "free": 4,
    "basic": 8,
    "academia": 16,
    "pro": 25,
    "enterprise": 50,
    "superadmin": 50,
}

# Default URLs.
DEFAULT_BASE_URL: str = "https://ligandai.com"
DEFAULT_RECEPTORDB_URL: str = "https://receptordb.com"

# HTTP defaults.
DEFAULT_TIMEOUT_SECS: float = 60.0
DEFAULT_MAX_RETRIES: int = 5
DEFAULT_RETRY_BASE_DELAY: float = 1.0
DEFAULT_RETRY_MAX_DELAY: float = 30.0

# Explicit connect timeout, distinct from (and shorter than) the overall
# read/write/pool timeout above — a dead TCP handshake should fail fast even
# when the caller configured a generous ``timeout`` for a long-running GPU
# job's status polling. Caller-overridable via ``connect_timeout=`` on
# ``HTTPTransport`` / ``AsyncHTTPTransport`` / ``LigandAI`` / ``AsyncLigandAI``.
# [bd-LIGANDAI_ALPHA_V2-x4vt8]
DEFAULT_CONNECT_TIMEOUT_SECS: float = 10.0

# ─── Residue-targeting client-side validation (bd-LIGANDAI_ALPHA_V2-x4vt8) ──
#
# ``target_residues`` / ``avoid_residues`` / ``hotspot_residues`` are PDB
# residue numbers as numbered in the uploaded structure — never 0-based array
# indices. See ``ligandai._targeting`` for the validator that enforces this
# client-side, matching fastapi_backend/core/residue_targeting.py and the
# Node SDK byte-for-byte (docs/x4vt8_contract.md §4/§6).

# Ca-Ca packing-shell radius grown around ``avoid_residues`` server-side.
# Mirrors ``residue_targeting.DEFAULT_SHELL_CA_ANGSTROM``. 0.0 disables the
# shell (named residues only, no neighbour exclusion).
DEFAULT_AVOID_SHELL_ANGSTROM: float = 8.0
MIN_AVOID_SHELL_ANGSTROM: float = 0.0
# A shell this large would swallow most single-domain receptors — the
# contract's explicit sanity ceiling.
MAX_AVOID_SHELL_ANGSTROM: float = 50.0

# A region spanning more residues than this is almost certainly a
# transposed-argument / index-confusion mistake (e.g. passing an array
# length as ``end``), not a legitimate pocket — reject it client-side rather
# than sending a nonsensical multi-thousand-residue "pocket" to the server.
MAX_REGION_SPAN_RESIDUES: int = 5000

# Job polling defaults.
DEFAULT_POLL_INTERVAL_SECS: float = 2.0
DEFAULT_JOB_TIMEOUT_SECS: float = 1800.0  # 30 minutes — generation+fold can take this long

# How long Job.wait() keeps polling through consecutive HTTP 404 "job not found"
# answers before it believes them. The hosted co-fold job store is per-process
# (several API workers, no shared store), so a status poll that lands on a worker
# that does not hold the job answers 404 for a job the API is still running.
# MEASURED 2026-10-02 (four EGFR co-fold smoke runs against the hosted route):
# 0, 7, 12 and 60 such answers per run, and the longest unbroken streak was 54
# polls over 530 s that ended with the job completing normally. 120 s would have
# failed that run; 600 s clears it. A genuinely lost job (expired, or the server
# restarted) costs one grace window before the error, never an unbounded wait,
# and the overall ``timeout`` still bounds the whole call. Pass
# ``not_found_grace=0`` to ``wait()`` to restore fail-on-first-404.
DEFAULT_NOT_FOUND_GRACE_SECS: float = 600.0

# Legal-acceptance interactive resolution defaults (SDK-side browser-redirect
# + resume flow, docs/clickwrap_legal_acceptance.md §3.6). Distinct from the
# job-polling defaults above: this is a human clicking through a clickwrap
# dialog in a browser, not a GPU job — hence a shorter poll interval (react
# quickly once the human clicks accept) and a much shorter hard timeout (300s
# is generous for a browser click; a stuck GPU job legitimately needs 30 min).
DEFAULT_LEGAL_POLL_INTERVAL_SECS: float = 3.0
DEFAULT_LEGAL_POLL_TIMEOUT_SECS: float = 300.0


# ─── GPU policy ───────────────────────────────────────────────────────────────
#
# The public SDK exposes a single GPU class: ``b200_plus`` (single-GPU B200+),
# which is the default and minimum. This is enforced client-side at the call
# boundary (see ligandai/client.py + ligandai/resources/peptides.py) so a
# custom HTTP layer cannot bypass it without removing the SDK guard, and the
# platform independently rejects any other value — defense in depth.
#
# The local dedupe + concurrency guards prevent accidental duplicate
# submissions on identical inputs from consuming credits and GPU slots.

GpuType = Literal["b200_plus"]  # additional SKUs are not exposed by the public SDK

# Allowed GPU strings the SDK will forward. Anything else → LigandAIInvalidConfig.
ALLOWED_GPU_TYPES: frozenset[str] = frozenset({"b200_plus"})

# Hard-rejected GPU strings. Multi-GPU and smaller-GPU classes are never
# callable through the public SDK. Listed explicitly so the rejection error
# message can name what was tried.
REJECTED_GPU_TYPES: frozenset[str] = frozenset({
    "b200_2x", "b200_4x", "b200_8x",      # explicitly forbidden multi-GPU
    "b200",                                # bare b200 — must use b200_plus
    "a100", "a100_40gb", "a100_80gb",     # smaller GPUs not supported
    "h100", "h100_80gb",                   # H100 not in SDK surface
    "l4", "l40", "l40s", "t4",             # cheaper GPUs
    "cpu",                                 # CPU only used by test workers
})

DEFAULT_GPU_TYPE: GpuType = "b200_plus"

# Default dedupe window — second identical fold submission within this window
# returns the cached Job handle instead of re-submitting. Override at the
# call site with force_resubmit=True.
DEFAULT_DEDUPE_WINDOW_SECS: int = 24 * 3600  # 24 hours

# Stale-submitted reaper window — a 'submitted' row with no job_id older than
# this is treated as orphaned (SSE disconnect, crashed POST) and ignored by
# lookup(). Prevents permanent lockout when the network drops mid-submit.
SUBMITTED_ORPHAN_SECS: int = 3600  # 1 hour

# Local-state DB paths. Both are sqlite, kept under ~/.ligandai/ with mode 0600
# and parent dir 0700.
LOCAL_STATE_DIR_NAME: str = ".ligandai"
SUBMITTED_DB_NAME: str = "submitted.db"
CREDIT_LEDGER_DB_NAME: str = "credit_ledger.db"

# ── The elite bar ───────────────────────────────────────────────────────────
# ⛔ SOURCE OF TRUTH: ``shared/peptide-ipsae.ts`` on the platform, constant
# ``ELITE_PEPTIDE_IPSAE_THRESHOLD``. The platform applies it in exactly one
# place — ``isEliteByBinderIpsae()`` called from ``storage.insertPtfFoldResult``
# — so every ``ptf_fold_results.is_elite`` flag a customer has ever seen was
# computed against this number, on the BINDER (peptide-receptor interface)
# iPSAE, never the inflated whole-complex value.
#
# Mirrored here so SDK docstrings and agent skills can cite ONE place instead of
# retyping a literal. The SDK docs said 0.80 for a long while; the gate never
# enforced 0.80, and 0.80 was explicitly rejected because it would have demoted
# genuine binders in [0.66, 0.80). [bd-dre-f29kd]
#
# ⛔ DISTINCT from the acceptance-gate TIER also spelled "elite"
# (``peptide_ipsae >= 0.85``, ``shared/acceptance-gate.json``). Same word, two
# rules: this constant is the legacy single-metric server FLAG; prefer
# ``Peptide.quality_tier`` for the acceptance ladder.
IS_ELITE_PEPTIDE_IPSAE_THRESHOLD: float = 0.66
