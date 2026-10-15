"""Transport and input-contract checks using recorded human/mouse fold queries.

No HTTP, billing, folding or scientific-score evidence is generated here.
"""
from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest

from ligandai.errors import LigandAIValidationError
from ligandai.resources.maturation import (
    AsyncMaturation, Maturation, MaturationSettings, MaturationSpec, MaturedDesign,
    Objective, ObjectiveMode, Receptor, Screening, Seed,
)

INPUTS = json.loads((Path(__file__).parents[1] / "fixtures" /
                    "maturation_joint_queries_20261011.json").read_text())
CID = "camp_" + "j" * 26


def receptor(species):
    query = INPUTS[species]["sequence"]
    return Receptor(gene="EGFR", receptor_sequence=query,
                    pocket_residues=INPUTS["pocket_residues"], target_id=f"egfr_{species}",
                    msa_csv=f"key,sequence\n-1,{query}\n")


def spec(**overrides):
    values = dict(seeds=[Seed(INPUTS["construct"], seed_id="gk1_e0476e735ca3_pc_human_s004",
                             linker_metadata=INPUTS["linker_metadata"])],
                  target=receptor("human"), positive_targets=[receptor("mouse")],
                  objective=Objective(target_kd_nM=10), objective_mode="joint_positive",
                  screening=Screening(seed=71, sampling_steps=100, recycling_steps=4))
    values.update(overrides)
    return MaturationSpec(**values)


class CaptureTransport:
    """Transport double records request bytes only; it is not a fold simulator."""
    def __init__(self):
        self.calls = []

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, copy.deepcopy(kwargs.get("json"))))
        if path.endswith("/estimate"):
            return {"estimate": {}, "quote": None}
        return {"campaign_id": CID, "status": "pending"}


def test_joint_sync_estimate_and_start_send_identical_exact_construct_query_protocol():
    transport = CaptureTransport()
    resource = Maturation(transport)
    request = spec()
    resource.estimate(request)
    resource.start(request)
    assert transport.calls[0][2] == transport.calls[1][2] == request.to_payload()
    body = transport.calls[1][2]
    assert body["target"]["gene"] == body["positive_targets"][0]["gene"] == "EGFR"
    assert body["target"]["target_id"] != body["positive_targets"][0]["target_id"]
    assert body["seeds"][0]["sequence"] == INPUTS["construct"]
    assert body["seeds"][0]["linker_metadata"]["novel_length"] == 45
    assert body["screening"]["seed"] == 71
    assert body["screening"]["sampling_steps"] == 100
    assert body["screening"]["recycling_steps"] == 4
    assert body["screening"]["fold_engine"] == "boltz2"


@pytest.mark.asyncio
async def test_joint_async_request_matches_sync_wire():
    class AsyncCapture(CaptureTransport):
        async def request(self, *args, **kwargs):
            return super().request(*args, **kwargs)
    transport = AsyncCapture()
    resource = AsyncMaturation(transport)
    request = spec()
    await resource.estimate(request)
    await resource.start(request)
    assert transport.calls[0][2] == transport.calls[1][2] == request.to_payload()


def test_legacy_default_payload_has_no_new_fields():
    request = MaturationSpec(seeds=[Seed(INPUTS["construct"])],
                            target=replace(receptor("human"), target_id=None, msa_csv=None),
                            objective=Objective(target_kd_nM=10))
    payload = request.to_payload()
    assert "positive_targets" not in payload
    assert "target_id" not in payload["target"]
    assert "msa_csv" not in payload["target"]
    assert "linker_metadata" not in payload["seeds"][0]
    assert not {"seed", "sampling_steps", "recycling_steps", "fold_engine"} & payload["screening"].keys()


def test_legacy_maturation_refuses_construct_metadata_it_does_not_consume():
    with pytest.raises(LigandAIValidationError, match="linker_metadata"):
        spec(positive_targets=[], objective_mode="affinity")


@pytest.mark.parametrize("overrides", [
    {"positive_targets": []},
    {"positive_targets": [receptor("human")]},
    {"positive_targets": [replace(receptor("mouse"), target_id=None)]},
    {"positive_targets": [replace(receptor("mouse"), target_id="egfr_human")]},
    {"screening": Screening()},
    {"screening": Screening(seed=71, diffusion_samples=1)},
    {"target": replace(receptor("human"), receptor_sequence=INPUTS["human"]["sequence"].lower(), msa_csv=None)},
    {"target": replace(receptor("human"), pocket_residues=[9999])},
    {"panel": [receptor("mouse")]},
    {"objective_mode": ObjectiveMode.AFFINITY},
    {"target": replace(receptor("human"), msa_csv=None, msa_path="/server/private.csv")},
])
def test_ambiguous_joint_inputs_fail_before_transport(overrides):
    with pytest.raises(LigandAIValidationError):
        spec(**overrides)


