# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""The ensemble resource against the HOSTED co-fold route's real contract.

Three defects shipped because the SDK was written against a shape the server
does not have, and every test passed because the fakes had the same wrong shape:

  1. ``_pick_metric`` looked for a top-level ``peptide_ipsae`` on each engine
     record. The server nests every score under ``best`` / ``trajectories[]`` with
     the keys ``iPSAE`` / ``ipTM`` / ``pTM`` / ``pLDDT`` — so every per-engine
     score was None live.
  2. ``estimate()`` sent ``perEngineTrajectories`` / ``perEngineSettings``. The
     handler (server/cofold-routes.ts ``handleCofoldEstimate``) reads ONLY
     ``per_engine`` (and ``complexCount``), so a caller who asked for 8
     trajectories was quoted the default.
  3. ``COFACTORS_REACHABLE_VIA_HOSTED_COFOLD`` said the route could not carry a
     cofactor after server commit 2e9ed2d1a2 made it able to.

The engine record below is the SERVER's own fixture, copied from
fastapi_backend/tests/test_cofold_pae_persistence.py::_engine_record (the
"CANONICAL RETURN" documented in fastapi_backend/services/cofold_engine_adapters.py).
"""
from __future__ import annotations

import pytest

from ligandai.errors import LigandAIInvalidConfig
from ligandai.resources.ensemble import (
    CofactorSpec,
    Ensemble,
    _per_engine_body,
    _pick_metric,
)
from ligandai.pricing import CREDITS_PER_FOLD_DEFAULT


def server_engine_record(engine: str = "boltz2", *, cofactor: dict | None = None) -> dict:
    """An engine record exactly as ``POST /api/v1/cofold/start`` returns it."""
    pae = [[0.0, 3.0], [3.0, 0.0]]
    rec = {
        "engine": engine, "ok": True, "error": None, "wall_s": 1.0,
        "n_traj": 2, "n_traj_ok": 2,
        "trajectories": [
            {"label": "r0", "ok": True, "ipTM": 0.82, "pTM": 0.9, "pLDDT": 0.85,
             "iPSAE": 0.73, "iPAE": 5.0, "struct": "CIF0", "struct_fmt": "cif",
             "pae": pae},
            {"label": "r1", "ok": True, "ipTM": 0.61, "pTM": 0.8, "pLDDT": 0.80,
             "iPSAE": 0.55, "iPAE": 7.0, "struct": "CIF1", "struct_fmt": "cif",
             "pae": pae},
        ],
        "best": {"ipTM": 0.82, "ipTM_complex": 0.95, "pTM": 0.9, "pLDDT": 0.85,
                 "iPSAE": 0.73, "iPAE": 5.0, "struct": "PDBBEST", "struct_fmt": "pdb"},
        "settings": {},
    }
    if cofactor is not None:
        rec["cofactor"] = cofactor
    return rec


# --------------------------------------------------------------------------- #
# (1) metric extraction
# --------------------------------------------------------------------------- #
def test_peptide_ipsae_is_read_from_best_ipsae_on_the_real_record():
    assert _pick_metric(server_engine_record(), "peptide_ipsae") == 0.73


def test_other_metrics_map_to_their_own_server_keys():
    rec = server_engine_record()
    assert _pick_metric(rec, "iptm") == 0.82
    assert _pick_metric(rec, "ptm") == 0.9
    assert _pick_metric(rec, "plddt") == 0.85


def test_whole_complex_iptm_is_reachable_only_by_asking_for_it():
    """``ipTM_complex`` (0.95) is contaminated by receptor<->receptor contacts. It
    must never be served for ``iptm`` (0.82, peptide<->receptor)."""
    rec = server_engine_record()
    assert _pick_metric(rec, "iptm") == 0.82
    assert _pick_metric(rec, "iptm_complex") == 0.95


def test_whole_complex_ipsae_is_not_served_the_peptide_interface_value():
    """The hosted route reports ONE iPSAE — peptide<->receptor. Asking for the
    whole-complex ``ipsae`` gets None, not a different quantity."""
    assert _pick_metric(server_engine_record(), "ipsae") is None


def test_the_engines_best_is_used_not_the_max_over_trajectories():
    """Taking the max over trajectories would select on the reported metric."""
    rec = server_engine_record()
    rec["trajectories"][1]["iPSAE"] = 0.99
    assert _pick_metric(rec, "peptide_ipsae") == 0.73


def test_a_failed_engine_has_no_score_not_zero():
    failed = {"engine": "boltz2", "ok": False, "error": "OOM", "n_traj": 2,
              "n_traj_ok": 0, "trajectories": [], "best": None, "settings": {}}
    assert _pick_metric(failed, "peptide_ipsae") is None


def test_a_best_with_a_null_score_is_none_not_zero():
    rec = server_engine_record()
    rec["best"]["iPSAE"] = None
    assert _pick_metric(rec, "peptide_ipsae") is None


def test_flat_single_engine_spellings_still_work():
    assert _pick_metric({"peptideIpsae": 0.71}, "peptide_ipsae") == 0.71


# --------------------------------------------------------------------------- #
# (2) the estimate request body
# --------------------------------------------------------------------------- #
class _Capture:
    """Transport stub: records every request, answers like /cofold/estimate."""

    def __init__(self, per_complex=30):
        self.per_complex = per_complex
        self.calls = []

    def request(self, method, path, **kw):
        self.calls.append((method, path, kw))
        return {"estimatedTotal": self.per_complex, "subtotal": self.per_complex,
                "discountPercent": 0, "perEngine": [], "breakdown": "stub"}


def _estimate_body(t):
    (_, path, kw), = [c for c in t.calls if c[1] == "/api/v1/cofold/estimate"]
    return kw["json"]


def test_estimate_sends_the_key_the_server_reads():
    t = _Capture()
    Ensemble(t, client=None).estimate(
        ["boltz2", "protenix"], 3,
        per_engine={"boltz2": {"diffusion_samples": 8}})
    body = _estimate_body(t)
    assert body["per_engine"] == {"boltz2": {"diffusion_samples": 8}}
    assert "perEngineTrajectories" not in body and "perEngineSettings" not in body
    assert body["complexCount"] == 1          # one job's quote, scaled by n_peptides


def test_trajectory_shortcut_becomes_each_engines_own_knob():
    out = _per_engine_body(None, {"boltz2": 8, "protenix": 2, "opendde": 4,
                                  "esmfold2": 3, "openfold3": 2, "promera": 9}, None)
    assert out["boltz2"] == {"diffusion_samples": 8}
    assert out["protenix"] == {"sample": 2}
    assert out["opendde"] == {"sample": 4}
    assert out["esmfold2"] == {"seeds": [1, 2, 3]}      # counted as len(seeds)
    assert out["openfold3"] == {"seeds": [1, 2]}
    assert "promera" not in out                          # fixed at 1 server-side


def test_settings_merge_into_the_same_per_engine_map():
    out = _per_engine_body({"boltz2": {"recycling_steps": 3}}, {"boltz2": 5},
                           {"boltz2": {"sampling_steps": 100, "use_potentials": False}})
    assert out == {"boltz2": {"recycling_steps": 3, "diffusion_samples": 5,
                              "sampling_steps": 100, "use_potentials": False}}


def test_the_same_knob_named_twice_raises_instead_of_one_silently_winning():
    with pytest.raises(LigandAIInvalidConfig):
        _per_engine_body({"boltz2": {"diffusion_samples": 4}}, {"boltz2": 8}, None)


def test_unknown_engine_and_bad_counts_raise():
    with pytest.raises(LigandAIInvalidConfig):
        _per_engine_body(None, {"alphafold9": 4}, None)
    with pytest.raises(LigandAIInvalidConfig):
        _per_engine_body(None, {"boltz2": 0}, None)
    with pytest.raises(LigandAIInvalidConfig):
        _per_engine_body(None, {"boltz2": True}, None)


def test_no_overrides_means_no_per_engine_key():
    t = _Capture()
    Ensemble(t, client=None).estimate(["boltz2"], 1)
    assert "per_engine" not in _estimate_body(t)


# --------------------------------------------------------------------------- #
# the new price flows through the server quote, and scales with N x M
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("n_peptides,engines", [
    (1, ["boltz2"]),
    (25, ["boltz2", "esmfold2", "protenix"]),
    (7, ["boltz2", "esmfold2"]),
])
def test_ensemble_projection_is_peptides_x_engines_x_the_default_fold_price(n_peptides, engines):
    """Default trajectories, the platform quoting one default fold per engine at
    CREDITS_PER_FOLD_DEFAULT: N peptides x M engines x 10 credits ($0.10 each).
    The SDK holds no rate of its own — it multiplies the server's per-complex quote."""
    per_complex = len(engines) * CREDITS_PER_FOLD_DEFAULT
    est = Ensemble(_Capture(per_complex), client=None).estimate(engines, n_peptides)
    assert est.credits_total == n_peptides * len(engines) * CREDITS_PER_FOLD_DEFAULT
    assert est.usd_total == pytest.approx(est.credits_total / 100.0)


