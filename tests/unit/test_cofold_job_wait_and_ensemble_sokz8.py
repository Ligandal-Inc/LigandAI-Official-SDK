# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Hosted co-fold ``Job.wait`` and ``Ensemble.run`` against REAL response shapes.

Found by the 2026-10-02 ensemble smoke (four EGFR runs, boltz2 + protenix, against
the hosted co-fold route). Every fixture here is read from
``tests/fixtures/cofold_smoke_20261002.json``, which holds those runs' status
payloads with structures, PAE matrices and sequences removed and every score,
status string and key verbatim.

What it pins, and why each one shipped:

* ``Ensemble.run`` called ``job.result(...)``. ``Job`` has no such method, so every
  peptide was recorded as a failure AFTER its job was submitted and billed. The
  stub jobs in the older tests exposed ``.result()``, so they agreed with the bug.
  These tests drive the real ``Job`` / ``AsyncJob``.
* The co-fold store's success state is ``done``. It was not a terminal status, so
  ``wait()`` read a finished job as still running until ``timeout``.
* The job store is per-process behind several API workers, so a status poll can
  404 "job not found" for a job that is running. ``wait()`` aborted on the first
  one. The smoke saw 0, 7, 12 and 60 of them in four runs; the longest unbroken
  streak was 54 polls over 530 s and the job then completed.
* A best structure with ipTM 0 / iPSAE 0 / iPAE 10 is the scorer's "no contact
  found" default, not a measured zero, and was returned as ``0.0``.
"""
from __future__ import annotations

import json
import pathlib
from types import SimpleNamespace
from typing import Any

import pytest

from ligandai.errors import (
    LigandAIJobError,
    LigandAINotFoundError,
    LigandAIServerError,
    LigandAITimeoutError,
)
from ligandai.jobs import AsyncJob, Job
from ligandai.resources.ensemble import (
    AsyncEnsemble,
    Ensemble,
    _no_interface_score,
    _pick_metric,
)
from ligandai.resources.peptides import _parse_cofold

FIXTURE = pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "cofold_smoke_20261002.json"
RUNS: dict[str, dict[str, Any]] = json.loads(FIXTURE.read_text())["runs"]

RECEPTOR = "M" * 40                    # synthetic: the tests never fold anything
PEPTIDE = "ACDEFGHIKLMNPQ"             # synthetic, not a campaign sequence
STATUS_PATH = "/api/v1/cofold/{job_id}/status"


# --------------------------------------------------------------------------- #
# scaffolding
# --------------------------------------------------------------------------- #
def _miss() -> LigandAINotFoundError:
    """What the transport raises for the hosted route's 404 (smoke raw_*.json)."""
    return LigandAINotFoundError('{"detail":"job not found"}', status_code=404)


class _Scripted:
    """Transport that answers status polls from a script; raises exceptions."""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls = 0

    def request(self, method: str, path: str, **kw: Any) -> Any:
        self.calls += 1
        if not self.script:
            raise AssertionError("poll script exhausted: wait() kept polling past the end")
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class _AsyncScripted(_Scripted):
    async def request(self, method: str, path: str, **kw: Any) -> Any:  # type: ignore[override]
        return super().request(method, path, **kw)


