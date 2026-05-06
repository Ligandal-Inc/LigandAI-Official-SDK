# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Unit tests for the extended generate() flags added in v0.3.2+.

Covers wire-format correctness for:
  - segment_config (custom scaffold)
  - pdc_config (peptide-drug conjugate)
  - ec_trimming_config (EC domain trimming)
  - sampling_steps / glycosylation_enabled
  - quality_guided / immunogenicity / serum_stability guidance flags
  - charge_mode / min_solubility filters
  - tier-gate error propagation (403 → LigandAITierError)

All tests use pytest-httpx to intercept the POST to /api/ptf/parallel/generate
and inspect the request body without touching a real server.
"""

from __future__ import annotations

import json

import pytest
from pytest_httpx import HTTPXMock

from ligandai import LigandAI
from ligandai.errors import LigandAITierError
from ligandai.types import EcTrimmingConfig, PdcConfig, PeptideSegment, ResidueRange, SegmentConfig

BASE = "http://api.ligandai.test"
GEN_URL = f"{BASE}/api/ptf/parallel/generate"

_QUEUED = {"sessionId": "sid_test", "status": "queued"}


@pytest.fixture
def client() -> LigandAI:
    return LigandAI(api_key="lgai_pro_test", base_url=BASE, max_retries=1)


def _body(httpx_mock: HTTPXMock) -> dict:
    req = httpx_mock.get_request()
    assert req is not None
    return json.loads(req.content)


def test_pocket_targeted_multi_chain_ranges_in_wire_body(
    httpx_mock: HTTPXMock,
    client: LigandAI,
) -> None:
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    target_residues = [
        *ResidueRange.from_residues([34, 35, 36, 41, 42], chain="A", label="pocket A"),
        *ResidueRange.from_residues([102, 103, 104], chain="B", label="pocket B"),
    ]

    client.peptides.generate(
        gene="EGFR",
        target_residues=target_residues,
        targeting_strategy="pocket_targeted",
        quality_guided=True,
    )

    body = _body(httpx_mock)
    target = body["targets"][0]
    assert target["gene"] == "EGFR"
    assert target["targetingStrategy"] == "pocket_targeted"
    assert target["targetResidues"] == [
        {"chain": "A", "start": 34, "end": 36, "label": "pocket A"},
        {"chain": "A", "start": 41, "end": 42, "label": "pocket A"},
        {"chain": "B", "start": 102, "end": 104, "label": "pocket B"},
    ]


# ---------------------------------------------------------------------------
# Segment config
# ---------------------------------------------------------------------------


def test_segment_config_pydantic_serialized(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    sc = SegmentConfig(
        mode="custom",
        segments=[
            PeptideSegment(id="s1", type="premade", position=0, sequence="GRKKRRQRRRPPQ", label="TAT CPP"),
            PeptideSegment(id="s2", type="binding", position=1, length_range=(20, 40), label="BD"),
        ],
    )
    client.peptides.generate(gene="CD8A", segment_config=sc)
    body = _body(httpx_mock)
    assert "segmentConfig" in body
    sc_wire = body["segmentConfig"]
    assert sc_wire["mode"] == "custom"
    segs = sc_wire["segments"]
    assert len(segs) == 2
    assert segs[0]["type"] == "premade"
    assert segs[0]["sequence"] == "GRKKRRQRRRPPQ"
    assert segs[1]["type"] == "binding"
    assert segs[1]["lengthRange"] == [20, 40]


def test_segment_config_dict_passthrough(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    raw = {"mode": "custom", "segments": [{"id": "s1", "type": "linker", "position": 0, "lengthRange": [3, 8]}]}
    client.peptides.generate(gene="EGFR", segment_config=raw)
    body = _body(httpx_mock)
    assert body["segmentConfig"] == raw


def test_no_segment_config_omitted(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(gene="EGFR")
    body = _body(httpx_mock)
    assert "segmentConfig" not in body


# ---------------------------------------------------------------------------
# PDC config
# ---------------------------------------------------------------------------


def test_pdc_config_pydantic_sets_pdc_enabled(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    pdc = PdcConfig(drug_name="doxorubicin", linker_sequence="GFLG", linker_position="c_terminal")
    client.peptides.generate(gene="EGFR", pdc_config=pdc)
    body = _body(httpx_mock)
    assert body.get("pdcEnabled") is True
    assert "pdcConfig" in body
    cfg = body["pdcConfig"]
    assert cfg["drugName"] == "doxorubicin"
    assert cfg["linkerSequence"] == "GFLG"
    assert cfg["linkerPosition"] == "c_terminal"


def test_pdc_config_custom_smiles(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    pdc = PdcConfig(drug_smiles="CC(=O)Oc1ccccc1C(=O)O", linker_sequence="PLGLAG")
    client.peptides.generate(gene="HER2", pdc_config=pdc)
    body = _body(httpx_mock)
    cfg = body["pdcConfig"]
    assert cfg["drugSmiles"] == "CC(=O)Oc1ccccc1C(=O)O"
    assert cfg["linkerSequence"] == "PLGLAG"


# ---------------------------------------------------------------------------
# EC trimming config
# ---------------------------------------------------------------------------


def test_ec_trimming_single_pass_defaults(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    ec = EcTrimmingConfig(remove_signal_peptide=True, generation_mode="ec_only", folding_mode="ec_only")
    client.peptides.generate(gene="HER2", ec_trimming_config=ec)
    body = _body(httpx_mock)
    assert "ecTrimming" in body
    et = body["ecTrimming"]
    assert et["removeSignalPeptide"] is True
    assert et["generationMode"] == "ec_only"
    assert et["foldingMode"] == "ec_only"


def test_ec_trimming_multipass_gpcr(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    ec = EcTrimmingConfig(generation_mode="ec_tm", folding_mode="trim_terminal_ic")
    client.peptides.generate(gene="ADRB2", ec_trimming_config=ec)
    body = _body(httpx_mock)
    et = body["ecTrimming"]
    assert et["generationMode"] == "ec_tm"
    assert et["foldingMode"] == "trim_terminal_ic"


def test_ec_trimming_none_omits_field(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(gene="EGFR")
    body = _body(httpx_mock)
    assert "ecTrimming" not in body


# ---------------------------------------------------------------------------
# sampling_steps / glycosylation_enabled
# ---------------------------------------------------------------------------


def test_sampling_steps_in_wire_body(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(gene="EGFR", sampling_steps=50)
    body = _body(httpx_mock)
    assert body["samplingSteps"] == 50


def test_sampling_steps_none_omitted(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(gene="EGFR")
    body = _body(httpx_mock)
    assert "samplingSteps" not in body


def test_glycosylation_enabled_true(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(gene="EGFR", glycosylation_enabled=True)
    body = _body(httpx_mock)
    assert body["glycosylationEnabled"] is True


def test_glycosylation_enabled_false_omitted(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    """glycosylation_enabled=None (default) should not appear in body."""
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(gene="EGFR")
    body = _body(httpx_mock)
    assert "glycosylationEnabled" not in body


# ---------------------------------------------------------------------------
# quality_guided
# ---------------------------------------------------------------------------


def test_quality_guided_true_in_wire_body(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(gene="EGFR", quality_guided=True)
    body = _body(httpx_mock)
    assert body["qualityGuidedEnabled"] is True


def test_quality_guided_false_default(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    """Default is False — server applies tier-aware default; client sends explicit False."""
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(gene="EGFR")
    body = _body(httpx_mock)
    assert body["qualityGuidedEnabled"] is False


def test_quality_guidance_scale_in_body(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(gene="EGFR", quality_guided=True, quality_guidance_scale=1.5)
    body = _body(httpx_mock)
    assert body["qualityGuidanceScale"] == pytest.approx(1.5)


def test_quality_guided_allowed_for_free_tier(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    c = LigandAI(api_key="lgai_free_testkey", base_url=BASE, max_retries=1)
    c.peptides.generate(gene="EGFR", quality_guided=True)
    body = _body(httpx_mock)
    assert body["qualityGuidedEnabled"] is True


# ---------------------------------------------------------------------------
# Immune guidance (immunogenicity)
# ---------------------------------------------------------------------------


def test_immunogenicity_enables_immuno_fields(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    """immunogenicity=True → immunoEnabled=True, immunoStrength forwarded."""
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(gene="EGFR", immunogenicity=True, immuno_strength=3.0)
    body = _body(httpx_mock)
    assert body["immunoEnabled"] is True
    assert body["immunoStrength"] == pytest.approx(3.0)


def test_immunogenicity_default_off(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(gene="EGFR")
    body = _body(httpx_mock)
    assert body["immunoEnabled"] is False


def test_immuno_modules_forwarded(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    modules = {"mhc_i": True, "mhc_ii": True}
    client.peptides.generate(gene="EGFR", immunogenicity=True, immuno_modules=modules)
    body = _body(httpx_mock)
    assert body.get("immunoModules") == modules


# ---------------------------------------------------------------------------
# Stability guidance (serum_stability)
# ---------------------------------------------------------------------------


def test_serum_stability_enables_stability_fields(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(gene="EGFR", serum_stability=True, stability_mode="resist", stability_strength=2.5)
    body = _body(httpx_mock)
    assert body["stabilityEnabled"] is True
    assert body["stabilityMode"] == "resist"
    assert body["stabilityStrength"] == pytest.approx(2.5)


def test_serum_stability_default_off(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(gene="EGFR")
    body = _body(httpx_mock)
    assert body["stabilityEnabled"] is False


def test_stability_target_mode_prodrug(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(gene="EGFR", serum_stability=True, stability_mode="target")
    body = _body(httpx_mock)
    assert body["stabilityMode"] == "target"


def test_stability_modules_forwarded(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    mods = {"trypsin": True, "dppiv": True, "elastase": False}
    client.peptides.generate(gene="EGFR", serum_stability=True, stability_modules=mods)
    body = _body(httpx_mock)
    assert body.get("stabilityModules") == mods


# ---------------------------------------------------------------------------
# Charge / solubility filters
# ---------------------------------------------------------------------------


def test_charge_mode_between_sends_range(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(gene="EGFR", charge_mode="between", charge_min=-2.5, charge_max=-0.5)
    body = _body(httpx_mock)
    assert body["chargeMode"] == "between"
    assert body["chargeMin"] == pytest.approx(-2.5)
    assert body["chargeMax"] == pytest.approx(-0.5)


def test_charge_mode_lt(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(gene="EGFR", charge_mode="lt", charge_value=-0.5)
    body = _body(httpx_mock)
    assert body["chargeMode"] == "lt"
    assert body["chargeValue"] == pytest.approx(-0.5)


def test_min_solubility_in_body(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(gene="EGFR", min_solubility=1.0)
    body = _body(httpx_mock)
    assert body["minSolubility"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Tier-gate error propagation
# ---------------------------------------------------------------------------


def test_tier_gate_free_key_raises_ligandai_tier_error() -> None:
    """Free-tier key → client-side LigandAITierError for academia+ guidance."""
    c = LigandAI(api_key="lgai_free_testkey", base_url=BASE, max_retries=1)
    with pytest.raises(LigandAITierError) as exc_info:
        c.peptides.generate(gene="EGFR", immunogenicity=True)
    assert exc_info.value.required_tier == "academia"
    assert exc_info.value.current_tier == "free"


def test_tier_gate_free_key_blocks_serum_stability() -> None:
    c = LigandAI(api_key="lgai_free_testkey", base_url=BASE, max_retries=1)
    with pytest.raises(LigandAITierError) as exc_info:
        c.peptides.generate(gene="EGFR", serum_stability=True)
    assert exc_info.value.required_tier == "academia"


def test_tier_gate_free_key_blocks_logits_output() -> None:
    c = LigandAI(api_key="lgai_free_testkey", base_url=BASE, max_retries=1)
    with pytest.raises(LigandAITierError) as exc_info:
        c.peptides.generate(gene="EGFR", return_logits=True)
    assert exc_info.value.required_tier == "academia"


def test_tier_gate_unknown_key_prefix_raises_ligandai_tier_error() -> None:
    """Unrecognized key prefix → tier=None → below all tiers → LigandAITierError."""
    c = LigandAI(api_key="unknown_prefix_key", base_url=BASE, max_retries=1)
    with pytest.raises(LigandAITierError):
        c.peptides.generate(gene="EGFR", cyclic_mode="disulfide")