@pytest.mark.parametrize("csv_text", [
    "key,sequence\n", "/server/private.csv", "sequence,key\nQUERY,-1\n",
    f"key,sequence\n-1,{INPUTS['mouse']['sequence']}\n",
    f"key,sequence\n-1,{INPUTS['human']['sequence']}\n,\n",
    f"key,sequence\n-1,{INPUTS['human']['sequence']}\ninvalid,{INPUTS['human']['sequence']}\n",
    f"key,sequence\n-1,{INPUTS['human']['sequence']}\n9606,{INPUTS['human']['sequence'][:-1]}\n",
])
def test_msa_query_identity_required(csv_text):
    with pytest.raises(LigandAIValidationError):
        replace(receptor("human"), msa_csv=csv_text)


def test_msa_insertion_symbols_and_pair_keys_preserved():
    query = INPUTS["human"]["sequence"]
    payload = f'key,sequence\n-1,{query}\n9606,{query[:10]}a{query[10:]}\n'
    assert replace(receptor("human"), msa_csv=payload).to_wire()["msa_csv"] == payload


@pytest.mark.parametrize("kwargs", [
    {"seed": True}, {"seed": False}, {"seed": -1}, {"seed": 1.2},
    {"seed": 2**32}, {"seed": 2**64},
    {"sampling_steps": 0}, {"sampling_steps": 1001},
    {"recycling_steps": 0}, {"recycling_steps": 101},
    {"fold_engine": "unknown-engine"},
    {"fold_engine": "protenix_v2"},
    {"fold_engine": "protenix_v2", "sampling_steps": 200},
    {"fold_engine": "protenix_v2", "recycling_steps": 10},
])
def test_invalid_protocol_is_rejected(kwargs):
    with pytest.raises(LigandAIValidationError):
        Screening(**kwargs)


@pytest.mark.parametrize("seed", [0, 2**32 - 1])
@pytest.mark.parametrize("engine,steps,recycles", [("boltz2", 50, 3), ("protenix_v2", 200, 10)])
def test_uint32_seed_boundaries_survive_estimate_and_start(seed, engine, steps, recycles):
    transport = CaptureTransport()
    resource = Maturation(transport)
    request = spec(screening=Screening(seed=seed, fold_engine=engine,
                                      sampling_steps=steps, recycling_steps=recycles),
                   maturation=MaturationSettings(pocket_conditioning="none"))
    resource.estimate(request)
    resource.start(request)
    for _, _, payload in transport.calls:
        assert payload["screening"]["seed"] == seed
        assert type(payload["screening"]["seed"]) is int
        assert payload["screening"]["fold_engine"] == engine


def test_seed_overflow_reports_the_native_range_before_transport():
    transport = CaptureTransport()
    with pytest.raises(LigandAIValidationError, match=r"screening.seed.*\[0, 4294967295\]"):
        Maturation(transport).start(spec(screening=Screening(seed=2**32)))
    assert not transport.calls


def test_changed_metadata_or_mutable_targets_cannot_bypass_validation():
    request = spec()
    request.positive_targets.append(receptor("human"))
    with pytest.raises(LigandAIValidationError):
        request.to_payload()
    request = spec()
    request.seeds[0].linker_metadata["novel_length"] += 1
    with pytest.raises(LigandAIValidationError):
        request.to_payload()


def test_protenix_v2_keeps_native_protocol_and_refuses_unsupported_pocket_mode():
    settings = Screening(seed=71, sampling_steps=200, recycling_steps=10,
                         fold_engine="protenix_v2")
    with pytest.raises(LigandAIValidationError, match="pocket_conditioning"):
        spec(screening=settings)
    payload = spec(screening=settings, maturation=MaturationSettings(
        pocket_conditioning="none")).to_payload()
    assert payload["screening"]["fold_engine"] == "protenix_v2"
    assert payload["screening"]["sampling_steps"] == 200
    assert payload["screening"]["recycling_steps"] == 10


def test_joint_missing_scores_remain_unavailable_and_species_maps_survive():
    # Software contract envelope: deliberately absent scores, no invented result.
    data = {"design_id": "existing-input", "sequence": INPUTS["construct"],
            "positive_readouts": {key: {"target_id": key, "gene": "EGFR",
                "core_ipsae_median": None, "kd_nM": None, "status": "unavailable",
                "is_negative_result": False} for key in ("egfr_human", "egfr_mouse")},
            "joint": {"status": "unavailable", "worst_core_ipsae": None,
                      "structural_gate_pass": False}}
    result = MaturedDesign.from_wire(data)
    assert result.positive_readouts == data["positive_readouts"]
    assert result.joint == data["joint"]
    assert result.kd_nM is None
    assert not result.is_negative_result
