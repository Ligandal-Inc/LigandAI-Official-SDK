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
    "free": 1,
    "basic": 4,
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

# Job polling defaults.
DEFAULT_POLL_INTERVAL_SECS: float = 2.0
DEFAULT_JOB_TIMEOUT_SECS: float = 1800.0  # 30 minutes — generation+fold can take this long


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