class _Clock:
    """Stands in for the ``time`` module inside ``ligandai.jobs`` so a 530 s
    streak costs no wall time. ``sleep`` advances ``monotonic``."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept = 0.0

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.now += s
        self.slept += s


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    c = _Clock()
    monkeypatch.setattr("ligandai.jobs.time", c)
    return c


def _job(transport: Any, job_id: str = "cofold_test", cls: type = Job) -> Any:
    return cls(
        transport, job_id, job_type="cofold", parser=_parse_cofold, status_path=STATUS_PATH,
        initial={"id": job_id, "type": "cofold", "status": "pending"},
    )


def _script_for(run: dict[str, Any]) -> list[Any]:
    """The poll sequence the smoke observed: running, then each measured 404
    streak separated by a successful poll, then the terminal payload."""
    seq: list[Any] = [run["running_payload"]]
    for streak in run["miss_streaks"]:
        seq += [_miss() for _ in range(streak["count"])]
        seq.append(run["running_payload"])
    seq.append(run["done_payload"])
    return seq


# --------------------------------------------------------------------------- #
# (1) ``done`` is terminal; wait() returns the multi-engine result
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("tag", sorted(RUNS))
def test_a_done_cofold_job_is_terminal_and_wait_returns_the_engine_record(tag: str, clock: _Clock):
    run = RUNS[tag]
    job = _job(_Scripted([run["done_payload"]]))
    res = job.wait(timeout=30, poll_interval=1)
    assert job.succeeded and job.is_terminal
    assert set(res["engines"]) == {"boltz2", "protenix"}
    # one poll: done must not be read as "still running" and polled until timeout
    assert job.status == "done"
    assert clock.slept == 0


def test_a_cofold_error_still_raises():
    payload = dict(RUNS["A_human"]["done_payload"], status="error", error="engine pool exhausted")
    payload.pop("result")
    job = _job(_Scripted([payload]))
    with pytest.raises(LigandAIJobError, match="engine pool exhausted"):
        job.wait(timeout=30, poll_interval=0)


# --------------------------------------------------------------------------- #
# (2) transient 404 "job not found"
# --------------------------------------------------------------------------- #
def test_the_smoke_runs_hit_the_streaks_these_tests_replay():
    """Guard the fixture: the replayed streaks are the measured ones."""
    counts = {t: sum(s["count"] for s in r["miss_streaks"]) for t, r in RUNS.items()}
    assert counts == {"A_human": 0, "A_mouse": 7, "B_human": 60, "B_mouse": 12}
    assert max(s["count"] for r in RUNS.values() for s in r["miss_streaks"]) == 54


@pytest.mark.parametrize("tag", sorted(RUNS))
def test_wait_rides_out_the_404_streaks_the_smoke_measured(tag: str, clock: _Clock):
    run = RUNS[tag]
    t = _Scripted(_script_for(run))
    res = _job(t, run["job_id"]).wait(timeout=1800, poll_interval=10)
    assert set(res["engines"]) == {"boltz2", "protenix"}
    assert t.script == []                     # consumed the whole run, ended on `done`


def test_the_default_window_clears_the_54_poll_530_second_streak(clock: _Clock):
    """120 s (the first number anyone reaches for) would have failed this run."""
    run = RUNS["B_human"]
    # the longest streak, polled at the smoke's 10 s interval, spans 530 s
    assert _job(_Scripted(_script_for(run))).wait(timeout=1800, poll_interval=10)

    with pytest.raises(LigandAINotFoundError) as ei:
        _job(_Scripted(_script_for(run)), run["job_id"]).wait(
            timeout=1800, poll_interval=10, not_found_grace=120)
    assert ei.value.status_code == 404


def test_a_streak_longer_than_the_window_fails_with_a_clear_message(clock: _Clock):
    t = _Scripted([RUNS["A_human"]["running_payload"]] + [_miss() for _ in range(100)])
    with pytest.raises(LigandAINotFoundError) as ei:
        _job(t, "cofold_gone").wait(timeout=1800, poll_interval=10, not_found_grace=60)
    msg = str(ei.value)
    assert "cofold_gone" in msg
    assert "404" in msg and "60s" in msg
    assert "7 time(s) in a row" in msg            # misses at t=0,10,..,60
    assert "not_found_grace" in msg                # says how to wait longer
    assert "job not found" in msg                  # keeps the server's own words
    assert ei.value.status_code == 404
    assert isinstance(ei.value.__cause__, LigandAINotFoundError)
    assert t.calls == 8                            # 1 running + 7 misses, then it stopped


def test_not_found_grace_zero_restores_fail_on_first_404(clock: _Clock):
    t = _Scripted([_miss(), RUNS["A_human"]["done_payload"]])
    with pytest.raises(LigandAINotFoundError):
        _job(t).wait(timeout=60, poll_interval=1, not_found_grace=0)
    assert t.calls == 1


def test_a_successful_poll_resets_the_streak(clock: _Clock):
    """Nine misses in total, but never more than three in a row (20 s) against a
    30 s window: it must complete."""
    running = RUNS["A_human"]["running_payload"]
    seq: list[Any] = []
    for _ in range(3):
        seq += [_miss(), _miss(), _miss(), running]
    seq.append(RUNS["A_human"]["done_payload"])
    res = _job(_Scripted(seq)).wait(timeout=600, poll_interval=10, not_found_grace=30)
    assert "engines" in res


def test_the_overall_timeout_still_bounds_a_404_streak(clock: _Clock):
    t = _Scripted([_miss() for _ in range(500)])
    with pytest.raises(LigandAITimeoutError, match="did not complete within 100"):
        _job(t).wait(timeout=100, poll_interval=10, not_found_grace=10_000)


def test_only_404_is_absorbed(clock: _Clock):
    t = _Scripted([LigandAIServerError("boom", status_code=500)])
    with pytest.raises(LigandAIServerError):
        _job(t).wait(timeout=60, poll_interval=1)
    assert t.calls == 1


def test_refresh_itself_stays_strict():
    """Only wait() has a grace window; a direct poll reports the 404 as it is."""
    with pytest.raises(LigandAINotFoundError):
        _job(_Scripted([_miss()])).refresh()


@pytest.mark.asyncio
async def test_async_wait_has_the_same_grace_and_done_semantics():
    run = RUNS["A_mouse"]
    job = _job(_AsyncScripted([_miss(), _miss(), run["running_payload"], _miss(), run["done_payload"]]),
               run["job_id"], AsyncJob)
    res = await job.wait(timeout=30, poll_interval=0.001, not_found_grace=5)
    assert set(res["engines"]) == {"boltz2", "protenix"}


@pytest.mark.asyncio
async def test_async_wait_gives_up_after_the_window():
    job = _job(_AsyncScripted([_miss() for _ in range(10_000)]), "cofold_gone", AsyncJob)
    with pytest.raises(LigandAINotFoundError, match="cofold_gone"):
        await job.wait(timeout=30, poll_interval=0.005, not_found_grace=0.05)


# --------------------------------------------------------------------------- #
# (3) a "no interface" structure is not a measured 0.0
# --------------------------------------------------------------------------- #
def _engine(tag: str, engine: str) -> dict[str, Any]:
    return RUNS[tag]["done_payload"]["result"]["engines"][engine]


def test_the_smoke_fixture_contains_both_the_sentinel_and_a_low_real_interface():
    """Fixture guard: the distinction under test exists in the real data."""
    for tag in ("A_human", "B_human"):
        best = _engine(tag, "protenix")["best"]
        assert (best["ipTM"], best["iPSAE"], best["iPAE"]) == (0, 0, 10)
    for tag, ipsae in (("A_mouse", 0.013623181687760466), ("B_mouse", 0.018015818264333426)):
        best = _engine(tag, "protenix")["best"]
        assert best["iPSAE"] == ipsae and 0 < best["iPSAE"] < 0.05 and best["iPAE"] < 10


@pytest.mark.parametrize("tag", ["A_human", "B_human"])
def test_no_interface_structure_is_none_not_a_measurement(tag: str):
    rec = _engine(tag, "protenix")
    assert _pick_metric(rec, "peptide_ipsae") is None
    assert _pick_metric(rec, "iptm") is None
    assert _no_interface_score(rec, "peptide_ipsae") is True


@pytest.mark.parametrize("tag", ["A_human", "B_human"])
def test_whole_complex_metrics_survive_a_no_interface_structure(tag: str):
    """The sentinel says the INTERFACE is absent; pTM, pLDDT and the engine-native
    complex ipTM describe the whole complex and are still measurements."""
    rec = _engine(tag, "protenix")
    assert _pick_metric(rec, "ptm") == rec["best"]["pTM"]
    assert _pick_metric(rec, "plddt") == rec["best"]["pLDDT"]
    assert _pick_metric(rec, "iptm_complex") == rec["best"]["ipTM_complex"] > 0
    assert _no_interface_score(rec, "ptm") is False


@pytest.mark.parametrize("tag", ["A_mouse", "B_mouse"])
def test_a_low_but_real_interface_is_still_a_measurement(tag: str):
    rec = _engine(tag, "protenix")
    v = _pick_metric(rec, "peptide_ipsae")
    assert v == rec["best"]["iPSAE"] and 0 < v < 0.05
    assert _no_interface_score(rec, "peptide_ipsae") is False


@pytest.mark.parametrize("tag,expected", [
    ("A_human", 0.6914105743424245), ("A_mouse", 0.2553485311167886),
    ("B_human", 0.6067796764868916), ("B_mouse", 0.5624645389682196),
])
def test_boltz2_best_ipsae_is_read_unchanged(tag: str, expected: float):
    assert _pick_metric(_engine(tag, "boltz2"), "peptide_ipsae") == expected


def test_a_bare_zero_with_no_ipae_beside_it_is_kept():
    """No evidence it is the no-contact default, so it stays the claim it makes."""
    assert _pick_metric({"best": {"iPSAE": 0.0}}, "peptide_ipsae") == 0.0
    assert _pick_metric({"best": {"iPSAE": 0.0, "iPAE": 6.0}}, "peptide_ipsae") == 0.0
    assert _pick_metric({"peptide_ipsae": 0.0}, "peptide_ipsae") == 0.0


# --------------------------------------------------------------------------- #
# (4) Ensemble.run drives the real Job API end to end
# --------------------------------------------------------------------------- #
class _EstimateTransport:
    def request(self, method: str, path: str, **kw: Any) -> Any:
        return {"estimatedTotal": 11, "subtotal": 12, "discountPercent": 5,
                "perEngine": [], "breakdown": "stub"}


class _AsyncEstimateTransport(_EstimateTransport):
    async def request(self, method: str, path: str, **kw: Any) -> Any:  # type: ignore[override]
        return super().request(method, path, **kw)


class _Peptides:
    """``client.peptides`` whose ``cofold`` returns a real Job over a scripted
    status transport — the only fake is the network."""

    def __init__(self, scripts: list[list[Any]], job_cls: type = Job) -> None:
        self._scripts = list(scripts)
        self._cls = job_cls
        self.jobs: list[Any] = []

    def _next(self) -> Any:
        i = len(self.jobs)
        transport_cls = _AsyncScripted if self._cls is AsyncJob else _Scripted
        job = _job(transport_cls(self._scripts[i]), f"cofold_{i}", self._cls)
        self.jobs.append(job)
        return job

    def cofold(self, chains, engines, **kw):
        return self._next()


class _AsyncPeptides(_Peptides):
    async def cofold(self, chains, engines, **kw):  # type: ignore[override]
        return self._next()


def _ensemble(scripts: list[list[Any]]) -> Ensemble:
    return Ensemble(_EstimateTransport(), client=SimpleNamespace(peptides=_Peptides(scripts)))


def test_run_collects_through_the_real_job_api_and_records_no_failure():
    run = RUNS["A_human"]
    ens = _ensemble([[_miss(), _miss(), run["done_payload"]]])
    res = ens.run(RECEPTOR, [PEPTIDE], ["boltz2", "protenix"], bar=0.67, poll_interval=0)
    assert res.failures == []
    (row,) = res.rows
    assert row.per_engine == {"boltz2": 0.6914105743424245, "protenix": None}
    assert row.n_engines_scored == 1
    assert row.no_interface == ["protenix"]
    assert row.engines_above_bar == ["boltz2"]
    # protenix folded and found no contact: it cannot back the peptide, so the
    # one engine that scored must not make this look unanimous
    assert row.unanimous is False and row.contested is True


def test_run_keeps_a_low_real_interface_as_a_number_not_a_gap():
    run = RUNS["B_mouse"]
    ens = _ensemble([[run["done_payload"]]])
    res = ens.run(RECEPTOR, [PEPTIDE], ["boltz2", "protenix"], bar=0.5, poll_interval=0)
    (row,) = res.rows
    assert row.per_engine == {"boltz2": 0.5624645389682196, "protenix": 0.018015818264333426}
    assert row.no_interface == []
    assert row.n_engines_scored == 2 and row.contested is True and row.unanimous is False


def test_a_submitted_job_that_fails_is_a_failure_that_names_its_job_id():
    """The job was submitted (and billed); the caller needs its id to collect it."""
    boom = LigandAIServerError("upstream broke", status_code=500)
    ens = _ensemble([[boom]])
    res = ens.run(RECEPTOR, [PEPTIDE], ["boltz2", "protenix"], poll_interval=0)
    (f,) = res.failures
    assert f["sequence"] == PEPTIDE and f["stage"] == "cofold"
    assert f["job_id"] == "cofold_0"
    assert "LigandAIServerError" in f["error"]
    assert "has no attribute" not in f["error"]       # the original defect's signature


def test_run_without_a_submitted_job_has_no_job_id_in_the_failure():
    class _Boom:
        def cofold(self, *a, **k):
            raise LigandAIServerError("submit refused", status_code=402)

    ens = Ensemble(_EstimateTransport(), client=SimpleNamespace(peptides=_Boom()))
    res = ens.run(RECEPTOR, [PEPTIDE], ["boltz2"], poll_interval=0)
    (f,) = res.failures
    assert "job_id" not in f and "submit refused" in f["error"]


def test_result_and_dict_carry_the_no_interface_flag():
    run = RUNS["B_human"]
    ens = _ensemble([[run["done_payload"]]])
    res = ens.run(RECEPTOR, [PEPTIDE], ["boltz2", "protenix"], bar=0.5, poll_interval=0)
    (design,) = res.to_dict()["designs"]
    assert design["no_interface"] == ["protenix"]
    assert design["per_engine"]["protenix"] is None


def test_rank_agreement_never_counts_a_no_interface_engine_as_a_value():
    per_peptide = {
        "P1": {"boltz2": 0.9, "protenix": None},
        "P2": {"boltz2": 0.8, "protenix": None},
        "P3": {"boltz2": 0.4, "protenix": 0.05},
        "P4": {"boltz2": 0.3, "protenix": 0.02},
        "P5": {"boltz2": 0.2, "protenix": 0.01},
    }
    res = Ensemble._build_result(
        0.67, ["boltz2", "protenix"], "peptide_ipsae", per_peptide,
        {"boltz2": False, "protenix": False}, [], [], None, 1.0, None,
        no_interface={"P1": ["protenix"], "P2": ["protenix"]},
    )
    (pair,) = res.rank_agreement()
    assert pair["n"] == 3                  # P3-P5 only; had P1/P2 been 0.0, n would be 5
    rows = {r.sequence: r for r in res.rows}
    assert rows["P1"].no_interface == ["protenix"] and rows["P1"].contested
    assert rows["P3"].no_interface == []


@pytest.mark.asyncio
async def test_async_run_uses_the_real_async_job_api():
    run = RUNS["A_human"]
    peptides = _AsyncPeptides([[_miss(), run["done_payload"]]], AsyncJob)
    ens = AsyncEnsemble(_AsyncEstimateTransport(), client=SimpleNamespace(peptides=peptides))
    res = await ens.run(RECEPTOR, [PEPTIDE], ["boltz2", "protenix"], bar=0.67, poll_interval=0.001)
    assert res.failures == []
    (row,) = res.rows
    assert row.per_engine["boltz2"] == 0.6914105743424245 and row.per_engine["protenix"] is None
    assert row.no_interface == ["protenix"] and row.contested is True
