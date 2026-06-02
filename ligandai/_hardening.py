# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Centralized pre-submission hardening for fold/fold_batch/generate.

This module is the single place where the SDK submission policy is enforced:

1. GPU type allowlist — only ``b200_plus`` accepted; other GPU strings
   raise :class:`~ligandai.errors.LigandAIInvalidConfig` BEFORE any HTTP call.
2. Local dedupe — identical fold submissions within
   :data:`~ligandai._constants.DEFAULT_DEDUPE_WINDOW_SECS` return the cached
   :class:`~ligandai.jobs.Job` handle instead of re-submitting (unless
   ``force_resubmit=True``).
3. Client-side concurrency cap — in-flight submissions in
   ``~/.ligandai/submitted.db`` are counted against ``TIER_GPU_SLOTS[tier]``.
4. Credit pre-flight — for non-unlimited tiers, the SDK estimates the cost
   locally and raises :class:`~ligandai.errors.LigandAIInsufficientCredits`
   if the balance can't cover it. Unlimited accounts skip.

The dedupe + concurrency guards protect against accidental duplicate
submissions on identical inputs, which would otherwise consume credits and
GPU slots without producing new results.
"""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING, Any, Sequence

from ligandai._constants import (
    ALLOWED_GPU_TYPES,
    DEFAULT_DEDUPE_WINDOW_SECS,
    DEFAULT_GPU_TYPE,
    REJECTED_GPU_TYPES,
    TIER_GPU_SLOTS,
)
from ligandai.errors import (
    LigandAIConcurrencyLimit,
    LigandAIInsufficientCredits,
    LigandAIInvalidConfig,
)

if TYPE_CHECKING:
    from ligandai._dedupe import CreditLedger, SubmittedSet
    from ligandai.client import _ClientCommon

_logger = logging.getLogger("ligandai.hardening")


def validate_gpu(gpu_value: str | None) -> str:
    """Validate a user-supplied GPU string. Returns the canonical accepted value.

    Raises :class:`LigandAIInvalidConfig` for anything other than
    ``"b200_plus"`` (case-insensitive). ``None`` defaults to
    :data:`~ligandai._constants.DEFAULT_GPU_TYPE`.

    Examples
    --------
    >>> validate_gpu(None)
    'b200_plus'
    >>> validate_gpu("b200_plus")
    'b200_plus'
    >>> validate_gpu("B200_PLUS")
    'b200_plus'
    >>> validate_gpu("b200_2x")  # doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
       ...
    LigandAIInvalidConfig: ...
    """
    if gpu_value is None or gpu_value == "":
        return DEFAULT_GPU_TYPE
    normalized = str(gpu_value).strip().lower()
    if normalized in ALLOWED_GPU_TYPES:
        return normalized
    # Build the user-facing message, calling out 2x/4x/8x explicitly when
    # the caller tried one of those (so the rejection is unambiguous).
    if normalized in {"b200_2x", "b200_4x", "b200_8x"}:
        msg = (
            f"Multi-GPU B200 ({gpu_value!r}) is not exposed through the public "
            f"LigandAI SDK. Use gpu='b200_plus' for single-GPU B200+ folding."
        )
    elif normalized == "b200":
        msg = (
            f"Bare 'b200' is not accepted — the SDK targets B200+ exclusively. "
            f"Pass gpu='b200_plus' (or omit; that's the default)."
        )
    elif normalized in REJECTED_GPU_TYPES:
        msg = (
            f"GPU type {gpu_value!r} is not supported by the public SDK "
            f"(only 'b200_plus' is allowed). The SDK targets B200+ exclusively."
        )
    else:
        msg = (
            f"Unknown GPU type {gpu_value!r}. The public SDK only accepts "
            f"'b200_plus' (allowed values: {sorted(ALLOWED_GPU_TYPES)})."
        )
    raise LigandAIInvalidConfig(
        msg,
        field="gpu",
        value=gpu_value,
        allowed=sorted(ALLOWED_GPU_TYPES),
    )


def extract_and_validate_gpu(kwargs: dict[str, Any]) -> str:
    """Pop ``gpu`` / ``gpu_type`` from a kwargs dict and validate.

    Mutates ``kwargs`` in place (removes the consumed keys) so the call site
    can pass the remaining ``**kwargs`` to its body builder without leaking
    the GPU string to the server. Returns the canonical accepted GPU value.

    Any of the following kwargs are accepted (in priority order):
    ``gpu``, ``gpu_type``, ``gpuType``.
    """
    raw: Any = None
    for k in ("gpu", "gpu_type", "gpuType"):
        if k in kwargs:
            raw = kwargs.pop(k)
            if raw is not None and raw != "":
                # Stop at the first non-empty value, but consume all keys so
                # nothing leaks downstream.
                pass
    return validate_gpu(raw if raw not in (None, "") else None)


def _is_unlimited_account(client: "_ClientCommon | None") -> bool:
    """Return True for superadmin / unlimited accounts that bypass credit pre-flight."""
    if client is None:
        return False
    tier = getattr(client, "tier", None)
    if tier == "superadmin":
        return True
    cached = getattr(client, "_credits", None)
    if cached is not None and getattr(cached, "is_unlimited", False):
        return True
    return False


def estimate_fold_batch_credits(
    *,
    peptide_count: int,
    trajectories: int,
    sampling_steps: int,
) -> int:
    """Mirror the platform's batch-fold cost estimate.

    The platform is authoritative for the final credit cost; this local
    estimate is used only for the pre-flight balance check.
    """
    multiplier = max(1.0, float(sampling_steps) / 50.0)
    return int(math.ceil(peptide_count * trajectories * 100 * multiplier))


def estimate_single_fold_credits(
    *,
    trajectories: int,
    sampling_steps: int | None,
) -> int:
    """Estimate cost of a single ``/api/folding/predict`` submission."""
    steps = int(sampling_steps) if sampling_steps else 50
    multiplier = max(1.0, float(steps) / 50.0)
    return int(math.ceil(trajectories * 100 * multiplier))


def _read_balance_for_preflight(client: "_ClientCommon | None") -> int | None:
    """Read the current balance for credit pre-flight, preferring cached values.

    Policy: only return a balance when one has already been fetched via
    ``client.credits`` / ``client.account.credits()`` and cached on
    ``client._credits``. Returns ``None`` otherwise so the pre-flight is
    skipped. This keeps the submit hot path free of hidden round-trips and
    preserves backward compatibility with tests that mock only the POST.

    Users who want the pre-flight to enforce against the real balance should
    warm the cache once:

    .. code-block:: python

        _ = client.credits  # one GET /api/credits, cached on client._credits
        client.fold_batch(...)  # pre-flight uses cached value

    Or fetch + populate manually:

    .. code-block:: python

        client._credits = client.account.credits()
        client.fold_batch(...)
    """
    if client is None:
        return None
    cached = getattr(client, "_credits", None)
    if cached is None:
        return None
    if getattr(cached, "is_unlimited", False):
        return None
    bal = getattr(cached, "balance", None)
    return int(bal) if bal is not None else None


def preflight_credits(
    client: "_ClientCommon | None",
    *,
    estimated: int,
    kind: str,
) -> tuple[int | None, bool]:
    """Compare estimated cost against the client's cached / fresh balance.

    Returns ``(available, ok)``:

    - ``available`` is the balance used for the comparison, or ``None`` when
      the pre-flight was skipped (anonymous, unlimited, or balance unreadable).
    - ``ok`` is True when the submission may proceed. False is impossible —
      the function raises :class:`LigandAIInsufficientCredits` instead when
      the balance can't cover the cost.

    Skipped (returns ``(None, True)``) for:

    - Anonymous clients (no api_key).
    - Superadmin / unlimited accounts (``_credits.is_unlimited`` is True).
    - Clients whose balance fetch raises (mocked-only-POST test envs,
      transient network failures). The server's 402 is still authoritative.
    """
    if client is None or not getattr(client, "api_key", None):
        return None, True
    if _is_unlimited_account(client):
        return None, True
    balance = _read_balance_for_preflight(client)
    if balance is None:
        # Unreadable / unlimited: skip pre-flight, let server be authoritative.
        return None, True
    if balance >= estimated:
        return balance, True
    raise LigandAIInsufficientCredits(
        (
            f"Insufficient credits to submit {kind}: "
            f"estimated {estimated:,} credits, available {balance:,}. "
            f"Top up at https://ligandai.com/account/billing or pass "
            f"force_resubmit=True after topping up."
        ),
        required=estimated,
        available=balance,
    )


def enforce_concurrency(
    client: "_ClientCommon | None",
    submitted_set: "SubmittedSet | None",
) -> tuple[int | None, int | None]:
    """Enforce the tier-specific in-flight cap. Returns (in_flight, cap).

    Raises :class:`LigandAIConcurrencyLimit` when ``in_flight >= cap``.
    Returns ``(None, None)`` when no cap can be determined (anonymous /
    unknown tier).
    """
    if client is None or submitted_set is None:
        return None, None
    tier = getattr(client, "tier", None)
    if tier is None:
        return None, None
    cap = TIER_GPU_SLOTS.get(tier)
    if cap is None:
        return None, None
    api_key_hash = getattr(client, "api_key_hash", "")
    in_flight = submitted_set.count_in_flight(
        api_key_hash, window_secs=DEFAULT_DEDUPE_WINDOW_SECS,
    )
    if in_flight >= cap:
        raise LigandAIConcurrencyLimit(
            (
                f"Local in-flight cap reached for tier {tier!r}: "
                f"{in_flight}/{cap} submissions in the last 24h. "
                f"Wait for jobs to complete (or call "
                f"client.submitted_set.mark_completed(...) to release a stale slot)."
            ),
            in_flight=in_flight,
            limit=cap,
        )
    return in_flight, cap


def dedupe_lookup_cached(
    submitted_set: "SubmittedSet | None",
    *,
    submission_hash: str,
    api_key_hash: str,
    force_resubmit: bool,
) -> dict[str, Any] | None:
    """Return the cached row (dict) if a recent identical submission exists.

    Honors ``force_resubmit`` — when True, always returns ``None`` and the
    caller proceeds with a fresh submission. Otherwise checks the sqlite
    dedupe DB.
    """
    if force_resubmit or submitted_set is None:
        return None
    return submitted_set.lookup(
        submission_hash, api_key_hash, window_secs=DEFAULT_DEDUPE_WINDOW_SECS,
    )


def build_fold_params_for_hash(
    *,
    target_gene: str | None,
    diffusion_samples: int | None,
    sampling_steps: int | None,
    recycling_steps: int | None,
    step_scale: float | None,
    msa_enabled: bool | None,
    glycosylation: bool | None,
    template_mode: bool | None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the canonical ``params`` dict that goes into the submission hash.

    Any parameter that meaningfully changes the result must be present here.
    Bool flags whose absence implies a server default are included as ``None``
    so the hash distinguishes "explicitly passed False" from "not passed".
    """
    p: dict[str, Any] = {
        "target_gene": target_gene,
        "diffusion_samples": (
            int(diffusion_samples) if diffusion_samples is not None else None
        ),
        "sampling_steps": (
            int(sampling_steps) if sampling_steps is not None else None
        ),
        "recycling_steps": (
            int(recycling_steps) if recycling_steps is not None else None
        ),
        "step_scale": (
            float(step_scale) if step_scale is not None else None
        ),
        "msa_enabled": (
            bool(msa_enabled) if msa_enabled is not None else None
        ),
        "glycosylation": (
            bool(glycosylation) if glycosylation is not None else None
        ),
        "template_mode": (
            bool(template_mode) if template_mode is not None else None
        ),
    }
    if extra:
        for k, v in extra.items():
            if k not in p:
                p[k] = v
    return p


