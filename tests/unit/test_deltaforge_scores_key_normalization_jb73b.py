# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""``deltaforge_scores=`` must carry ``fold_job_id``. bd-LIGANDAI_ALPHA_V2-jb73b.

⛔ THE DEFECT, MEASURED ON PRODUCTION (2026-09-23, https://ligandai.com).

``client.deltaforge.batch_score_fold`` emits each result keyed ``foldJobId``
(camelCase). ``POST /api/v1/folds/filter-by-criteria`` builds its merge map with
``if (ds?.fold_job_id) map.set(String(ds.fold_job_id), ...)`` (snake_case) — the
same in the DEPLOYED blue bundle, not only in main. Nothing in the transport
normalises between them, so the merge key never matches.

The consequence is the worst available shape: passing ``dg["results"]``
verbatim — **exactly what the SDK docstring instructs** — is byte-identical in
its effect to passing nothing at all. Three live fold ids, three arms:

===  =====================================  ==========================
arm  ``deltaforge_scores``                   resulting ``metrics.kd_nm``
===  =====================================  ==========================
A    ``dg["results"]`` verbatim (documented) ``null, null, null``
B    omitted entirely                        ``null, null, null``
C    same list with ``fold_job_id`` added    ``86.197, 160.317, 87.632``
===  =====================================  ==========================

So the SDK's own documented path for obtaining thermodynamics returns a panel of
nulls and reports success. Kd then reads as "not measured" everywhere
downstream — which is why the selectivity ratio, whose definition is
``min(off-target Kd) / on-target Kd``, had no numerator and no denominator to
work with and every loop stayed ipSAE-only.

⛔ The fix belongs HERE, not on the server: the route is deployed to blue and
green and is owned elsewhere, the camelCase emitter is a second deployed
surface, and a client that normalises works against both. Normalising also
leaves a correct ``fold_job_id`` untouched, so it is safe on every input.
"""

from __future__ import annotations

import pytest

from ligandai.resources.evaluator import _empirical_body, _filter_body

IDS = ["fold_1790143533204_2l4tdfou1", "fold_1790143533204_abcdefgh2"]


def _batch_score_results() -> list[dict]:
    """``batch_score_fold`` results, in the key names production actually emits.

    Verbatim from a live 200 response: the id key is ``foldJobId`` and there is
    no ``fold_job_id`` anywhere on the row.
    """
    return [
        {
            "foldJobId": IDS[0],
            "kd_nm": 86.19726080415171,
            "delta_g": -9.637731939020915,
            "classification": "Strong binder",
            "scorer_version": "v10_2_unified_parallel_2026_05_08",
            "interface_type": "single_chain_pair",
            "affinity_quotable": True,
        },
        {
            "foldJobId": IDS[1],
            "kd_nm": 160.317,
            "delta_g": -9.270,
            "scorer_version": "v10_2_unified_parallel_2026_05_08",
            "affinity_quotable": True,
        },
    ]


# ─────────────────────────────────────────────────────────────────────────────
# filter_folds
# ─────────────────────────────────────────────────────────────────────────────


def test_filter_body_adds_the_snake_case_id_the_route_merges_on():
    body = _filter_body(IDS, {"max_kd_nm": 100.0}, _batch_score_results())
    sent = body["deltaforge_scores"]
    assert [d["fold_job_id"] for d in sent] == IDS, (
        "⛔ the route merges on fold_job_id; without it the Kd silently never "
        "lands and filter_folds returns kd_nm: null while reporting success"
    )


def test_the_camel_case_key_is_preserved_not_replaced():
    # The server ignores it, but discarding a field the caller handed us would
    # make this a lossy transform on data we do not own.
    sent = _filter_body(IDS, {}, _batch_score_results())["deltaforge_scores"]
    assert all(d["foldJobId"] == d["fold_job_id"] for d in sent)


def test_an_already_snake_case_score_is_untouched():
    rows = [{"fold_job_id": IDS[0], "kd_nm": 5.0, "delta_g": -11.0}]
    sent = _filter_body(IDS, {}, rows)["deltaforge_scores"]
    assert sent[0]["fold_job_id"] == IDS[0]
    assert sent[0]["kd_nm"] == 5.0


def test_a_conflicting_pair_keeps_the_snake_case_value_the_route_reads():
    # If both keys are present and disagree, the one the server actually reads
    # wins — silently rewriting it would change which fold a Kd is attributed to.
    rows = [{"fold_job_id": IDS[0], "foldJobId": IDS[1], "kd_nm": 5.0}]
    sent = _filter_body(IDS, {}, rows)["deltaforge_scores"]
    assert sent[0]["fold_job_id"] == IDS[0]


def test_a_score_row_with_no_id_at_all_is_passed_through_untouched():
    # Not our data to invent. It cannot merge, and fabricating an id would
    # attribute an affinity to an arbitrary fold.
    rows = [{"kd_nm": 5.0, "delta_g": -11.0}]
    sent = _filter_body(IDS, {}, rows)["deltaforge_scores"]
    assert "fold_job_id" not in sent[0]


def test_normalization_does_not_mutate_the_callers_list():
    rows = _batch_score_results()
    _filter_body(IDS, {}, rows)
    assert "fold_job_id" not in rows[0], (
        "the caller's own dg['results'] must not be edited under them — they may "
        "hold it for a second call or a record"
    )


def test_omitting_the_scores_still_omits_the_field():
    assert "deltaforge_scores" not in _filter_body(IDS, {}, None)


def test_non_mapping_entries_are_refused_rather_than_silently_dropped():
    with pytest.raises((TypeError, ValueError)):
        _filter_body(IDS, {}, ["not-a-dict"])  # type: ignore[list-item]


# ─────────────────────────────────────────────────────────────────────────────
# empirical_evaluate — same route-side key, same defect
# ─────────────────────────────────────────────────────────────────────────────


def test_empirical_body_normalizes_the_same_way():
    # sdk-fold-evaluator-routes.ts keys off ds.fold_job_id identically here, so
    # fixing only filter_folds would leave the report surface broken.
    sent = _empirical_body(
        IDS, {"min_peptide_ipsae": 0.67}, _batch_score_results()
    )["deltaforge_scores"]
    assert [d["fold_job_id"] for d in sent] == IDS
