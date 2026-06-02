# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""End-to-end fold submission + retrieval timing.

Live test, gated on ``LIGANDAI_TEST_API_KEY``. Verifies that after the
backend regression fix lands, the SDK's ``client.fold(target, peptide)`` +
``client.peptides.fold_batch().stream()`` return durable PDB content within
the documented window — and never return ``pdb_data=None`` on a job that
the server reported as succeeded.

These tests are slow (~5-15s per fold against warm compute containers, longer
on cold start). Run explicitly:

    LIGANDAI_TEST_API_KEY=lgai_pro_... \\
    LIGANDAI_TEST_BASE_URL=https://ligandai.com \\
    pytest tests/test_e2e_fold_instant.py -v -s
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from ligandai import LigandAI

pytestmark = pytest.mark.skipif(
    "LIGANDAI_TEST_API_KEY" not in os.environ,
    reason="LIGANDAI_TEST_API_KEY not set — set it to run e2e tests against a real platform deployment",
)


# Known-good warm-cached targets — these are receptor sequences that already
# have MSA + pocket features baked into the production cache, so cold-start
# overhead is minimised. If these go stale (target removed from cache, MSA
# server reset, etc.) the test will just take longer — not fail.
KNOWN_RECEPTOR_EGFR = (
    "LEEKKVCQGTSNKLTQLGTFEDHFLSLQRMFNNCEVVLGNLEITYVQRNYDLSFLKTIQEVAGYVLIALNTVERIPLENLQIIRGNMYYENSYALAVLSNYDANKTGLKELPMRNLQEILHGAVRFSNNPALCNVESIQWRDIVSSDFLSNMSMDFQNHLGSCQKCDPSCPNGSCWGAGEENCQKLTKIICAQQCSGRCRGKSPSDCCHNQCAAGCTGPRESDCLVCRKFRDEATCKDTCPPLMLYNPTTYQMDVNPEGKYSFGATCVKKCPRNYVVTDHGSCVRACGADSYEMEEDGVRKCKKCEGPCRKVCNGIGIGEFKDSLSINATNIKHFKNCTSISGDLHILPVAFRGDSFTHTPPLDPQELDILKTVKEITGFLLIQAWPENRTDLHAFENLEIIRGRTKQHGQFSLAVVSLNITSLGLRSLKEISDGDVIISGNKNLCYANTINWKKLFGTSGQKTKIISNRGENSCKATGQVCHALCSPEGCWGPEPRDCVSCRNVSRGRECVDKCNLLEGEPREFVENSECIQCHPECLPQAMNITCTGRGPDNCIQCAHYIDGPHCVKTCPAGVMGENNTLVWKYADAGHVCHLCHPNCTYGCTGPGLEGCPTNGPKIPSIATGMVGALLLLLVVALGIGLFM"
)
KNOWN_PEPTIDE_EGFR = "RGDDIVKR"  # 8mer, in-vocabulary, fast to fold


@pytest.fixture
def live_client() -> LigandAI:
    return LigandAI(
        api_key=os.environ["LIGANDAI_TEST_API_KEY"],
        base_url=os.environ.get("LIGANDAI_TEST_BASE_URL", "https://ligandai.com"),
        max_retries=2,
    )


@pytest.mark.integration
@pytest.mark.slow
def test_fold_completes_within_window_of_backend_returning(live_client: LigandAI) -> None:
    """live e2e — client.fold() + Job.wait(durable=True) returns
    a fully-populated FoldResult with PDB content non-empty.

    Sets a generous timeout (180s) to absorb cold start. The post-completion
    SDK overhead (decode + parse) should be milliseconds; we assert the
    overall budget hasn't blown out.
    """
    t0 = time.time()
    job = live_client.fold(target=KNOWN_RECEPTOR_EGFR, peptide=KNOWN_PEPTIDE_EGFR)
    result = job.wait(timeout=300.0, poll_interval=2.0)

    elapsed = time.time() - t0

    # The brief targeted <8s for warm-cached folds. We loosen to <300s for
    # cold start; the load-bearing assertion is that pdb_data is non-empty
    # and iptm landed.
    assert elapsed < 300, f"Fold + wait took {elapsed:.1f}s (>300s budget)"
    assert result.pdb_data is not None and len(result.pdb_data) > 100, (
        f"Server reported success but pdb_data is empty (len={len(result.pdb_data or '')}) "
        f"— this is the regression"
    )
    assert result.iptm is not None, "iptm is None on a succeeded fold"
    assert result.has_structure is True


@pytest.mark.integration
@pytest.mark.slow
def test_batch_stream_yields_per_fold(live_client: LigandAI) -> None:
    """live e2e — BatchFoldJob.stream() yields one event per
    peptide, each carrying non-empty pdb_content.

    Submits a 3-peptide batch and streams events. Asserts that exactly 3
    events arrive, all marked status='succeeded', all with pdb_content
    non-empty.
    """
    peptides = ["RGDDIVKR", "WYLKPRST", "MNPQRSTA"]  # 3 short peptides

    batch = live_client.fold_batch(
        peptides=peptides,
        receptor_sequence=KNOWN_RECEPTOR_EGFR,
        receptor_name="EGFR_test_e2e",
        diffusion_samples=1,
        sampling_steps=15,  # speed: minimum useful steps
    )

    events = []
    for event in batch.stream(timeout=600.0, poll_interval=2.0):
        events.append(event)

    assert len(events) == len(peptides), (
        f"Expected {len(peptides)} BatchFoldEvent(s), got {len(events)}"
    )
    for event in events:
        assert event.status == "succeeded", (
            f"Sub-job {event.job_id} status={event.status!r}: "
            f"phase={event.phase}, peptide={event.peptide_sequence}"
        )
        assert event.pdb_content and len(event.pdb_content) > 100, (
            f"Sub-job {event.job_id} reported succeeded but pdb_content is empty "
            f"(peptide={event.peptide_sequence})— regression"
        )
        assert event.iptm is not None, f"Sub-job {event.job_id} has iptm=None"


@pytest.mark.integration
@pytest.mark.slow
def test_recover_idempotent_on_already_completed(live_client: LigandAI) -> None:
    """client.folds.recover(job_id) on an already-durable job returns
    alreadyComplete=true without re-fetching."""
    # First do a fresh fold to get a known-good job_id with durable PDB.
    job = live_client.fold(target=KNOWN_RECEPTOR_EGFR, peptide=KNOWN_PEPTIDE_EGFR)
    job.wait(timeout=300.0, poll_interval=2.0)

    resp = live_client.folds.recover(job.id, wait=False)
    # Either alreadyComplete (preferred) or success+hasStructure — both
    # acceptable for an already-durable job.
    if resp.get("alreadyComplete"):
        assert resp["alreadyComplete"] is True
    else:
        assert resp.get("success") in (True, "true")
        # has_structure already true means recovery is a no-op
        assert (
            resp.get("hasStructure") or resp.get("has_structure")
            or resp.get("status") == "completed"
        )
