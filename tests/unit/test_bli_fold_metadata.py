"""Exact full-construct transport; no structural or sensor-simulation claims."""
import copy
import hashlib

import pytest

from ligandai import BliConfig
from ligandai.resources.construct_contract import fold_linker_metadata, resolve_linker_metadata
from ligandai.resources.peptides import _fold_body
from ligandai.resources.peptides import _normalize_fold_groups, _parse_fold, _parse_cofold
from ligandai.resources.construct_contract import get_construct_regions
from ligandai.types import Sequence

# Recorded ctrl_s0042 core and literal custom spacer; the preset tags are
# already pinned by the Oct-1 full-construct pilot manifest.
CORE = "GDGAVSVNHSRHFQRCETPAGSPKNGLEALFGSATLIVCKSETQKLEICLIRAICITFYA"
SPACER = "GSGSGS"


def full_frame(position="c_terminal"):
    return BliConfig(tag_position=position, linker_sequence=SPACER).fold_plan(
        {"ctrl_s0042": CORE}, frames=("with_full_cassette",)).frames[0]


@pytest.mark.parametrize("position", ["c_terminal", "n_terminal"])
def test_literal_regions_tile_actual_whole_construct(position):
    frame = full_frame(position)
    boundary = resolve_linker_metadata(frame.sequence, frame.linker_metadata)
    assert boundary.sequence_sha256 == hashlib.sha256(frame.sequence.encode()).hexdigest()
    assert boundary.sequence_sha256 != hashlib.sha256(CORE.encode()).hexdigest()
    for role, expected in (("core", CORE), ("linker", SPACER), ("tag", frame.tag_residues)):
        assert "".join(aa for aa, keep in zip(frame.sequence, boundary.mask(role)) if keep) == expected
    assert boundary.mask("core") == frame.binder_mask(frame.sequence)
    assert sum(boundary.fixed_sequence_mask) == len(frame.sequence) - len(CORE)
    assert all(sum(boundary.mask(role)[i] for role in ("core", "linker", "tag")) == 1 for i in range(len(frame.sequence)))


@pytest.mark.parametrize("typed", [False, True])
@pytest.mark.parametrize("alias", ["linker_metadata", "linkerMetadata"])
def test_real_single_and_multichain_serializers_keep_full_construct_and_boundary(typed, alias):
    frame = full_frame()
    entity = frame.fold_entity()
    entity[alias] = entity.pop("linker_metadata")
    supplied = Sequence(**entity) if typed else entity
    for inputs in ([supplied], [{"sequence": CORE, "chainId": "A"}, supplied]):
        body = _fold_body(inputs)
        binder = body["entities"][-1]
        assert binder["sequence"] == frame.sequence
        assert binder["linker_metadata"] == frame.linker_metadata
        assert binder["linker_metadata"]["binder_end"] == len(CORE)
        assert binder["use_msa"] is False
        assert binder["isPeptide"] is True
        assert "fixed_coordinates" not in binder


def test_single_binder_without_chain_id_does_not_silently_drop_metadata():
    frame = full_frame()
    body = _fold_body([{"sequence": frame.sequence, "isPeptide": True, "linker_metadata": frame.linker_metadata}])
    assert body["entities"][0]["linker_metadata"]["sequence_sha256"] == frame.sequence_sha256


def test_unidentified_chain_cannot_claim_binder_boundary():
    frame = full_frame()
    with pytest.raises(ValueError, match="explicitly identified"):
        _fold_body([{"sequence": frame.sequence, "linker_metadata": frame.linker_metadata}])


@pytest.mark.parametrize("change", ["wrong_full_hash", "same_length_changed_sequence", "missing_end",
    "wrong_end", "wrong_core_length", "boolean_core_length", "stale_bounds", "placeholder", "overlong_tag"])
def test_ambiguous_stale_or_unresolved_construct_refused(change):
    frame = full_frame(); sequence = frame.sequence; metadata = frame.linker_metadata
    if change == "wrong_full_hash": metadata["sequence_sha256"] = hashlib.sha256(CORE.encode()).hexdigest()
    if change == "same_length_changed_sequence": sequence = "A" + sequence[1:]
    if change == "missing_end": metadata.pop("linker_terminus")
    if change == "wrong_end": metadata["tag_terminus"] = "N"
    if change == "wrong_core_length": metadata["novel_length"] -= 1
    if change == "boolean_core_length": metadata["novel_length"] = True
    if change == "stale_bounds": metadata["binder_end"] += 1
    if change == "placeholder": metadata.update(linker_origin="generative", linker_residues="------")
    if change == "overlong_tag": metadata["tag_residues"] += "A"
    with pytest.raises(ValueError): resolve_linker_metadata(sequence, metadata)


