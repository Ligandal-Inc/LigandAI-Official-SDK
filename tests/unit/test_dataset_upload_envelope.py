# Copyright © 2025 Ligandal, Inc. All rights reserved.
"""`Dataset` must accept BOTH envelopes that produce one.

The upload envelope is the one that raised. Measured 2026-10-22 against the live
account: `POST /api/transcriptomics/upload` answered 202 with
`{"datasetId": 52, "status": "processing", "estimatedTime": "...",
  "fileName": "...", "datasetType": "other"}`
and `Dataset` required `id`, `name` and `type`, so `upload_dataset` raised
ValidationError AFTER the server had created the dataset and queued it. The
literals below are that measured response, not invented ones.
"""

from __future__ import annotations

import pytest

from ligandai.types import Dataset

# The exact 202 body the live route returned.
UPLOAD_202 = {
    "datasetId": 52,
    "status": "processing",
    "estimatedTime": "~2-4 min",
    "fileName": "bulk_normalized_counts_Treg_Teff_reps.csv",
    "datasetType": "other",
}

# The shape GET /api/transcriptomics/datasets returns.
LIST_ROW = {
    "id": "geo-job-7",
    "name": "GSE123456",
    "type": "bulk",
    "sampleCount": 12,
    "uploadedAt": "2026-10-01T00:00:00Z",
}


def test_the_upload_envelope_no_longer_raises_after_the_server_wrote():
    ds = Dataset.model_validate(UPLOAD_202)
    assert ds.id == 52
    assert ds.name == "bulk_normalized_counts_Treg_Teff_reps.csv"
    assert ds.type == "other"


def test_the_upload_envelope_keeps_the_202_fields_a_caller_needs_to_wait():
    """An upload is ACCEPTED, not finished. Dropping status/estimatedTime would
    leave the caller unable to tell a queued import from a complete one."""
    ds = Dataset.model_validate(UPLOAD_202)
    assert ds.status == "processing"
    assert ds.estimated_time == "~2-4 min"


def test_the_list_envelope_is_unchanged():
    ds = Dataset.model_validate(LIST_ROW)
    assert (ds.id, ds.name, ds.type) == ("geo-job-7", "GSE123456", "bulk")
    assert ds.sample_count == 12
    assert ds.status is None and ds.estimated_time is None


def test_an_id_is_still_required_so_an_unusable_envelope_still_fails():
    """Tolerating missing name/type must not tolerate a dataset with no identity —
    that would hand the caller an object it cannot pass to any other method."""
    with pytest.raises(Exception):
        Dataset.model_validate({"status": "processing", "fileName": "x.csv"})


@pytest.mark.parametrize("key", ["datasetId", "dataset_id", "id"])
def test_every_spelling_of_the_identity_key_is_accepted(key):
    assert Dataset.model_validate({key: 7}).id == 7


def test_an_explicit_field_wins_over_its_envelope_alias():
    """If a server ever sends both, the documented field is the authority — the
    alias fills a GAP, it never overwrites a value that was sent."""
    ds = Dataset.model_validate({"id": 1, "datasetId": 99, "name": "real", "fileName": "other"})
    assert ds.id == 1 and ds.name == "real"


def test_a_non_dict_payload_is_passed_through_untouched():
    """The before-validator must not turn a wrong-type payload into a confusing
    AttributeError instead of pydantic's own message."""
    with pytest.raises(Exception):
        Dataset.model_validate(["not", "a", "dict"])
