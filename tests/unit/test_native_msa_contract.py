"""Contract regressions using recorded EGFR native-featurizer identities/counts.

The normalized wire fixture below adapts the real Oct-1 Beta CPU witness. It is
not a claim that a deployed worker emitted the new production envelope.
"""
import copy
import hashlib
import json

import pytest

from ligandai import AsyncLigandAI, LigandAI
from ligandai.resources.msa_contract import NativeMSAReceipt, get_native_msa_receipt, validate_prepared_msa
from ligandai.resources.peptides import _fold_body, _parse_cofold, _parse_fold, _resolve_segment_config
from ligandai.resources.synthesis import _adaptyv_sequence_entry
from ligandai.types import FoldResult, Sequence

HUMAN = "RKVCNGIGIGEFKDSLSINATNIKHFKNCTSISGDLHILPVAFRGDSFTHTPPLDPQELDILKTVKEITGFLLIQAWPENRTDLHAFENLEIIRGRTKQHGQFSLAVVSLNITSLGLRSLKEISDGDVIISGNKNLCYANTINWKKLFGTSGQKTKIISNRGENSCKATGQVCHALCSPEGCWGPEPRDCVSCRNVSRGRECVDK"
MOUSE = "RKVCNGIGIGEFKDTLSINATNIKHFKYCTAISGDLHILPVAFKGDSFTRTPPLDPRELEILKTVKEITGFLLIQAWPDNWTDLHAFENLEIIRGRTKQHGQFSLAVVGLNITSLGLRSLKEISDGDVIISGNRNLCYANTINWKKLFGTPNQKTKIMNNRAEKDCKAVNHVCNPLCSSEGCWGPEPRDCVSCQNVSRGRECVEK"
CORE = "GDGAVSVNHSRHFQRCETPAGSPKNGLEALFGSATLIVCKSETQKLEICLIRAICITFYA"
LINKER = "GGSGGSGGSGG"
TAG = "WSHPQFEKGGGSAWSHPQFEKRDHMVLHEYVNAAGIT"


def native_receipt():
    return {"schema": "ligandai.native_msa.v1", "evidence_stage": "native_featurizer", "runs": [{
        "status": "completed", "engine": "boltz",
        "input_identity_sha256": "2ceeb72d908b715c6169a72e52c1b392fa42f96320df02fdf9daa189358378f4",
        "input_sha256": "ef23de7ff9d37f6f1bbbf13aff448f86f8e25a8c230c7d245c7355c2da25ac89",
        "guard_sha256": "c2621d2f663fd09a00e99c3b242b467e365247531b3b71001fe676bba2f6a3cf",
        "native_source_sha256": "af1f9e6ec0c3d7289eb1f7503c3c1c3ee0d9f03436778dd4a7d412b8f7bd9f94",
        "fresh_output": "/home/dre/ligandai_audits/joint_egfr_msa_repair_20261001/consumer/producer_validation/2ceeb72d908b715c6169a72e52c1b392fa42f96320df02fdf9daa189358378f4",
        "witnesses": [{"record_id": "s0042_human_linker", "chains": [{
            "chain_name": "A", "chain_id": 0, "policy": "receptor_homologs_required",
            "query_exact": True, "query_sha256": hashlib.sha256(HUMAN.encode()).hexdigest(),
            "unique_non_gap_rows": 1057, "shape": [1057, 205],
            "tensor_sha256": "f8a4054f52b70094e0122f1f0896d7438bdc868bf58f54306d067f46f2d86812",
        }, {
            "chain_name": "B", "chain_id": 1, "policy": "binder_query_only_allowed",
            "query_exact": True, "query_sha256": hashlib.sha256((CORE + LINKER).encode()).hexdigest(),
            "unique_non_gap_rows": 1, "shape": [1057, 71],
            "tensor_sha256": "527e7faf3e71c453a71be557bd9253658e569dc057ee8d743cbd41371ae01ee1",
        }]}]}]}