def test_the_estimate_follows_the_server_quote_when_the_platform_reprices():
    """A price change on the platform reaches the projection with no SDK release."""
    cheap = Ensemble(_Capture(30), client=None).estimate(["boltz2"], 10)
    dear = Ensemble(_Capture(300), client=None).estimate(["boltz2"], 10)
    assert (cheap.credits_total, dear.credits_total) == (300, 3000)


# --------------------------------------------------------------------------- #
# run(): the ceiling, the per_engine map and the cofactor all reach the wire
# --------------------------------------------------------------------------- #
class _Job:
    def __init__(self, result):
        self._result = result

    def result(self, **kw):
        return self._result


class _Peptides:
    def __init__(self, result_for):
        self.calls = []
        self._result_for = result_for

    def cofold(self, chains, engines, **kw):
        self.calls.append((chains, engines, kw))
        return _Job(self._result_for(engines))


class _Client:
    def __init__(self, result_for):
        self.peptides = _Peptides(result_for)


def _ensemble(result_for, per_complex=30):
    t = _Capture(per_complex)
    return Ensemble(t, client=_Client(result_for)), t


def _engines_result(engines, **cof):
    return {"engines": {e: server_engine_record(e, cofactor=cof.get(e)) for e in engines}}


def test_run_scores_come_back_from_the_real_record_shape():
    ens, _ = _ensemble(lambda e: _engines_result(e))
    res = ens.run("R" * 30, ["ACDEFGHIK", "MNPQRSTVW"], ["boltz2", "esmfold2"], bar=0.67)
    assert all(v == 0.73 for r in res.rows for v in r.per_engine.values())
    assert all(r.unanimous for r in res.rows)


