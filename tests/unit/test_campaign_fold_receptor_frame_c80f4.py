# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""[bd-dre-c80f4.7] SDKPlatform.fold must actually fold a COMPLEX.

Three failures, all measured on the gonadotropin campaigns 2026-09-23, all in
one method:

1. It passed BOTH ``receptor_sequence=`` and ``target_gene=``.
   ``_build_batch_fold_body()`` requires exactly one of
   ``target_gene`` / ``receptor_pdb`` / ``receptor_sequence`` and raised
   ``ValueError: Pass exactly one of ...`` before any pose existed.
   ``Target.from_dict`` does ``con.gene = con.gene or gene``, so EVERY construct
   in EVERY real spec carries a gene — including the shipped worked example
   ``examples/campaign/ca12_selective_binders.yaml``. The fold stage could not
   run at all.

2. It passed ``cofactors=``, which ``fold_batch()`` does not accept.

3. A fold asked for against a receptor can come back as a SINGLE-CHAIN
   prediction of the peptide alone — ``status`` fine, row shape fine, header
   reading "Boltz-2 Single Chain Prediction ... CHAIN: A; LENGTH: 10". No
   interface exists on it, so every interface metric is absent, and absent then
   reads downstream as "this design scored badly" rather than "nobody folded a
   complex".