@pytest.mark.parametrize("envelope", ["top", "result", "output_data", "outputData"])
def test_boltz_receipt_survives_real_fold_parser(envelope):
    raw = {"prediction_metadata": {"msa_consumption": native_receipt()}}
    payload = {"jobId": "recorded-cpu-contract", **raw} if envelope == "top" else {"jobId": "recorded-cpu-contract", envelope: raw}
    result = _parse_fold(payload)
    assert isinstance(result, FoldResult)
    assert result.msa_evidence_stage == "native_featurizer"
    result.msa_consumption.require_queries({"A": HUMAN, "B": CORE + LINKER})
    assert result.msa_consumption.runs[0].witnesses[0].chains[0].shape == [1057, 205]
    assert result.model_dump(mode="json", by_alias=True)["msa_consumption"]["schema"] == "ligandai.native_msa.v1"


def test_top_level_protenix_receipt_and_missing_are_distinct():
    raw = native_receipt()
    raw["runs"][0]["engine"] = "protenix"  # schema variant, not a new measured Protenix result
    for chain in raw["runs"][0]["witnesses"][0]["chains"]:
        chain["chain_index"] = chain.pop("chain_id")
    assert _parse_fold({"jobId": "schema-variant", "msa_consumption": raw}).msa_consumption.runs[0].engine == "protenix"
    absent = _parse_fold({"jobId": "historical", "msa_depth": 100, "verified": True})
    assert absent.msa_consumption is None and absent.msa_evidence_stage == "unavailable"


@pytest.mark.parametrize("field,value", [("status", "running"), ("fresh_output", ""),
    ("fresh_output", True), ("fresh_output", "relative"), ("input_sha256", "bad"),
    ("guard_sha256", None), ("native_source_sha256", {}), ("witnesses", [])])
def test_invalid_run_rejected(field, value):
    raw = native_receipt(); raw["runs"][0][field] = value
    with pytest.raises(ValueError): NativeMSAReceipt.model_validate(raw)


@pytest.mark.parametrize("field,value", [("query_exact", False), ("query_exact", 1),
    ("query_sha256", "bad"), ("shape", [0, 205]), ("shape", [1057, True]),
    ("unique_non_gap_rows", 1), ("unique_non_gap_rows", 1058), ("chain_name", ""),
    ("tensor_sha256", "bad"), ("chain_id", None)])
def test_invalid_receptor_witness_rejected(field, value):
    raw = native_receipt(); raw["runs"][0]["witnesses"][0]["chains"][0][field] = value
    with pytest.raises(ValueError): NativeMSAReceipt.model_validate(raw)


def test_exact_same_length_wrong_construct_and_tagged_binder_rejected():
    receipt = NativeMSAReceipt.model_validate(native_receipt())
    for queries in [{"A": MOUSE, "B": CORE + LINKER}, {"A": HUMAN, "B": CORE + LINKER + TAG}, {"A": HUMAN}]:
        with pytest.raises(ValueError): receipt.require_queries(queries)
    with pytest.raises(ValueError): receipt.require_queries({"A": HUMAN, "B": CORE + LINKER}, record_id="unrelated")


def test_conflicting_envelopes_and_duplicate_chain_refused():
    first = native_receipt(); other = copy.deepcopy(first); other["runs"][0]["input_sha256"] = "0" * 64
    with pytest.raises(ValueError): get_native_msa_receipt({"msa_consumption": first, "prediction_metadata": {"msa_consumption": other}})
    first["runs"][0]["witnesses"][0]["chains"].append(copy.deepcopy(first["runs"][0]["witnesses"][0]["chains"][0]))
    with pytest.raises(ValueError): NativeMSAReceipt.model_validate(first)


def test_cofold_receipt_validated_without_breaking_json_transport():
    parsed = _parse_cofold({"result": {"engines": {"boltz2": {"prediction_metadata": {"msa_consumption": native_receipt()}}, "historical": {}}}})
    json.dumps(parsed)
    assert get_native_msa_receipt(parsed["engines"]["boltz2"]).runs[0].engine == "boltz"
    assert parsed["engines"]["historical"]["msa_consumption_status"] == "unavailable"