def test_run_quotes_the_callers_per_engine_settings_before_checking_the_ceiling():
    """The ceiling check used to quote DEFAULTS while the run used the caller's
    settings, so it could pass a run that cost more than the ceiling."""
    ens, t = _ensemble(lambda e: _engines_result(e))
    ens.run("R" * 30, ["ACDEFGHIK"], ["boltz2"],
            per_engine={"boltz2": {"diffusion_samples": 8}})
    assert _estimate_body(t)["per_engine"] == {"boltz2": {"diffusion_samples": 8}}


def test_run_refuses_over_the_ceiling_and_submits_nothing():
    ens, t = _ensemble(lambda e: _engines_result(e), per_complex=30)
    with pytest.raises(LigandAIInvalidConfig, match="exceeds the ceiling"):
        ens.run("R" * 30, ["ACDEFGHIK"] * 4, ["boltz2"], max_credits=119)
    assert ens._client.peptides.calls == []
    # exactly at the ceiling is allowed: 4 peptides x 30
    ens.run("R" * 30, ["ACDEFGHIK"] * 4, ["boltz2"], max_credits=120)
    assert len(ens._client.peptides.calls) == 4


def test_run_sends_the_cofactor_to_the_hosted_route():
    ens, _ = _ensemble(lambda e: _engines_result(
        e, **{x: {"honoured": True} for x in e}))
    ens.run("R" * 30, ["ACDEFGHIK"], ["boltz2", "protenix"],
            cofactors=[CofactorSpec(ccd="ZN", copies=2)])
    _, engines, kw = ens._client.peptides.calls[0]
    assert kw["cofactors"] == [{"copies": 2, "ccd": "ZN"}]
    assert kw["allow_cofactor_absent"] is False


def test_an_arm_the_server_did_not_confirm_is_never_marked_honoured():
    """An older deployment ignores the unknown ``cofactors`` field and folds the
    complex WITHOUT it, returning no ``cofactor`` block. The SDK's expectation
    alone must not label that arm as carrying the cofactor."""
    ens, _ = _ensemble(lambda e: _engines_result(e))          # no cofactor blocks
    res = ens.run("R" * 30, ["ACDEFGHIK"], ["boltz2", "protenix"], cofactors=["ZN"])
    assert res.rows[0].cofactor_honoured == {"boltz2": False, "protenix": False}


def test_an_arm_the_server_confirmed_stays_honoured():
    ens, _ = _ensemble(lambda e: _engines_result(
        e, boltz2={"honoured": True}, protenix={"honoured": False}))
    res = ens.run("R" * 30, ["ACDEFGHIK"], ["boltz2", "protenix"], cofactors=["ZN"])
    assert res.rows[0].cofactor_honoured == {"boltz2": True, "protenix": False}