"""
from __future__ import annotations

import pytest

from ligandai.campaign.platform import SDKPlatform, _pdb_chain_ids
from ligandai.campaign.spec import Construct, FoldSettings

GNRH = "QHWSYGLRPG"
# A short stand-in receptor; identity is the sequence, and the point of these
# tests is which KWARGS go on the wire, not the biology of this fragment.
RECEPTOR = "MANSASPEQNQNHCSAINNSIPLMQGNLPTLTLSGKIRVTVTFFLFLLSATFNASFLLKLQKWTQKKEK"


class _Job:
    def __init__(self, payload):
        self._payload = payload
        self.id = "job_test"

    def wait(self, **_kw):
        return self._payload


class _Peptides:
    """Records the kwargs fold_batch was called with, and replays a payload."""

    def __init__(self, payload):
        self.payload = payload
        self.calls: list[dict] = []

    def fold_batch(self, peptides, **kwargs):
        self.calls.append({"peptides": list(peptides), **kwargs})
        return _Job(self.payload)


class _Client:
    def __init__(self, payload):
        self.peptides = _Peptides(payload)


class _Spec:
    """Only the attribute SDKPlatform.fold reads."""

    def __init__(self, fold: FoldSettings):
        self.fold = fold


def _construct(gene: str | None = "GNRHR") -> Construct:
    return Construct(name="GNRHR_full_1_328", sequence=RECEPTOR, gene=gene,
                     uniprot="P30968", residue_range=(1, 328))


def _two_chain_pdb() -> str:
    return (
        "ATOM      1  CA  MET A   1      11.104  13.207  10.000  1.00 90.00           C\n"
        "ATOM      2  CA  ALA A   2      12.104  14.207  10.000  1.00 90.00           C\n"
        "ATOM      3  CA  GLN B   1      21.104  23.207  10.000  1.00 80.00           C\n"
        "END\n"
    )


def _one_chain_pdb() -> str:
    return (
        "REMARK   Boltz-2 Single Chain Prediction\n"
        "REMARK   CHAIN: A; LENGTH: 10\n"
        "ATOM      1  CA  GLN A   1       1.104   3.207  10.000  1.00 70.00           C\n"
        "ATOM      2  CA  HIS A   2       2.104   4.207  10.000  1.00 70.00           C\n"
        "END\n"
    )


def _payload(pdb: str | None):
    row = {"sequence": GNRH, "peptide_ipsae": 0.71, "iptm": 0.83, "plddt": 89.0}
    if pdb is not None:
        row["pdb_content"] = pdb
    return {"folds": [row]}


def _platform(payload) -> tuple[SDKPlatform, _Client]:
    client = _Client(payload)
    return SDKPlatform(client=client, program_id=None, timeout=1, poll_interval=0), client


# ── 1. the receptor frame ────────────────────────────────────────────────────

def test_fold_never_sends_target_gene_next_to_receptor_sequence():
    """⛔ THE BLOCKING BUG: two receptor selectors in one request."""
    plat, client = _platform(_payload(_two_chain_pdb()))
    plat.fold(_Spec(FoldSettings()), [GNRH], construct=_construct("GNRHR"),
              cofactors=(), n_seed_units=1, label="target")
    sent = client.peptides.calls[0]
    assert "target_gene" not in sent, (
        "target_gene= alongside receptor_sequence= raises "
        "'Pass exactly one of target_gene=, receptor_pdb=, or receptor_sequence='"
    )
    assert sent["receptor_sequence"] == RECEPTOR, "the construct sequence is authoritative"


def test_fold_keeps_the_gene_as_the_MSA_SOURCE_not_the_receptor():
    """The gene is still useful — as the MSA parent, which is what it is for.

    Dropping it entirely would trade a hard error for a quietly worse fold: a
    fresh fragment search on a construct slice is often degenerate, and folding
    a receptor without an MSA produces garbage ipSAE.
    """
    plat, client = _platform(_payload(_two_chain_pdb()))
    plat.fold(_Spec(FoldSettings()), [GNRH], construct=_construct("GNRHR"),
              cofactors=(), n_seed_units=1, label="target")
    assert client.peptides.calls[0]["msa_source_gene"] == "GNRHR"


def test_fold_omits_the_msa_parent_when_the_spec_turned_the_receptor_MSA_OFF():
    """``msa_source_gene=`` is an MSA instruction; with MSA off it is a lie."""
    plat, client = _platform(_payload(_two_chain_pdb()))
    plat.fold(_Spec(FoldSettings(msa_receptor=False)), [GNRH],
              construct=_construct("GNRHR"), cofactors=(), n_seed_units=1, label="target")
    sent = client.peptides.calls[0]
    assert "msa_source_gene" not in sent
    assert "target_gene" not in sent


def test_fold_works_on_a_construct_with_no_gene_at_all():
    plat, client = _platform(_payload(_two_chain_pdb()))
    plat.fold(_Spec(FoldSettings()), [GNRH], construct=_construct(None),
              cofactors=(), n_seed_units=1, label="target")
    sent = client.peptides.calls[0]
    assert "msa_source_gene" not in sent and "target_gene" not in sent


def test_the_shipped_worked_example_would_have_raised():
    """Characterisation of the defect, against the real SDK validator."""
    from ligandai.resources.peptides import _build_batch_fold_body

    with pytest.raises(ValueError, match="exactly one of target_gene"):
        _build_batch_fold_body(
            peptides=[GNRH], target_gene="GNRHR", receptor_pdb=None,
            receptor_sequence=RECEPTOR, receptor_name=None, msa_source_gene=None,
            gene_range=None, diffusion_samples=4, sampling_steps=50,
            recycling_steps=3, step_scale=None, msa_enabled=True,
            glycosylation=None, template_mode=False, n_parallel_gpus=None,
            session_id=None, contribute_to_receptordb=None, num_trajectories=None,
            msa_depth=None, use_potentials=True, seed=None,
        )


# ── 2. cofactors ─────────────────────────────────────────────────────────────

def test_a_declared_cofactor_raises_instead_of_being_dropped():
    """A cofactor-dependent interface folded cofactor-free is a different
    measurement. fold_batch has no cofactor parameter, so say so — loudly,
    before any credit is spent — rather than folding apo and labelling it holo.
    """
    plat, client = _platform(_payload(_two_chain_pdb()))
    with pytest.raises(RuntimeError, match="no cofactor parameter"):
        plat.fold(_Spec(FoldSettings()), [GNRH], construct=_construct("GNRHR"),
                  cofactors=("ZN",), n_seed_units=1, label="target")
    assert client.peptides.calls == [], "nothing must reach the wire"


# ── 3. the single-chain fold ─────────────────────────────────────────────────

def test_a_single_chain_result_raises():
    plat, _ = _platform(_payload(_one_chain_pdb()))
    with pytest.raises(RuntimeError, match="ONE chain"):
        plat.fold(_Spec(FoldSettings()), [GNRH], construct=_construct("GNRHR"),
                  cofactors=(), n_seed_units=1, label="target")


def test_a_two_chain_result_passes_and_yields_a_pose():
    plat, _ = _platform(_payload(_two_chain_pdb()))
    poses = plat.fold(_Spec(FoldSettings()), [GNRH], construct=_construct("GNRHR"),
                      cofactors=(), n_seed_units=1, label="target")
    assert len(poses) == 1
    assert poses[0].sequence == GNRH
    assert poses[0].receptor_construct == "GNRHR_full_1_328"


def test_absent_coordinates_are_UNVERIFIED_not_a_failure():
    """⛔ A missing file is not a measurement.

    No coordinates means the frame could not be established. That is neither a
    pass nor a reject, so it must NOT raise — collapsing it into "single chain"
    would turn "I could not look" into "I looked and it was wrong".
    """
    plat, _ = _platform(_payload(None))
    poses = plat.fold(_Spec(FoldSettings()), [GNRH], construct=_construct("GNRHR"),
                      cofactors=(), n_seed_units=1, label="target")
    assert len(poses) == 1


@pytest.mark.parametrize("pdb,expected", [
    (_two_chain_pdb(), {"A", "B"}),
    (_one_chain_pdb(), {"A"}),
])
def test_pdb_chain_ids_reads_coordinates(pdb, expected):
    assert _pdb_chain_ids({"pdb_content": pdb}) == expected


@pytest.mark.parametrize("row", [
    {},
    {"pdb_content": ""},
    {"pdb_content": "   "},
    {"pdb_content": "REMARK  header only, no coordinates\nEND\n"},
    {"pdb_content": 12345},
])
def test_pdb_chain_ids_returns_None_when_it_could_not_look(row):
    assert _pdb_chain_ids(row) is None


def test_pdb_chain_ids_reads_the_camelCase_and_pdb_data_spellings():
    assert _pdb_chain_ids({"pdbContent": _two_chain_pdb()}) == {"A", "B"}
    assert _pdb_chain_ids({"pdb_data": _two_chain_pdb()}) == {"A", "B"}
