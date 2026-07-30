# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""FoldResult must surface the peptide-receptor interface iPSAE as its own field.

Regression cover for bd-LIGANDAI_ALPHA_V2-8gd2a.3 / bd-gpkuo.

THE RULE: ``peptide_ipsae`` (peptide<->receptor interface) is the binder metric.
The bare ``ipsae`` is complex-level and carries receptor-self confidence, so on a
multimeric target it stays high even when the peptide has detached. The two must
never be coalesced: a missing ``peptide_ipsae`` means "not computed", not zero,
and must NOT silently inherit the complex score.
"""

from __future__ import annotations

import pytest

from ligandai.resources.peptides import _parse_fold


def test_parse_fold_surfaces_peptide_ipsae() -> None:
    fold = _parse_fold(
        {
            "jobId": "fold_ok",
            "status": "completed",
            "ipsae": 0.93,          # complex-level — NOT the binder metric
            "peptideIpsae": 0.74,   # binder metric
            "peptideIpsaeStatus": "ok",
            "iptm": 0.82,
        }
    )
    assert fold.peptide_ipsae == pytest.approx(0.74)
    assert fold.peptide_ipsae_status == "ok"
    # complex-level score still surfaced for backward compatibility
    assert fold.ipsae == pytest.approx(0.93)


def test_parse_fold_accepts_snake_case_alias() -> None:
    """Server payloads vary between camelCase and snake_case; both must parse."""
    fold = _parse_fold(
        {"jobId": "fold_snake", "peptide_ipsae": 0.71, "peptide_ipsae_status": "ok"}
    )
    assert fold.peptide_ipsae == pytest.approx(0.71)
    assert fold.peptide_ipsae_status == "ok"


def test_parse_fold_never_coalesces_peptide_ipsae_to_overall() -> None:
    """⛔ The core anti-regression: a fold with a high complex ipsae but no
    computed peptide value must report None, never the complex score."""
    fold = _parse_fold({"jobId": "fold_batch_path", "ipsae": 0.93, "iptm": 0.88})
    assert fold.peptide_ipsae is None, (
        "peptide_ipsae must stay None when not computed — coalescing to the "
        "complex-level ipsae re-admits receptor-self confidence under a "
        "peptide-interface name"
    )
    assert fold.peptide_ipsae_status == "not_computed"
    assert fold.ipsae == pytest.approx(0.93)


def test_parse_fold_derives_status_on_legacy_payloads() -> None:
    """Payloads predating peptideIpsaeStatus still get a meaningful status."""
    ok = _parse_fold({"jobId": "legacy_ok", "peptideIpsae": 0.7})
    assert ok.peptide_ipsae_status == "ok"

    missing = _parse_fold({"jobId": "legacy_missing"})
    assert missing.peptide_ipsae_status == "not_computed"


def test_parse_fold_zero_peptide_ipsae_is_preserved_not_treated_as_missing() -> None:
    """A detached peptide scores a genuine 0.0 — that is a real measurement and
    must not be coerced to None or backfilled from the complex score."""
    fold = _parse_fold({"jobId": "fold_detached", "ipsae": 0.933, "peptideIpsae": 0.0})
    assert fold.peptide_ipsae == pytest.approx(0.0)
    assert fold.peptide_ipsae_status == "ok"
    assert fold.ipsae == pytest.approx(0.933)


def test_parse_fold_exposes_peptide_ipsae_in_metrics_dict() -> None:
    fold = _parse_fold({"jobId": "fold_metrics", "peptideIpsae": 0.74, "ipsae": 0.93})
    assert fold.metrics is not None
    assert fold.metrics["peptide_ipsae"] == pytest.approx(0.74)


def test_viewer_ipsae_key_order_prefers_peptide_interface() -> None:
    """The dashboard's score() helper is first-present-wins, so the peptide key
    must precede the complex-level keys or it can never be reached."""
    from ligandai import peptide_viewer

    src = peptide_viewer.__file__
    with open(src, encoding="utf-8") as fh:
        text = fh.read()

    assert "const IPSAE_KEYS = ['peptide_ipsae','ipsae','overall_ipsae'];" in text, (
        "viewer IPSAE key order regressed — peptide_ipsae must come first"
    )
    # the old inverted literal must not come back
    assert "['ipsae','overall_ipsae','peptide_ipsae']" not in text
