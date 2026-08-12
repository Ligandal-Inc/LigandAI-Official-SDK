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
  - model → generatorModelId (emitted when set, ABSENT when not). All three
    known models (v6.5, v6.5.21, v6.9.21) currently accept the four guidance
    heads, including the typed cellfree/cellfree_strength kwargs. The
    deny-set + conflict-raise mechanism (bd-LIGANDAI_ALPHA_V2-sxc8j) is
    exercised only indirectly here since no shipped model triggers it today —
    it exists for a future model that ships without guidance support.

All tests use pytest-httpx to intercept the POST to /api/ptf/parallel/generate
and inspect the request body without touching a real server.
"""

from __future__ import annotations

import json

import pytest
from pytest_httpx import HTTPXMock

from ligandai import LigandAI
from ligandai.errors import LigandAIInvalidConfig, LigandAITierError
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


def test_quality_guided_true_default(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    """Default is True (set in 491a904) — generate() sends qualityGuidedEnabled=True
    unless the caller explicitly passes quality_guided=False."""
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(gene="EGFR")
    body = _body(httpx_mock)
    assert body["qualityGuidedEnabled"] is True


def test_quality_guided_explicit_false_in_body(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    """Explicit quality_guided=False is forwarded as qualityGuidedEnabled=False."""
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(gene="EGFR", quality_guided=False)
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


# ---------------------------------------------------------------------------
# Generator model selection (model= → generatorModelId)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model_id", ["v6.5.21", "v6.9.21", "v6.5"])
def test_model_emits_generator_model_id(
    httpx_mock: HTTPXMock,
    client: LigandAI,
    model_id: str,
) -> None:
    """Every selectable model id reaches the wire verbatim as generatorModelId."""
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(gene="EGFR", model=model_id)
    body = _body(httpx_mock)
    assert body["generatorModelId"] == model_id


def test_model_omitted_sends_no_generator_model_id(
    httpx_mock: HTTPXMock,
    client: LigandAI,
) -> None:
    """No model= → NO key on the wire, so the SERVER default applies.

    The SDK must not write the current default into the body. If it did, an SDK
    build would pin callers to whatever the platform default was on its release
    day, and a later server-side default change would never reach them.
    """
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(gene="EGFR")
    body = _body(httpx_mock)
    assert "generatorModelId" not in body


def test_model_explicit_none_sends_no_generator_model_id(
    httpx_mock: HTTPXMock,
    client: LigandAI,
) -> None:
    """model=None is the same as omitting it — server default, no key."""
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(gene="EGFR", model=None)
    body = _body(httpx_mock)
    assert "generatorModelId" not in body


def test_model_unknown_id_is_forwarded_not_gated(
    httpx_mock: HTTPXMock,
    client: LigandAI,
) -> None:
    """An id this SDK build does not know still goes out; the server registry decides.

    A client-side allowlist would make every future model unusable until users
    upgraded the SDK.
    """
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(gene="EGFR", model="v7.0.0-experimental")
    body = _body(httpx_mock)
    assert body["generatorModelId"] == "v7.0.0-experimental"


def test_unknown_model_with_guidance_is_forwarded_for_the_server_to_judge(
    httpx_mock: HTTPXMock,
    client: LigandAI,
) -> None:
    """Absence from the local capability set is NOT a capability claim.

    A published SDK cannot know whether a model released after it was built
    supports the guidance heads. So an unrecognized id is forwarded WITH the
    guidance flags, and the server's request validation — which reads
    ``supports_guidance_heads`` off the model registry — decides. Blocking here
    would reject a valid future model; assuming it is capable is equally wrong,
    which is exactly why the SDK does neither and defers.
    """
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(
        gene="EGFR",
        model="v7.0.0-experimental",
        serum_stability=True,
    )
    body = _body(httpx_mock)
    assert body["generatorModelId"] == "v7.0.0-experimental"
    assert body["stabilityEnabled"] is True


def test_model_is_whitespace_trimmed(httpx_mock: HTTPXMock, client: LigandAI) -> None:
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(gene="EGFR", model="  v6.5.21  ")
    body = _body(httpx_mock)
    assert body["generatorModelId"] == "v6.5.21"


@pytest.mark.parametrize("bad", ["", "   ", 6.521, [], {}])
def test_model_rejects_non_string_or_blank(client: LigandAI, bad: object) -> None:
    with pytest.raises(LigandAIInvalidConfig) as exc_info:
        client.peptides.generate(gene="EGFR", model=bad)  # type: ignore[arg-type]
    assert exc_info.value.field == "model"


def test_model_selection_survives_other_generation_settings(
    httpx_mock: HTTPXMock,
    client: LigandAI,
) -> None:
    """generatorModelId coexists with the rest of the body, and does not disturb it."""
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(
        gene="EGFR",
        model="v6.9.21",
        num_peptides=50,
        length_range=(20, 40),
        quality_guided=True,
        quality_guidance_scale=1.5,
    )
    body = _body(httpx_mock)
    assert body["generatorModelId"] == "v6.9.21"
    assert body["peptidesPerTarget"] == 50
    assert body["lengthRange"] == [20, 40]
    assert body["qualityGuidedEnabled"] is True
    assert body["qualityGuidanceScale"] == pytest.approx(1.5)


async def test_model_async_generate_emits_generator_model_id(
    httpx_mock: HTTPXMock,
) -> None:
    """The async surface threads model= through the SAME body builder."""
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    from ligandai import AsyncLigandAI

    async with AsyncLigandAI(api_key="lgai_pro_test", base_url=BASE, max_retries=1) as ac:
        await ac.peptides.generate(gene="EGFR", model="v6.9.21")
    body = _body(httpx_mock)
    assert body["generatorModelId"] == "v6.9.21"


# --- v6.9.21 supports guidance (bd-LIGANDAI_ALPHA_V2-sxc8j) -------------------
#
# The four guidance heads (proteolytic/serum stability, immunogenicity,
# half-life, cell-free) are the same v6.5-line runtime penalty functions on
# all three known models. v6.5/v6.5.21 route them through the streaming
# guided pool; v6.9.21 routes them through the normal batch-spawn path
# ("default_function" transport, no streaming surface) — same kernels, same
# strength calibration. v6.9.21 used to be denied this combination
# client-side; the worker has since been redeployed and verified to serve the
# four heads, so the deny-set is empty and this file exercises the ACCEPT path
# for all three models instead of a raise.


def test_v6921_without_guidance_is_accepted(
    httpx_mock: HTTPXMock,
    client: LigandAI,
) -> None:
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(gene="EGFR", model="v6.9.21", num_peptides=25)
    body = _body(httpx_mock)
    assert body["generatorModelId"] == "v6.9.21"
    assert body["immunoEnabled"] is False
    assert body["stabilityEnabled"] is False
    assert body["halflifeEnabled"] is False
    assert body["cellfreeEnabled"] is False


def test_v6921_quality_guided_is_not_a_conflict(
    httpx_mock: HTTPXMock,
    client: LigandAI,
) -> None:
    """quality_guided is a different mechanism from the four penalty heads.

    It also defaults to True, so treating it as a conflict would make any
    guidance-less future model unusable without an explicit opt-out on every
    call.
    """
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(gene="EGFR", model="v6.9.21", quality_guided=True)
    body = _body(httpx_mock)
    assert body["generatorModelId"] == "v6.9.21"
    assert body["qualityGuidedEnabled"] is True


@pytest.mark.parametrize("model_id", ["v6.5.21", "v6.5", "v6.9.21", None])
def test_guidance_allowed_on_all_known_models(
    httpx_mock: HTTPXMock,
    client: LigandAI,
    model_id: str | None,
) -> None:
    """All three known models (plus the server-default None) accept guidance."""
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(
        gene="EGFR",
        model=model_id,
        serum_stability=True,
        immunogenicity=True,
        residue_composition="extended",
        cellfree=True,
        cellfree_strength=2.0,
    )
    body = _body(httpx_mock)
    assert body["stabilityEnabled"] is True
    assert body["immunoEnabled"] is True
    assert body["residueCompositionTarget"] == "extended"
    assert body["cellfreeEnabled"] is True
    assert body["cellfreeStrength"] == pytest.approx(2.0)


def test_cellfree_defaults_to_disabled_with_platform_default_strength(
    httpx_mock: HTTPXMock,
    client: LigandAI,
) -> None:
    """Pins the CLIENT default at 1.5.

    ⛔ This is knowingly NOT the worker signature's own default of 2.0
    (ligandforge_v6_5_guided_inference_unified.py:2118). Every client — this
    SDK, the Node SDK, the website, FastAPI — sends 1.5 explicitly, so the
    worker default never applies to a client-originated run. The two numbers
    disagree and one is wrong; resolving it changes what every default request
    does, so it is a platform decision, not a unilateral SDK edit
    (bd-LIGANDAI_ALPHA_V2-b4f34). This test exists to make a silent drift of
    that number fail loudly rather than ship.
    """
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(gene="EGFR")
    body = _body(httpx_mock)
    assert body["cellfreeEnabled"] is False
    assert body["cellfreeStrength"] == pytest.approx(1.5)


# ---------------------------------------------------------------------------
# halflife -> residue_composition rename + back-compat alias
# [bd-LIGANDAI_ALPHA_V2-b4f34.5]
#
# The knob never measured a half-life; it shapes sequence-level composition.
# The rename is only safe if EVERY existing caller keeps working, so these
# tests pin the alias contract rather than just the new spelling.
# ---------------------------------------------------------------------------


def test_residue_composition_emits_canonical_and_legacy_wire_keys(
    httpx_mock: HTTPXMock,
    client: LigandAI,
) -> None:
    """Both spellings go on the wire, with identical values.

    New key only => a server that has not taken the rename drops the knob
    (silent no-op). Legacy key only => the SDK keeps a dead name alive. Both is
    the one option that cannot silently no-op against either server generation.
    """
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(
        gene="EGFR",
        residue_composition="extended",
        residue_composition_strength=1.0,
    )
    body = _body(httpx_mock)
    assert body["residueCompositionEnabled"] is True
    assert body["residueCompositionTarget"] == "extended"
    assert body["residueCompositionStrength"] == pytest.approx(1.0)
    assert body["halflifeEnabled"] is True
    assert body["halflifeTarget"] == "extended"
    assert body["halflifeStrength"] == pytest.approx(1.0)
    # Canonical spelling => no alias provenance.
    assert "residueCompositionAliasUsed" not in body


def test_legacy_halflife_still_works_and_warns(
    httpx_mock: HTTPXMock,
    client: LigandAI,
) -> None:
    """An existing caller on the old name must not break — it must warn."""
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    with pytest.warns(DeprecationWarning, match="residue_composition"):
        client.peptides.generate(
            gene="EGFR",
            halflife="moderate",
            halflife_strength=1.0,
        )
    body = _body(httpx_mock)
    assert body["residueCompositionTarget"] == "moderate"
    assert body["residueCompositionStrength"] == pytest.approx(1.0)
    assert body["halflifeTarget"] == "moderate"
    assert body["residueCompositionAliasUsed"] == "halflife"


def test_new_name_wins_when_both_supplied_and_they_disagree(
    httpx_mock: HTTPXMock,
    client: LigandAI,
) -> None:
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    with pytest.warns(DeprecationWarning, match="NEW name wins"):
        client.peptides.generate(
            gene="EGFR",
            residue_composition="extended",
            residue_composition_strength=1.0,
            halflife="moderate",
            halflife_strength=3.0,
        )
    body = _body(httpx_mock)
    assert body["residueCompositionTarget"] == "extended"
    assert body["residueCompositionStrength"] == pytest.approx(1.0)
    assert body["halflifeTarget"] == "extended"
    assert body["halflifeStrength"] == pytest.approx(1.0)
    # The new name carried the request, so no legacy alias is recorded.
    assert "residueCompositionAliasUsed" not in body


def test_both_supplied_and_agreeing_warns_without_conflict(
    httpx_mock: HTTPXMock,
    client: LigandAI,
) -> None:
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    with pytest.warns(DeprecationWarning) as record:
        client.peptides.generate(
            gene="EGFR",
            residue_composition="extended",
            halflife="extended",
        )
    messages = [str(w.message) for w in record]
    assert any("deprecated names" in m for m in messages)
    assert not any("they disagree" in m for m in messages)


def test_residue_composition_absent_emits_disabled_under_both_names(
    httpx_mock: HTTPXMock,
    client: LigandAI,
) -> None:
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(gene="EGFR")
    body = _body(httpx_mock)
    assert body["residueCompositionEnabled"] is False
    assert body["halflifeEnabled"] is False
    assert "residueCompositionTarget" not in body
    assert "halflifeTarget" not in body


def test_strength_above_ceiling_warns_but_is_not_rewritten(
    httpx_mock: HTTPXMock,
    client: LigandAI,
) -> None:
    """The SDK is not the enforcement point — it warns and sends verbatim.

    Clamping client-side would hide the fact that the worker clamped, and the
    two clamps would drift.
    """
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    with pytest.warns(UserWarning, match="exceeds the server-side ceiling"):
        client.peptides.generate(
            gene="EGFR",
            residue_composition="extended",
            residue_composition_strength=8.0,
        )
    body = _body(httpx_mock)
    assert body["residueCompositionStrength"] == pytest.approx(8.0)


def test_negative_strength_is_rejected_client_side(client: LigandAI) -> None:
    """A negative strength inverts every penalty sign — not a supported config."""
    with pytest.raises(LigandAIInvalidConfig, match="below the minimum"):
        client.peptides.generate(
            gene="EGFR",
            residue_composition="extended",
            residue_composition_strength=-1.0,
        )


def test_cysteine_enabled_is_tri_state_on_the_wire(
    httpx_mock: HTTPXMock,
    client: LigandAI,
) -> None:
    """Absent key preserves legacy resolution; an explicit bool is forwarded."""
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(gene="EGFR")
    assert "cysteineEnabled" not in _body(httpx_mock)


@pytest.mark.parametrize("flag", [True, False])
def test_cysteine_enabled_explicit_value_reaches_the_wire(
    httpx_mock: HTTPXMock,
    client: LigandAI,
    flag: bool,
) -> None:
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(gene="EGFR", cysteine_enabled=flag)
    body = _body(httpx_mock)
    assert body["cysteineEnabled"] is flag
    assert body["cysteineMode"] == "disulfide_only"


async def test_legacy_halflife_alias_works_on_the_async_surface(
    httpx_mock: HTTPXMock,
) -> None:
    """Both resource surfaces share one resolver — pin that they agree.

    A rename that lands on the sync surface only is a rename that half the
    callers never see.
    """
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    from ligandai import AsyncLigandAI

    async with AsyncLigandAI(api_key="lgai_pro_test", base_url=BASE, max_retries=1) as ac:
        with pytest.warns(DeprecationWarning, match="residue_composition"):
            await ac.peptides.generate(gene="EGFR", halflife="extended")
    body = _body(httpx_mock)
    assert body["residueCompositionTarget"] == "extended"
    assert body["halflifeTarget"] == "extended"
    assert body["residueCompositionAliasUsed"] == "halflife"


def test_cellfree_via_extra_still_lands_on_the_wire(
    httpx_mock: HTTPXMock,
    client: LigandAI,
) -> None:
    """The typed `cellfree` kwarg is the blessed surface, but **extra remains a
    supported bypass — it must not be silently dropped or ignored in favor of
    the typed field.
    """
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(
        gene="EGFR",
        model="v6.9.21",
        cellfreeEnabled=True,
        cellfreeStrength=3.0,
    )
    body = _body(httpx_mock)
    assert body["cellfreeEnabled"] is True
    assert body["cellfreeStrength"] == pytest.approx(3.0)


async def test_v6921_guidance_accepted_on_async_surface(httpx_mock: HTTPXMock) -> None:
    """Both resource surfaces accept it — they share one body builder."""
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    from ligandai import AsyncLigandAI

    async with AsyncLigandAI(api_key="lgai_pro_test", base_url=BASE, max_retries=1) as ac:
        await ac.peptides.generate(
            gene="EGFR",
            model="v6.9.21",
            serum_stability=True,
            cellfree=True,
        )
    body = _body(httpx_mock)
    assert body["generatorModelId"] == "v6.9.21"
    assert body["stabilityEnabled"] is True
    assert body["cellfreeEnabled"] is True
