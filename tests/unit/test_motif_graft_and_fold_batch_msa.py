# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Unit tests for motif-grafting (segments=) and arbitrary-seq MSA on fold_batch.

Refs: bd-dre-h0cuo.1 (fold_batch msa_source_gene / parent_gene / gene_range),
      bd-dre-h0cuo.4 (motif-grafting: segments= kwarg, auto-id/position,
                      ResidueRange serialization).

Covers:
  - segments=[...] convenience wraps into SegmentConfig(mode="custom", ...) with
    auto id/position from list order; type="generated" aliases to "binding".
  - segments= and segment_config= are mutually exclusive (raises).
  - A bare ResidueRange in target_residues serializes to a single dict (NOT the
    pydantic (field, value) tuple corruption).
  - fold_batch forwards msa_source_gene / parent_gene (alias) / gene_range to the
    predict-batch body; parent_gene conflict and bad ranges raise.
"""

from __future__ import annotations

import json

import pytest
from pytest_httpx import HTTPXMock

from ligandai import LigandAI
from ligandai.resources.peptides import (
    _build_batch_fold_body,
    _resolve_segment_config,
)
from ligandai.types import PeptideSegment, ResidueRange, SegmentConfig

BASE = "http://api.ligandai.test"
GEN_URL = f"{BASE}/api/ptf/parallel/generate"
FOLD_BATCH_URL = f"{BASE}/api/v1/folding/predict-batch"

_QUEUED = {"sessionId": "sid_test", "status": "queued"}
_BATCH_QUEUED = {
    "batch_id": "fb_test",
    "jobs": [],
    "total_cost_credits": 0,
    "peptide_count": 1,
    "trajectories_per_peptide": 1,
    "sampling_steps": 50,
}


@pytest.fixture
def client() -> LigandAI:
    return LigandAI(api_key="lgai_pro_test", base_url=BASE, max_retries=1)


def _body(httpx_mock: HTTPXMock) -> dict:
    req = httpx_mock.get_request()
    assert req is not None
    return json.loads(req.content)


# -- motif-grafting: segments= convenience ---------------------------------


def test_resolve_segment_config_auto_numbers() -> None:
    sc = _resolve_segment_config(
        [
            PeptideSegment(type="premade", sequence="SNRFTCREGYL"),
            {"type": "generated", "lengthRange": [9, 39]},
        ],
        None,
    )
    assert isinstance(sc, SegmentConfig)
    assert sc.mode == "custom"
    assert sc.segments[0].id == "seg0" and sc.segments[0].position == 0
    assert sc.segments[0].type == "premade"
    # type="generated" aliased to "binding"
    assert sc.segments[1].id == "seg1" and sc.segments[1].position == 1
    assert sc.segments[1].type == "binding"


def test_resolve_segment_config_explicit_numbers_preserved() -> None:
    sc = _resolve_segment_config(
        [PeptideSegment(id="cap", type="premade", sequence="GG", position=7)],
        None,
    )
    assert sc.segments[0].id == "cap" and sc.segments[0].position == 7


def test_segments_and_segment_config_mutually_exclusive() -> None:
    with pytest.raises(ValueError):
        _resolve_segment_config([PeptideSegment(type="binding")], SegmentConfig())


def test_resolve_segment_config_none_passthrough() -> None:
    assert _resolve_segment_config(None, None) is None
    sc = SegmentConfig(mode="custom")
    assert _resolve_segment_config(None, sc) is sc


def test_generate_segments_serializes_to_wire_body(
    httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(
        gene="PRLR",
        segments=[
            PeptideSegment(type="premade", sequence="SNRFTCREGYL"),
            PeptideSegment(type="generated", length_range=(9, 39)),
        ],
    )
    body = _body(httpx_mock)
    seg_cfg = body.get("segmentConfig") or body.get("segment_config")
    assert seg_cfg is not None, body.keys()
    assert seg_cfg["mode"] == "custom"
    segs = seg_cfg["segments"]
    assert segs[0]["sequence"] == "SNRFTCREGYL"
    assert segs[0]["position"] == 0 and segs[0]["id"] == "seg0"
    assert segs[1]["type"] == "binding" and segs[1]["position"] == 1


# -- ResidueRange serialization (the bare-RR corruption guard) --------------


def test_bare_residue_range_does_not_corrupt(
    httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    """A single ResidueRange passed (not in a list) must serialize to ONE dict,
    not the iterated pydantic (field, value) tuples."""
    httpx_mock.add_response(url=GEN_URL, method="POST", json=_QUEUED)
    client.peptides.generate(
        gene="GPER1",
        target_residues=[ResidueRange(chain="A", start=140, end=160, label="pocket")],
        targeting_strategy="pocket_targeted",
    )
    body = _body(httpx_mock)
    tr = body["targets"][0]["targetResidues"]
    assert tr == [{"chain": "A", "start": 140, "end": 160, "label": "pocket"}]


# -- fold_batch arbitrary-seq MSA ------------------------------------------


def test_fold_batch_body_msa_source_gene() -> None:
    body = _build_batch_fold_body(
        peptides=["ACDEFGHIK"],
        target_gene=None,
        receptor_pdb=None,
        receptor_sequence="MKTAYIAKQRQISFVK",
        receptor_name=None,
        diffusion_samples=1,
        sampling_steps=50,
        recycling_steps=None,
        step_scale=None,
        msa_enabled=None,
        glycosylation=None,
        template_mode=False,
        n_parallel_gpus=None,
        session_id=None,
        contribute_to_receptordb=None,
        msa_source_gene="COL17A1",
    )
    assert body["msa_source_gene"] == "COL17A1"
    assert body["receptor_sequence"] == "MKTAYIAKQRQISFVK"


def test_fold_batch_body_gene_range() -> None:
    body = _build_batch_fold_body(
        peptides=["ACDEFGHIK"],
        target_gene="COL17A1",
        receptor_pdb=None,
        receptor_sequence=None,
        receptor_name=None,
        diffusion_samples=1,
        sampling_steps=50,
        recycling_steps=None,
        step_scale=None,
        msa_enabled=None,
        glycosylation=None,
        template_mode=False,
        n_parallel_gpus=None,
        session_id=None,
        contribute_to_receptordb=None,
        gene_range=(489, 566),
    )
    assert body["gene_range"] == [489, 566]
    assert body["target_gene"] == "COL17A1"


def test_fold_batch_body_gene_range_requires_target_gene() -> None:
    with pytest.raises(ValueError):
        _build_batch_fold_body(
            peptides=["A"],
            target_gene=None,
            receptor_pdb=None,
            receptor_sequence="SEQ",
            receptor_name=None,
            diffusion_samples=1,
            sampling_steps=50,
            recycling_steps=None,
            step_scale=None,
            msa_enabled=None,
            glycosylation=None,
            template_mode=False,
            n_parallel_gpus=None,
            session_id=None,
            contribute_to_receptordb=None,
            gene_range=(1, 5),
        )


def test_fold_batch_body_msa_source_gene_requires_receptor_sequence() -> None:
    with pytest.raises(ValueError):
        _build_batch_fold_body(
            peptides=["A"],
            target_gene="EGFR",
            receptor_pdb=None,
            receptor_sequence=None,
            receptor_name=None,
            diffusion_samples=1,
            sampling_steps=50,
            recycling_steps=None,
            step_scale=None,
            msa_enabled=None,
            glycosylation=None,
            template_mode=False,
            n_parallel_gpus=None,
            session_id=None,
            contribute_to_receptordb=None,
            msa_source_gene="COL17A1",
        )


def test_fold_batch_body_bad_gene_range() -> None:
    with pytest.raises(ValueError):
        _build_batch_fold_body(
            peptides=["A"],
            target_gene="COL17A1",
            receptor_pdb=None,
            receptor_sequence=None,
            receptor_name=None,
            diffusion_samples=1,
            sampling_steps=50,
            recycling_steps=None,
            step_scale=None,
            msa_enabled=None,
            glycosylation=None,
            template_mode=False,
            n_parallel_gpus=None,
            session_id=None,
            contribute_to_receptordb=None,
            gene_range=(566, 489),  # end < start
        )


def test_fold_batch_parent_gene_alias_wire(
    httpx_mock: HTTPXMock, client: LigandAI
) -> None:
    """parent_gene= is an alias for msa_source_gene= and reaches the wire body."""
    httpx_mock.add_response(url=FOLD_BATCH_URL, method="POST", json=_BATCH_QUEUED)
    client.peptides.fold_batch(
        peptides=["ACDEFGHIK"],
        receptor_sequence="MKTAYIAKQRQISFVK",
        parent_gene="COL17A1",
    )
    body = _body(httpx_mock)
    assert body["msa_source_gene"] == "COL17A1"


def test_fold_batch_parent_gene_conflict_raises(client: LigandAI) -> None:
    with pytest.raises(ValueError):
        client.peptides.fold_batch(
            peptides=["ACDEFGHIK"],
            receptor_sequence="MKTAYIAKQRQISFVK",
            msa_source_gene="COL17A1",
            parent_gene="EGFR",
        )
