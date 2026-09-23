# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""[bd-LIGANDAI_ALPHA_V2-jb73b] A cached fold handle must say it is cached.

Measured 2026-09-23: six SEPARATE submissions of one payload returned
peptide_ipsae identical to eight decimal places, because each after the first
was served by the 24-hour dedupe. Anyone building "independent draws" out of
repeat calls therefore gets N copies of one measurement, and the only signal
lived inside ``receptor={'source': 'cached'}`` — nowhere a spread calculation
would look.
"""
from __future__ import annotations

import pytest

from ligandai.resources.peptides import BatchFoldJob


def mk(**over):
    kw = dict(batch_id="b1", jobs=[], total_cost_credits=0, peptide_count=1,
              trajectories_per_peptide=1, sampling_steps=50)
    kw.update(over)
    return BatchFoldJob(None, **kw)


def test_a_fresh_submission_is_not_cached():
    assert mk().from_cache is False


def test_the_explicit_flag_is_honoured():
    assert mk(from_cache=True).from_cache is True


def test_the_legacy_receptor_marker_is_still_recognised():
    # The pre-existing signal. A handle built by an older code path must not
    # read as fresh just because the new kwarg was not passed.
    assert mk(receptor={"source": "cached"}).from_cache is True


def test_a_normal_receptor_does_not_read_as_cached():
    assert mk(receptor={"source": "gene", "gene": "BRD4"}).from_cache is False


def test_a_missing_receptor_does_not_crash_the_check():
    assert mk(receptor=None).from_cache is False


def test_from_cache_is_a_bool_not_a_truthy_object():
    # A caller writing `if job.from_cache:` is fine either way, but one writing
    # `assert job.from_cache is False` must not be defeated by a dict.
    v = mk(receptor={"source": "gene"}).from_cache
    assert isinstance(v, bool)


@pytest.mark.parametrize("marker", ["cached", "CACHED"])
def test_marker_matching_is_exact_not_fuzzy(marker):
    # Only the exact lowercase marker the dedupe writes counts. Guessing at
    # case here would make an unrelated source string read as a cache hit.
    expected = marker == "cached"
    assert mk(receptor={"source": marker}).from_cache is expected


def test_the_property_is_documented_for_the_draws_case():
    doc = BatchFoldJob.from_cache.__doc__ or ""
    assert "independent draws" in doc
    assert "force_resubmit" in doc
