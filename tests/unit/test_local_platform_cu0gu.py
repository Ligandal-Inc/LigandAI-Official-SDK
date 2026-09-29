# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Guards in ``LocalPlatform`` that no GPU is needed to check.

Every test here pins a failure mode that is SILENT in production: the run
completes, the numbers look ordinary, and nothing downstream can tell that the
measurement is not the one the spec asked for.  That is the whole reason these
are assertions rather than comments.

The ipSAE test is a REGRESSION on a real complex, not a synthetic fixture.  The
campaign gate (peptide ipSAE >= 0.66) is calibrated on that scorer's scale, so a
scorer that drifts silently re-scales every gate in every spec.
"""

from __future__ import annotations

import os

import pytest

from ligandai.campaign.local_platform import (
    IPSAE_REFERENCE, MSARegistry, PocketSpec, POCKET_BLIND_CHECKPOINTS,
    LocalLigandForge, read_a3m_query,
)

# The EGFR domain III construct and its alignment, both on this host.
DOMIII_A3M = "/home/dre/egfr_ph_campaign/fold/EGFR_domIII.a3m"
DOMIII_FASTA = ("/home/dre/egfr_ph_campaign/spec/constructs/"
                "EGFR_domIII_334_538.fasta")
SIX_ARU = "/home/dre/egfr_ph_campaign/target/6ARU.pdb"
ONE_N8Z = "/home/dre/egfr_ph_campaign/gonogo/structures/1N8Z.pdb"


def _fasta(path: str) -> str:
    return "".join(l.strip() for l in open(path) if not l.startswith(">"))


needs_domiii = pytest.mark.skipif(
    not (os.path.exists(DOMIII_A3M) and os.path.exists(DOMIII_FASTA)),
    reason="EGFR domIII construct/alignment not present on this host")
needs_6aru = pytest.mark.skipif(not os.path.exists(SIX_ARU),
                                reason="6ARU not present on this host")
needs_1n8z = pytest.mark.skipif(not os.path.exists(ONE_N8Z),
                                reason="1N8Z not present on this host")


# -- the scorer the gates are calibrated on --------------------------------

@pytest.mark.skipif(not os.path.exists(IPSAE_REFERENCE["pdb"]),
                    reason="8C2E reference pose not present on this host")
def test_ipsae_reproduces_the_validated_value_on_a_real_complex():
    """8C2E A-B must still be 0.6934.

    If this moves, the 0.66 gate no longer means what every spec that declares
    it believes it means.  Nothing else in the package can detect that.
    """
    from ligandai.campaign.local_platform import IPSAEScorer
    got = IPSAEScorer().selftest()
    assert got["ran"] is True
    assert got["observed"] == pytest.approx(IPSAE_REFERENCE["expected"],
                                            abs=IPSAE_REFERENCE["tolerance"])


# -- an MSA is applied POSITIONALLY ----------------------------------------

@needs_domiii
def test_msa_registers_when_the_query_row_is_the_construct():
    reg = MSARegistry()
    entry = reg.register("EGFR_domIII_334_538", _fasta(DOMIII_FASTA), DOMIII_A3M)
    assert len(entry.query) == len(_fasta(DOMIII_FASTA))
    assert reg.get(_fasta(DOMIII_FASTA)) is entry


@needs_domiii
def test_msa_refuses_a_construct_it_does_not_align():
    """A one-residue difference shifts every column and still folds fine.

    The fold succeeds, the metrics are produced, and they are meaningless. The
    refusal has to happen at registration because nothing later can see it.
    """
    seq = _fasta(DOMIII_FASTA)
    mutated = ("A" if seq[0] != "A" else "C") + seq[1:]
    with pytest.raises(ValueError, match="POSITIONALLY"):
        MSARegistry().register("EGFR_domIII_334_538", mutated, DOMIII_A3M)


@needs_domiii
def test_msa_lookup_is_by_sequence_not_by_name():
    """Keying on the name would let a renamed construct collect the wrong a3m."""
    reg = MSARegistry()
    reg.register("whatever_you_call_it", _fasta(DOMIII_FASTA), DOMIII_A3M)
    assert reg.get(_fasta(DOMIII_FASTA)) is not None
    assert reg.get("MKVLLAA") is None


@needs_domiii
def test_a3m_query_strips_insertions_and_gaps():
    query, rows = read_a3m_query(DOMIII_A3M)
    assert rows > 1
    assert query == query.upper()
    assert "-" not in query


# -- a pocket is residues in a STATED FRAME --------------------------------

@needs_6aru
def test_pocket_verifies_the_residues_it_was_promised():
    """EGFR Patch A in 6ARU author numbering is DEHEDE."""
    p = PocketSpec(pdb_path=SIX_ARU,
                   residue_numbers=[392, 397, 483, 495, 498, 510],
                   frame="6ARU author (UniProt P00533 - 24)",
                   chain_id="A", expect_residues="DEHEDE")
    assert p.verify()["residues_found"] == "DEHEDE"


@needs_6aru
def test_pocket_refuses_uniprot_numbers_read_in_the_author_frame():
    """The exact mistake this campaign can make: forget the -24 offset.

    The numbers all EXIST in the structure, so nothing raises on lookup — they
    simply resolve to different residues, on a different face, and generation is
    steered there with every downstream number looking normal.
    """
    with pytest.raises(ValueError, match="WRONG RESIDUES"):
        PocketSpec(pdb_path=SIX_ARU,
                   residue_numbers=[416, 421, 507, 519, 522, 534],  # UniProt
                   frame="UniProt P00533 (WRONG for this structure)",
                   chain_id="A", expect_residues="DEHEDE").verify()


@needs_6aru
def test_pocket_refuses_residues_absent_from_the_chain():
    with pytest.raises(ValueError, match="NUMBERING FRAME ERROR"):
        PocketSpec(pdb_path=SIX_ARU, residue_numbers=[99991, 99992],
                   frame="nonsense", chain_id="A").verify()


@needs_1n8z
def test_erbb2_cfg_negative_resolves_to_the_equivalent_face():
    """ERBB2's version of Patch A is DQHGQE — two carboxylates, not five.

    That difference is the campaign's selectivity handle, so it is worth an
    assertion: if this mapping ever silently changes, the CFG negative stops
    being the paralog's form of the same epitope.
    """
    p = PocketSpec(pdb_path=ONE_N8Z,
                   residue_numbers=[399, 404, 490, 502, 505, 517],
                   frame="1N8Z chain C author (UniProt P04626 - 22)",
                   chain_id="C", expect_residues="DQHGQE")
    assert p.verify()["residues_found"] == "DQHGQE"


# -- checkpoints -----------------------------------------------------------

@pytest.mark.parametrize("ckpt", sorted(POCKET_BLIND_CHECKPOINTS))
def test_pocket_blind_checkpoints_are_refused_at_construction(ckpt):
    """Refused before anything loads, so it cannot be reached by accident."""
    with pytest.raises(ValueError, match="POCKET-BLIND"):
        LocalLigandForge(model_id=ckpt)


def test_pocket_blind_refusal_survives_a_full_path():
    with pytest.raises(ValueError, match="POCKET-BLIND"):
        LocalLigandForge(model_id="/checkpoints/ligandgen_v6.5_final.pt")


def test_a_responsive_checkpoint_is_not_refused():
    """The guard must reject the blind one WITHOUT rejecting the good ones."""
    assert LocalLigandForge(model_id="v6.9.21").model_id == "v6.9.21"
    assert LocalLigandForge(model_id="v6.5.21").model_id == "v6.5.21"


# -- the platform is still a Platform --------------------------------------

def test_local_platform_satisfies_the_platform_protocol():
    from ligandai.campaign.local_platform import LocalPlatform
    for method in ("generate", "fold", "score_developability", "score_affinity"):
        assert callable(getattr(LocalPlatform, method)), method
    assert LocalPlatform.name == "local"


def test_local_platform_is_exported_lazily_from_the_package():
    """Importing the package must not require numpy or the local runtime."""
    import ligandai.campaign as c
    assert "LocalPlatform" in c.__all__
    assert c.LocalPlatform.name == "local"
    with pytest.raises(AttributeError):
        c.NoSuchPlatform