def prepared_payload():
    # Two real EGFR homolog sequences; this is a preparation fixture, not a new search.
    return {"chains": {"A": {"csv": f"key,sequence\n-1,{HUMAN}\n10090,{MOUSE}\n", "hits": 2}}, "cached": True}


def test_prepared_alignment_is_not_native_proof_and_bytes_survive():
    raw = prepared_payload(); result = validate_prepared_msa(raw, {"A": HUMAN})
    chain = result.chains["A"]
    assert chain.csv == raw["chains"]["A"]["csv"]
    assert chain.prepared.query_sha256 == hashlib.sha256(HUMAN.encode()).hexdigest()
    assert chain.prepared.evidence_stage == "prepared_alignment"
    assert chain.prepared.native_consumption_verified is False


@pytest.mark.parametrize("change", ["wrong_query", "wrong_width", "all_gap", "bad_pairing", "missing_chain"])
def test_prepared_invalid_source_refused(change):
    raw = prepared_payload()
    if change == "wrong_query": raw["chains"]["A"]["csv"] = raw["chains"]["A"]["csv"].replace(HUMAN, MOUSE)
    if change == "wrong_width": raw["chains"]["A"]["csv"] += "4,ACD\n"
    if change == "all_gap": raw["chains"]["A"]["csv"] += "4," + "-" * len(HUMAN) + "\n"
    if change == "bad_pairing": raw["chains"]["A"]["csv"] = raw["chains"]["A"]["csv"].replace("10090,", "--2,")
    if change == "missing_chain": raw["chains"] = {}
    with pytest.raises(ValueError): validate_prepared_msa(raw, {"A": HUMAN})


def test_sync_msa_resource_validates_response(httpx_mock):
    httpx_mock.add_response(url="https://api.ligandai.test/api/msa/generate", json=prepared_payload())
    with LigandAI(api_key="lgai_pro_test", base_url="https://api.ligandai.test") as client:
        result = client.msa.generate({"A": HUMAN}, gene="EGFR")
    assert result.chains["A"].prepared.aligned_columns == 205
    assert json.loads(httpx_mock.get_request().content)["sequences"] == {"A": HUMAN}


@pytest.mark.asyncio
async def test_async_msa_resource_validates_response(httpx_mock):
    httpx_mock.add_response(url="https://api.ligandai.test/api/msa/generate", json=prepared_payload())
    async with AsyncLigandAI(api_key="lgai_pro_test", base_url="https://api.ligandai.test") as client:
        result = await client.msa.generate({"A": HUMAN})
    assert result.chains["A"].prepared.native_consumption_verified is False


@pytest.mark.parametrize("typed", [False, True])
def test_source_gene_range_survive_single_and_multi_chain_requests(typed):
    chain = {"sequence": HUMAN, "msaSourceGene": "EGFR", "geneRange": [334, 538]}
    if typed: chain = Sequence(**chain)
    for chains in [[chain], [chain, {"sequence": CORE + LINKER, "isPeptide": True}]]:
        body = _fold_body(chains)
        assert body["entities"][0]["msaSourceGene"] == "EGFR"
        assert body["entities"][0]["geneRange"] == [334, 538]
        assert body["entities"][0]["sequence"] == HUMAN


def test_fixed_linker_tag_contract_and_bli_core_transport():
    config = _resolve_segment_config([{"type": "binding", "lengthRange": [60, 60]},
        {"type": "premade", "sequence": LINKER, "locked": True, "label": "linker"},
        {"type": "premade", "sequence": TAG, "locked": True, "label": "tag"}], None)
    assert [s.sequence for s in config.segments[1:]] == [LINKER, TAG]
    assert all(s.locked for s in config.segments[1:])
    _, entry = _adaptyv_sequence_entry({"name": "s0042", "sequence": CORE}, 0, "C")
    assert entry["aa_string"] == CORE and entry["metadata"]["tag_location"] == "C"


@pytest.mark.parametrize("segment", [{"type": "premade"}, {"type": "linker", "locked": True},
    {"type": "premade", "sequence": TAG, "lengthRange": [1, 2]}])
def test_invalid_fixed_construct_refused(segment):
    with pytest.raises(ValueError): _resolve_segment_config([segment], None)
