"""Input and numerical contracts; real-checkpoint execution is separate."""

from contextlib import contextmanager
from types import SimpleNamespace

import numpy as np
import pytest

from ligandai.opendde import (
    LocalOpenDDETrunk,
    _block_stats,
    _disable_coordinates,
    _msa_query,
    _precision_context,
    _sequence,
)


@pytest.mark.parametrize("sequence", ["", "AC D", "acd", "ACX", "AC/../D"])
def test_invalid_sequences_are_not_silently_changed(sequence):
    with pytest.raises(ValueError):
        _sequence(sequence)


def test_a3m_wrapped_query_and_insertions_are_preserved():
    assert _msa_query("#comment\n>query\nACD\nEFG\n>hit\nACd-EFG\n") == "ACDEFG"


def test_both_cross_chain_blocks_and_thresholds():
    matrix = np.array([[0.0, 0.9, 0.6], [0.9, 1.0, 0.1], [0.6, 0.1, 1.0]])
    actual = _block_stats(matrix, [7, 3, 3], 1)
    assert actual["n_contacts_p80"] == 2
    assert actual["n_contacts_p50"] == 4
    assert actual["contact_prob_sum"] == pytest.approx(3.0)
    assert actual["contact_prob_mean"] == pytest.approx(0.75)


@pytest.mark.parametrize(
    "matrix,asym,n",
    [
        (np.zeros((2, 3)), [0, 1], 1),
        (np.full((3, 3), np.nan), [0, 1, 1], 1),
        (np.full((3, 3), 1.1), [0, 1, 1], 1),
        (np.zeros((2, 2)), [0, 1], 1),
        (np.zeros((3, 3)), [0, 0, 0], 1),
    ],
)
def test_invalid_or_ambiguous_contact_arrays_fail(matrix, asym, n):
    with pytest.raises(RuntimeError):
        _block_stats(matrix, asym, n)


def test_a_single_measurement_is_not_warm_throughput():
    scorer = object.__new__(LocalOpenDDETrunk)
    scorer.load_s = 10.0
    scorer.timings = [{"total_s": 4.0}]
    assert scorer.cost_summary()["warm_median_total_s"] is None
    scorer.timings.append({"total_s": 2.0})
    assert scorer.cost_summary()["warm_median_total_s"] == 2.0


def test_msa_mismatch_fails_before_model_load(tmp_path):
    msa = tmp_path / "query.a3m"
    msa.write_text(">query\nACDF\n")
    with pytest.raises(ValueError, match="MSA query"):
        LocalOpenDDETrunk(
            [("A", "ACDE", str(msa))],
            opendde_source="missing",
            runtime_root="missing",
            checkpoint="missing",
        )


def test_cpu_bf16_fails_instead_of_silently_using_fp32():
    with pytest.raises(ValueError, match="explicitly selected CUDA"):
        LocalOpenDDETrunk(
            [], opendde_source="missing", runtime_root="missing", checkpoint="missing", dtype="bf16"
        )


@pytest.mark.parametrize("device", ["auto", "mps", "cuda:1"])
def test_device_selection_never_silently_selects_a_gpu(device):
    with pytest.raises(ValueError, match="CUDA_VISIBLE_DEVICES"):
        LocalOpenDDETrunk(
            [],
            opendde_source="missing",
            runtime_root="missing",
            checkpoint="missing",
            device=device,
        )


def test_bf16_really_enters_autocast_and_restores_it():
    # Control-flow test only: no CUDA call or speed/accuracy evidence.
    calls = []

    @contextmanager
    def autocast(**kwargs):
        calls.append(("enter", kwargs))
        yield
        calls.append(("exit", kwargs))

    torch = SimpleNamespace(autocast=autocast, bfloat16="bf16")
    with _precision_context(torch, SimpleNamespace(type="cuda"), "bf16"):
        assert calls == [("enter", {"device_type": "cuda", "dtype": "bf16"})]
    assert calls[-1][0] == "exit"
    with _precision_context(torch, SimpleNamespace(type="cpu"), "fp32"):
        assert len(calls) == 2
    with pytest.raises(ValueError, match="explicitly selected CUDA"):
        _precision_context(torch, SimpleNamespace(type="cpu"), "bf16")


def test_structure_free_reexecutes_trunk_and_forbids_coordinates():
    def unreachable(*args, **kwargs):
        raise AssertionError("Original cache or coordinate stage reached")

    calls = []

    def trunk(**kwargs):
        calls.append(kwargs)
        return "real-trunk-path"

    names = (
        "run_sample_diffusion_stage",
        "run_confidence_head_stage",
        "run_post_confidence_outputs_stage",
        "prepare_diffusion_cache_for_sampling",
        "sample_diffusion",
        "run_confidence_head",
        "_trunk_with_cache",
    )
    model = SimpleNamespace(**dict.fromkeys(names, unreachable), get_pairformer_output=trunk)
    _disable_coordinates(model)
    for _ in range(2):
        assert model._trunk_with_cache(N_cycle=10) == "real-trunk-path"
    assert len(calls) == 2
    pred = {}
    model.run_sample_diffusion_stage(pred_dict=pred)
    assert pred["coordinate"] is None
    with pytest.raises(RuntimeError, match="primitive reached"):
        model.sample_diffusion()