def test_alias_conflict_refused_without_mutating_original():
    frame = full_frame(); raw = frame.linker_metadata; before = copy.deepcopy(raw)
    wrong = {**raw, "novel_length": 5}
    with pytest.raises(ValueError): fold_linker_metadata({"linker_metadata": raw, "linkerMetadata": wrong}, frame.sequence)
    assert raw == before


@pytest.mark.parametrize("origin,fixed", [("fixed", True), ("generative", False), ("mixed", None), (None, None)])
def test_literal_residue_identity_does_not_invent_generation_fixedness(origin, fixed):
    frame = full_frame(); meta = frame.linker_metadata; meta["linker_origin"] = origin
    region = next(r for r in resolve_linker_metadata(frame.sequence, meta).regions if r.role == "linker")
    assert region.fixed_sequence is fixed


def test_opposite_explicit_scaffold_ends_and_binder_only_are_unambiguous():
    tag = "WSHPQFEK"
    sequence = tag + CORE + SPACER
    boundary = resolve_linker_metadata(sequence, {"tag_residues": tag, "tag_terminus": "N",
        "linker_residues": SPACER, "linker_terminus": "C", "linker_origin": "fixed", "novel_length": len(CORE)})
    assert [(r.role, r.start, r.end) for r in boundary.regions] == [
        ("tag", 0, len(tag)), ("core", len(tag), len(tag) + len(CORE)), ("linker", len(tag) + len(CORE), len(sequence))]
    frame = BliConfig().fold_plan({"core": CORE}, frames=("binder_only",)).frames[0]
    assert frame.linker_metadata["novel_length"] == len(CORE)
    assert resolve_linker_metadata(frame.sequence, frame.linker_metadata).fixed_sequence_mask == (False,) * len(CORE)


def test_plan_grouped_wire_preserves_parent_target_crop_and_whole_frame():
    plan = BliConfig(linker_sequence=SPACER).fold_plan({"ctrl_s0042": CORE})
    request = plan._submission({"receptor_sequence": CORE, "parent_gene": "EGFR", "gene_range": (334, 393),
                                "receptor_name": "exact-target", "msa_enabled": True, "seed": 17})
    groups = _normalize_fold_groups(request["groups"])
    assert request["seed"] == 17
    for group, frame in zip(groups, plan.frames):
        assert group["target"]["sequence"] == CORE
        assert group["target"]["msa_source_gene"] == "EGFR"
        assert group["target"]["gene_range"] == [334, 393]
        assert group["target"]["name"] == "exact-target"
        assert group["target"]["use_msa"] is True
        binder = group["partners"][0]
        assert binder["sequence"] == frame.sequence
        assert binder["linker_metadata"] == frame.linker_metadata
        assert binder["sequence_sha256"] == frame.sequence_sha256


def test_conflicting_target_identity_cannot_be_dropped_during_grouping():
    plan = BliConfig().fold_plan({"core": CORE})
    for extra in ({"parent_gene": "EGFR", "msa_source_gene": "ERBB2"}, {"gene_range": [0, 60]}):
        request = plan._submission({"target_gene": "EGFR", **extra})
        with pytest.raises(ValueError): _normalize_fold_groups(request["groups"])
    with pytest.raises(ValueError): plan._submission({"target_gene": "EGFR", "receptor_sequence": CORE})


def test_unavailable_region_receipt_survives_fold_and_cofold_without_fake_scores():
    raw = {"schema": "ligandai.construct_regions.v1", "score_scope": "post_fold_region_recalculation",
           "chains": {"B": {"status": "unavailable", "reason": "missing_linker_metadata", "core": None, "linker": None, "tag": None}}}
    payload = {"ipsae_scores": {"construct_regions": raw}}
    result = _parse_fold({"jobId": "unavailable-example", "result": payload})
    assert result.construct_regions.chains["B"].core is None
    with pytest.raises(ValueError): result.construct_regions.require_query("B", CORE)
    cofold = _parse_cofold({"result": {"engines": {"boltz2": payload}}})
    assert get_construct_regions(cofold["engines"]["boltz2"]).chains["B"].status == "unavailable"
    invalid = copy.deepcopy(raw); invalid["chains"]["B"]["core"] = {"ipsae": 0}
    with pytest.raises(ValueError): _parse_fold({"ipsae_scores": {"construct_regions": invalid}})