def test_no_cofactor_requested_sends_none_and_marks_nothing_honoured():
    ens, _ = _ensemble(lambda e: _engines_result(e))
    res = ens.run("R" * 30, ["ACDEFGHIK"], ["boltz2"])
    _, _, kw = ens._client.peptides.calls[0]
    assert kw["cofactors"] is None
    assert res.rows[0].cofactor_honoured == {"boltz2": False}


# --------------------------------------------------------------------------- #
# Peptides.cofold forwards the cofactor fields
# --------------------------------------------------------------------------- #
def test_cofold_body_carries_cofactors_and_the_opt_out(httpx_mock):
    from ligandai import LigandAI

    httpx_mock.add_response(
        method="POST", url="http://api.ligandai.test/api/v1/cofold/start",
        json={"job_id": "cf-1", "status": "queued", "engines": ["boltz2"]})
    c = LigandAI(api_key="lgai_pro_testkey0123456789ABCDEF",
                 base_url="http://api.ligandai.test", max_retries=1)
    try:
        c.peptides.cofold(
            [{"sequence": "R" * 20}, {"sequence": "ACDEFGHIK"}], ["boltz2", "opendde"],
            cofactors=["zn", {"ccd": "MG", "copies": 2}], allow_cofactor_absent=True)
    finally:
        c.close()
    import json
    body = json.loads(httpx_mock.get_requests()[0].content)
    assert body["cofactors"] == [{"ccd": "ZN"}, {"ccd": "MG", "copies": 2}]
    assert body["allow_cofactor_absent"] is True


def test_cofold_omits_cofactor_fields_when_none_requested(httpx_mock):
    from ligandai import LigandAI

    httpx_mock.add_response(
        method="POST", url="http://api.ligandai.test/api/v1/cofold/start",
        json={"job_id": "cf-2", "status": "queued", "engines": ["boltz2"]})
    c = LigandAI(api_key="lgai_pro_testkey0123456789ABCDEF",
                 base_url="http://api.ligandai.test", max_retries=1)
    try:
        c.peptides.cofold([{"sequence": "R" * 20}, {"sequence": "ACDEFGHIK"}], ["boltz2"])
    finally:
        c.close()
    import json
    body = json.loads(httpx_mock.get_requests()[0].content)
    assert "cofactors" not in body and "allow_cofactor_absent" not in body


def test_a_cofactor_with_neither_or_both_of_ccd_smiles_is_refused_before_the_wire():
    from ligandai.resources.peptides import _cofold_cofactors

    with pytest.raises(ValueError):
        _cofold_cofactors([{"copies": 2}])
    with pytest.raises(ValueError):
        _cofold_cofactors([{"ccd": "ZN", "smiles": "[Zn+2]"}])
    with pytest.raises(TypeError):
        _cofold_cofactors([42])


# --------------------------------------------------------------------------- #
# the async twin cannot drift
# --------------------------------------------------------------------------- #
class _AsyncCapture(_Capture):
    async def request(self, method, path, **kw):
        return super().request(method, path, **kw)


class _AsyncJob:
    def __init__(self, result):
        self._result = result

    async def result(self, **kw):
        return self._result


class _AsyncPeptides:
    def __init__(self, result_for):
        self.calls = []
        self._result_for = result_for

    async def cofold(self, chains, engines, **kw):
        self.calls.append((chains, engines, kw))
        return _AsyncJob(self._result_for(engines))


class _AsyncClient:
    def __init__(self, result_for):
        self.peptides = _AsyncPeptides(result_for)


@pytest.mark.asyncio
async def test_async_run_has_the_same_estimate_scores_and_cofactor_contract():
    from ligandai.resources.ensemble import AsyncEnsemble

    t = _AsyncCapture(30)
    ens = AsyncEnsemble(t, client=_AsyncClient(lambda e: _engines_result(e)))
    res = await ens.run("R" * 30, ["ACDEFGHIK", "MNPQRSTVW"], ["boltz2", "protenix"],
                        cofactors=["ZN"], per_engine={"boltz2": {"diffusion_samples": 6}})
    body = _estimate_body(t)
    assert body["per_engine"] == {"boltz2": {"diffusion_samples": 6}}
    assert "perEngineTrajectories" not in body
    assert all(v == 0.73 for r in res.rows for v in r.per_engine.values())
    assert all(r.cofactor_honoured == {"boltz2": False, "protenix": False} for r in res.rows)
    _, _, kw = ens._client.peptides.calls[0]
    assert kw["cofactors"] == [{"copies": 1, "ccd": "ZN"}]
    est = await ens.estimate(["boltz2", "esmfold2"], 5)
    assert est.credits_total == 5 * 30
