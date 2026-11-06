"""Regression: EnsembleResult.credits_spent reports the AUTHORITATIVE server
charge (Tom ask D, item 5 — thread 1a1ad2089ad625c0: quoted AND charged 45
credits while the ensemble result had no credits_spent field value).

The server's settlement writes a usage_gpu row per co-fold job with
``metadata.jobId`` and the consumed (negative) amount; attribution sums those
rows for THIS run's jobs only. Unknown/unsettled jobs → None or partial,
never a guessed total. [bd-LIGANDAI_ALPHA_V2-tom-D5]
"""

from __future__ import annotations

from ligandai.resources.ensemble import _attribute_from_history
from ligandai.types import CreditTransaction


def _tx(job_id, amount, type_="usage_gpu", **extra_meta):
    return CreditTransaction.model_validate(
        {"id": f"tx-{job_id}-{amount}", "amount": amount, "type": type_,
         "metadata": {"jobId": job_id, **extra_meta}}
    )


def test_settled_jobs_sum_exact_charge():
    history = [
        _tx("job-a", -23),
        _tx("job-b", -22),
        _tx("other-job", -99),          # not ours — must not be counted
        _tx("job-a", 0),                # reserve audit row — not a charge
    ]
    total, partial = _attribute_from_history(history, ["job-a", "job-b"])
    assert total == 45                  # Tom's exact 45-credit ensemble
    assert partial is False


def test_unsettled_job_flags_partial_and_is_not_extrapolated():
    history = [_tx("job-a", -23)]
    total, partial = _attribute_from_history(history, ["job-a", "job-b"])
    assert total == 23                  # ONLY what the ledger proved
    assert partial is True


def test_no_matching_rows_returns_none_not_zero():
    history = [_tx("zzz", -10)]
    total, partial = _attribute_from_history(history, ["job-a"])
    assert total is None                # attribution nil ≠ spend nil
    assert partial is True


def test_empty_job_list_returns_none():
    assert _attribute_from_history([], []) == (None, True)


def test_refund_and_topup_rows_never_count_as_spend():
    history = [
        _tx("job-a", -23),
        _tx("job-a", 5, type_="refund"),   # positive + refund type: excluded
        {"id": 1, "amount": 500, "type": "topup",
         "metadata": {"jobId": "job-a"}},  # a topup can't be a job charge
    ]
    total, partial = _attribute_from_history(history, ["job-a"])
    assert total == 23


def test_most_negative_row_wins_per_job():
    # If ledger bookkeeping ever lands both a release row and a final consume
    # row for one job, the consumed total is the largest-magnitude debit.
    history = [_tx("job-a", -5), _tx("job-a", -23)]
    total, _ = _attribute_from_history(history, ["job-a"])
    assert total == 23
