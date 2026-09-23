# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Panel depth + leg deepening — client.folds.deepen / client.folds.panel.

[bd-dre-ykrlo]

The power arithmetic here is not decoration. Selectivity is paralog Kd /
on-target Kd over poses clearing peptide_ipsae >= 0.67, and the SD of log10(Kd)
WITHIN one (design x isoform) cell is 0.646 against 0.569 BETWEEN designs — an
ICC of 0.437, i.e. most of the spread is pose noise. That fixes the pose targets
the platform quotes, so the targets ARE the test: if
``poses_needed_per_leg`` stops returning 15 / 36 / 104 the model behind every
"deepen this leg by N" recommendation has drifted.
"""

from __future__ import annotations

import math
import re

import pytest
from pytest_httpx import HTTPXMock

from ligandai import LigandAI
from ligandai.resources.folds import (
    SD_LOG10_KD_WITHIN_CELL,
    SELECTIVITY_GATE_PEPTIDE_IPSAE,
    poses_needed_per_leg,
    resolvable_fold_change,
)

BASE = "http://api.ligandai.test"


@pytest.fixture
def client() -> LigandAI:
    return LigandAI(api_key="lgai_pro_test123", base_url=BASE, max_retries=1)


# ── The measured constants and the arithmetic they fix ───────────────────────


def test_gate_is_the_binder_metric_gate_not_the_elite_threshold() -> None:
    # 0.67 is the selectivity gate ("is there an interface to take a Kd ratio
    # over"), deliberately distinct from the 0.66 elite threshold ("is this
    # design worth shipping"). Close in value, different in purpose.
    assert SELECTIVITY_GATE_PEPTIDE_IPSAE == 0.67
    assert SD_LOG10_KD_WITHIN_CELL == 0.646


@pytest.mark.parametrize(
    ("fold_change", "expected"),
    [(3.0, 15), (2.0, 36), (1.5, 104)],
)
def test_poses_needed_per_leg_reproduces_the_measured_targets(
    fold_change: float, expected: int
) -> None:
    assert poses_needed_per_leg(fold_change) == expected


def test_twenty_pose_comparison_resolves_only_about_3_7x() -> None:
    # "a 20-pose estimate carries an SE of ~3.7x" — 20 poses split across the
    # two legs of the ratio, i.e. 10 each.
    assert resolvable_fold_change(10, 10) == pytest.approx(3.68, abs=0.01)


def test_real_panel_depths_11_to_28_cannot_support_a_2x_claim() -> None:
    # Real panels here ran 11-28 poses per leg. Every one of those depths
    # resolves strictly worse than 2x, which is why deepen exists.
    for n in (11, 20, 28):
        rfc = resolvable_fold_change(n, n)
        assert rfc is not None and rfc > 2.0, (n, rfc)
    # And 36 per leg is the depth at which 2x becomes resolvable.
    assert resolvable_fold_change(36, 36) == pytest.approx(2.0, abs=0.02)


def test_resolvable_fold_change_is_none_not_infinity_on_an_empty_leg() -> None:
    # A leg with no gated poses resolves NOTHING. Returning a number here would
    # read as "resolves an infinite margin", which is the opposite of the truth.
    assert resolvable_fold_change(0, 30) is None
    assert resolvable_fold_change(30, 0) is None
    assert resolvable_fold_change(0, 0) is None


def test_poses_needed_per_leg_refuses_a_1x_margin() -> None:
    # There is no finite depth at which a 1x margin becomes resolvable, so this
    # must raise rather than return a huge number that looks actionable.
    with pytest.raises(ValueError):
        poses_needed_per_leg(1.0)
    with pytest.raises(ValueError):
        poses_needed_per_leg(0.5)


def test_the_two_helpers_are_inverses() -> None:
    for fold in (1.5, 2.0, 3.0, 5.0):
        n = poses_needed_per_leg(fold)
        rfc = resolvable_fold_change(n, n)
        assert rfc is not None
        # ceil() on the pose count can only make the achieved margin TIGHTER.
        assert rfc <= fold + 1e-9
        assert math.isclose(rfc, fold, rel_tol=0.05)


# ── deepen ───────────────────────────────────────────────────────────────────


def _deepen_response(**over: object) -> dict:
    base = {
        "jobId": "fold_1700000000001_newjob",
        "deepenOf": "fold_1699999999999_orig",
        "status": "submitted",
        "model": "boltz2",
        "seed": 1234567,
        "diffusionSamples": 40,
        "leg": {
            "gene": "HTR2A",
            "conformation": "32_monomer_A",
            "peptide_length": 24,
            "session_id": "session_HTR2A_1766653258382",
            "leg_key": "dev|HTR2A|PEPT|32_monomer_A",
        },
        "aggregation": {
            "sample_idx_from": 4,
            "sample_idx_to": 43,
            "poses_before": 4,
            "gated_before": 3,
            "gate": 0.67,
            "aggregates_with_existing": True,
        },
        "era": {
            "generatorModelId": "v6.5.21",
            "pocketFeatureDim": 48,
            "cfgGuidanceWeight": 0,
            "fingerprint": "x",
            "source": "provenance",
        },
        "estimatedCredits": 1000,
    }
    base.update(over)  # type: ignore[arg-type]
    return base


def test_deepen_posts_the_sample_count_and_returns_the_aggregation_window(
    httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/folding/jobs/fold_1699999999999_orig/deepen",
        method="POST",
        json=_deepen_response(),
    )
    out = client.folds.deepen(
        "fold_1699999999999_orig", diffusion_samples=40, wait=False,
    )

    req = httpx_mock.get_requests()[-1]
    import json as _json

    body = _json.loads(req.read())
    assert body["diffusion_samples"] == 40
    assert body["gate"] == 0.67
    assert "seed" not in body  # omitted -> platform derives a deterministic one

    # The NEW job id is what the caller polls; the original is only provenance.
    assert out["jobId"] != out["deepenOf"]
    # The window starts PAST the leg's existing poses. That is the whole point:
    # sample_idx is in the live uniqueness index, so overlapping it would either
    # collide or overwrite the poses being extended.
    agg = out["aggregation"]
    assert agg["sample_idx_from"] == agg["poses_before"]
    assert agg["sample_idx_to"] == agg["sample_idx_from"] + 40 - 1
    assert agg["aggregates_with_existing"] is True


def test_deepen_forwards_an_explicit_seed(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/folding/jobs/j1/deepen",
        method="POST",
        json=_deepen_response(seed=987),
    )
    client.folds.deepen("j1", diffusion_samples=8, seed=987, wait=False)
    import json as _json

    assert _json.loads(httpx_mock.get_requests()[-1].read())["seed"] == 987


@pytest.mark.parametrize("bad", [0, -1, 65, 1000])
def test_deepen_rejects_an_out_of_range_sample_count_before_spending(
    bad: int, client: LigandAI
) -> None:
    # Client-side so a typo never reaches the GPU billing path. The message
    # carries the real pose targets so the caller can size it correctly.
    with pytest.raises(ValueError, match=r"diffusion_samples must be in \[1, 64\]"):
        client.folds.deepen("j1", diffusion_samples=bad, wait=False)


def test_deepen_no_wait_does_not_poll(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/folding/jobs/j1/deepen", method="POST", json=_deepen_response(),
    )
    client.folds.deepen("j1", diffusion_samples=4, wait=False)
    assert len(httpx_mock.get_requests()) == 1


def test_deepen_waits_for_the_new_job_not_the_original(
    httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    httpx_mock.add_response(
        url=f"{BASE}/api/folding/jobs/orig/deepen",
        method="POST",
        json=_deepen_response(jobId="newjob", deepenOf="orig"),
    )
    httpx_mock.add_response(
        url=f"{BASE}/api/folding/jobs/newjob",
        method="GET",
        json={"status": "completed", "id": "newjob"},
    )
    out = client.folds.deepen("orig", diffusion_samples=4, wait=True, poll_interval=0.01)
    assert out["status"] == "completed"
    polled = [r for r in httpx_mock.get_requests() if r.method == "GET"]
    assert polled and polled[-1].url.path.endswith("/newjob")


def test_deepen_surfaces_a_platform_refusal(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    # The platform refuses a seed the leg already ran: at a fixed seed and spec
    # Boltz-2 redraws the same trajectories, so the pose count would rise while
    # the SE it is used to estimate would not move at all.
    from ligandai.errors import LigandAIError

    httpx_mock.add_response(
        url=f"{BASE}/api/folding/jobs/j1/deepen",
        method="POST",
        status_code=409,
        json={"error": "seed 42 has already been run on this leg", "seeds_used": [42]},
    )
    with pytest.raises(LigandAIError):
        client.folds.deepen("j1", diffusion_samples=4, seed=42, wait=False)


# ── panel ────────────────────────────────────────────────────────────────────


_PANEL = {
    "gate": 0.67,
    "on_target": "HTR2A",
    "on_target_source": "request",
    "on_target_note": None,
    "statistics": {
        "sd_log10_kd_within_cell": 0.646,
        "sd_log10_kd_between_designs": 0.569,
        "poses_needed_per_leg": {"3x": 15, "2x": 36, "1.5x": 104},
    },
    "legs": [
        {
            "design": "PEPTIDEONE",
            "gene": "HTR2A",
            "conformation": "32_monomer_A",
            "total_poses": 12,
            "gated_poses": 9,
            "unscored_poses": 1,
            "kd_poses": 9,
            "geomean_kd_gated": 4.2e6,
            "mean_plddt_gated": 80.6,
            "max_sample_idx": 11,
            "distinct_structures": 12,
            "session_ids": ["s1"],
            "job_ids": ["fold_a"],
        },
        {
            "design": "PEPTIDEONE",
            "gene": "HTR1A",
            "conformation": "monomer_A",
            "total_poses": 6,
            "gated_poses": 5,
            "unscored_poses": 0,
            "kd_poses": 5,
            "geomean_kd_gated": 3.1e7,
            "mean_plddt_gated": 73.1,
            "max_sample_idx": 5,
            "distinct_structures": 6,
            "session_ids": ["s1"],
            "job_ids": ["fold_b"],
        },
    ],
    "designs": [
        {
            "design": "PEPTIDEONE",
            "on_target_leg": {"gene": "HTR2A", "gated_poses": 9, "geomean_kd_gated": 4.2e6},
            "paralogs": [{"gene": "HTR1A", "gated_poses": 5, "selectivity": 7.38}],
            "deciding_paralog": {"gene": "HTR1A", "selectivity": 7.38, "gated_poses": 5},
            "depth": 5,
            "resolvable_fold_change": 4.05,
            "poses_to_add": {"on_target": 27, "deciding_paralog": 31, "target_fold_change": 2},
            "unresolved_reason": None,
            "shared_structure_warning": None,
        },
    ],
}


def test_panel_sends_the_scope_and_the_gate(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(method="GET", url=re.compile(rf"{re.escape(BASE)}/api/folding/panels.*"), json=_PANEL)
    out = client.folds.panel(session_id="s1", on_target="HTR2A")
    q = httpx_mock.get_requests()[-1].url.params
    assert q["session_id"] == "s1"
    assert q["on_target"] == "HTR2A"
    assert float(q["gate"]) == 0.67
    assert float(q["target_fold_change"]) == 2.0
    assert out["on_target_source"] == "request"


def test_panel_requires_a_scope(client: LigandAI) -> None:
    # An unscoped panel query would scan every fold the account owns.
    with pytest.raises(ValueError, match="session_id, peptide or gene"):
        client.folds.panel(on_target="HTR2A")


def test_panel_depth_is_the_min_of_the_two_deciding_legs(
    httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    httpx_mock.add_response(method="GET", url=re.compile(rf"{re.escape(BASE)}/api/folding/panels.*"), json=_PANEL)
    d = client.folds.panel(session_id="s1", on_target="HTR2A")["designs"][0]
    assert d["depth"] == min(
        d["on_target_leg"]["gated_poses"], d["deciding_paralog"]["gated_poses"]
    )
    # ...and the shortfall the platform reports is exactly what the local
    # arithmetic says, so a client can size a deepen without a round trip.
    need = poses_needed_per_leg(2.0)
    assert d["poses_to_add"]["on_target"] == need - d["on_target_leg"]["gated_poses"]
    assert d["poses_to_add"]["deciding_paralog"] == need - d["deciding_paralog"]["gated_poses"]


def test_panel_keeps_unscored_poses_out_of_the_gate_verdict(
    httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    # peptide_ipsae NULL means NOT COMPUTED, never a failed gate: gated +
    # unscored must not be assumed to account for every pose, and unscored is
    # reported separately so "not measured" never reads as "measured and lost".
    httpx_mock.add_response(method="GET", url=re.compile(rf"{re.escape(BASE)}/api/folding/panels.*"), json=_PANEL)
    leg = client.folds.panel(session_id="s1")["legs"][0]
    assert leg["unscored_poses"] == 1
    assert leg["gated_poses"] + leg["unscored_poses"] <= leg["total_poses"]


def test_panel_without_on_target_reports_why_depth_is_missing(
    httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    # The on-target gene is never inferred — naming the tightest-binding leg
    # "the on-target" would make every panel selective by construction.
    unresolved = {
        **_PANEL,
        "on_target": None,
        "on_target_source": "unresolved",
        "on_target_note": "Pass on_target=<GENE> for panel depth; per-leg depth is reported regardless.",
        "designs": [
            {
                "design": "PEPTIDEONE",
                "on_target_leg": None,
                "paralogs": [],
                "deciding_paralog": None,
                "depth": None,
                "resolvable_fold_change": None,
                "poses_to_add": None,
                "unresolved_reason": "no on_target gene supplied",
                "shared_structure_warning": None,
            }
        ],
    }
    httpx_mock.add_response(method="GET", url=re.compile(rf"{re.escape(BASE)}/api/folding/panels.*"), json=unresolved)
    out = client.folds.panel(session_id="s1")
    assert out["on_target_source"] == "unresolved"
    d = out["designs"][0]
    assert d["depth"] is None
    assert d["unresolved_reason"]  # never a silent null
    # Per-leg depth is still usable without an on-target.
    assert out["legs"][0]["gated_poses"] == 9


def test_panel_surfaces_a_leg_whose_poses_are_one_duplicated_structure(
    httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    # MEASURED on the live DB: an HTR1A row and an HTR2A row carrying
    # peptide_ipsae 0.74652106, predicted_kd 9125945 and the SAME
    # md5(pdb_content). One fold filed under two gene labels, so the ratio comes
    # out at exactly 1.000 — and deepening cannot fix it, because more
    # trajectories of the on-target fold are not a paralog fold.
    dup = {
        **_PANEL,
        "legs": [{**_PANEL["legs"][0], "total_poses": 12, "distinct_structures": 1}],
        "designs": [
            {
                **_PANEL["designs"][0],
                "deciding_paralog": {"gene": "HTR1A", "selectivity": 1.0, "gated_poses": 1},
                "depth": 1,
                "shared_structure_warning": "share 1 identical structure(s) by md5(pdb_content)",
            }
        ],
    }
    httpx_mock.add_response(method="GET", url=re.compile(rf"{re.escape(BASE)}/api/folding/panels.*"), json=dup)
    out = client.folds.panel(session_id="s1", on_target="HTR2A")
    leg = out["legs"][0]
    # 12 poses over 1 structure is ONE measurement recorded twelve times.
    assert leg["distinct_structures"] < leg["total_poses"]
    assert out["designs"][0]["shared_structure_warning"]
    assert out["designs"][0]["deciding_paralog"]["selectivity"] == 1.0


def test_async_mirror_exists_with_the_same_signature() -> None:
    import inspect

    from ligandai.resources.folds import AsyncFolds, Folds

    for name in ("deepen", "panel"):
        sync_sig = inspect.signature(getattr(Folds, name))
        async_sig = inspect.signature(getattr(AsyncFolds, name))
        assert sync_sig.parameters.keys() == async_sig.parameters.keys(), name
        assert inspect.iscoroutinefunction(getattr(AsyncFolds, name)), name