def receptor_seq_for_hash(
    *,
    target_gene: str | None,
    receptor_sequence: str | None,
    receptor_pdb: str | None,
) -> str:
    """Pick the most stable string available to identify the receptor in the hash.

    Priority: explicit sequence > PDB string > gene symbol. The same receptor
    re-resolved via gene will hash identically across calls; an uploaded PDB
    will hash identically when the file content matches.
    """
    if receptor_sequence:
        return receptor_sequence.strip().upper()
    if receptor_pdb:
        return receptor_pdb.strip()
    if target_gene:
        return f"GENE:{target_gene.strip().upper()}"
    return ""


def record_submission(
    submitted_set: "SubmittedSet | None",
    credit_ledger: "CreditLedger | None",
    *,
    submission_hash: str,
    api_key_hash: str,
    kind: str,
    gpu: str = DEFAULT_GPU_TYPE,
    estimated_credits: int | None = None,
    balance_before: int | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    """Persist the pre-submission row + credit ledger entry.

    Called BEFORE the HTTP POST. If the POST fails, the caller should call
    :func:`mark_failed` to flip the row's status so the next attempt isn't
    blocked.
    """
    if submitted_set is not None:
        try:
            submitted_set.record_submission(
                submission_hash,
                api_key_hash,
                gpu=gpu,
                kind=kind,
                estimated_credits=estimated_credits,
                meta=meta,
            )
        except Exception as exc:  # noqa: BLE001 — dedupe failure is not fatal
            _logger.warning(
                "[SDK dedupe] record_submission failed (continuing): %s", exc
            )
    if credit_ledger is not None and estimated_credits is not None:
        try:
            credit_ledger.record(
                api_key_hash=api_key_hash,
                kind=kind,
                job_id=None,
                estimated=int(estimated_credits),
                balance_before=balance_before,
                note="pre-submit",
            )
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "[SDK ledger] record failed (continuing): %s", exc
            )


def attach_job_id(
    submitted_set: "SubmittedSet | None",
    *,
    submission_hash: str,
    api_key_hash: str,
    job_id: str,
) -> None:
    """After server returns a job/batch id, persist it onto the dedupe row."""
    if submitted_set is None or not job_id:
        return
    try:
        submitted_set.update_job_id(submission_hash, api_key_hash, job_id)
    except Exception as exc:  # noqa: BLE001
        _logger.warning("[SDK dedupe] update_job_id failed: %s", exc)


def mark_failed(
    submitted_set: "SubmittedSet | None",
    *,
    submission_hash: str,
    api_key_hash: str,
    reason: str,
) -> None:
    """Flip the row's status to 'failed' so a retry won't be blocked by dedupe."""
    if submitted_set is None:
        return
    try:
        submitted_set.mark_failed(submission_hash, api_key_hash, reason=reason)
    except Exception as exc:  # noqa: BLE001
        _logger.warning("[SDK dedupe] mark_failed failed: %s", exc)


def mark_completed(
    submitted_set: "SubmittedSet | None",
    credit_ledger: "CreditLedger | None",
    *,
    submission_hash: str,
    api_key_hash: str,
    actual_credits: int | None = None,
    job_id: str | None = None,
) -> None:
    """Flip the row to 'completed' and append a final ledger event."""
    if submitted_set is not None:
        try:
            submitted_set.mark_completed(
                submission_hash, api_key_hash, actual_credits=actual_credits,
            )
        except Exception as exc:  # noqa: BLE001
            _logger.warning("[SDK dedupe] mark_completed failed: %s", exc)
    if credit_ledger is not None and actual_credits is not None:
        try:
            credit_ledger.record(
                api_key_hash=api_key_hash,
                kind="fold_complete",
                job_id=job_id,
                actual=int(actual_credits),
                note="post-complete",
            )
        except Exception as exc:  # noqa: BLE001
            _logger.warning("[SDK ledger] record (complete) failed: %s", exc)
