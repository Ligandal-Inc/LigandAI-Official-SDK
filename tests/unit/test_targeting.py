# Copyright © 2025 Ligandal, Inc. All rights reserved.
"""Unit tests for ``ligandai._targeting`` — client-side PDB-resnum validation.

[bd-LIGANDAI_ALPHA_V2-x4vt8]

Covers the hard-error rules and exact message strings from the x4vt8
contract §4/§6: resnum-vs-index rejection, malformed region shapes, missing
chain (warn, not error), avoid_shell_angstrom bounds, and the region span
sanity limit. Python and Node SDKs are required to raise byte-identical
messages for the same input (contract §6) — do not change a message string
here without making the matching change in the Node SDK.
"""

from __future__ import annotations

import warnings

import pytest

from ligandai._targeting import (
    normalize_region,
    validate_and_normalize_targeting,
    validate_avoid_shell_angstrom,
    validate_hotspot_residues,
    validate_region_list,
)
from ligandai.errors import LigandAITargetingWarning, LigandAIValidationError
from ligandai.types import ResidueRange

# ---------------------------------------------------------------------------
# resnum-vs-index rejection — the whole point of bd-LIGANDAI_ALPHA_V2-x4vt8
# ---------------------------------------------------------------------------


def test_start_zero_is_rejected_with_exact_message() -> None:
    with pytest.raises(LigandAIValidationError) as exc:
        normalize_region({"chain": "A", "start": 0, "end": 10}, field="target_residues", index=0)
    assert str(exc.value) == (
        "target_residues[0]: residue numbers are PDB numbering and start at "
        "1; got start=0. If you meant an array index, that is not supported "
        "— pass the residue number shown in the structure."
    )


def test_start_negative_is_rejected() -> None:
    with pytest.raises(LigandAIValidationError, match=r"got start=-5"):
        normalize_region({"chain": "A", "start": -5, "end": 10}, field="avoid_residues", index=2)


def test_end_zero_is_rejected() -> None:
    with pytest.raises(LigandAIValidationError, match=r"got start=0"):
        normalize_region({"chain": "A", "start": 5, "end": 0}, field="target_residues", index=0)


def test_bare_int_zero_is_rejected() -> None:
    """A bare int (single-residue shorthand) of 0 is the same array-index mistake."""
    with pytest.raises(LigandAIValidationError, match=r"residue numbers are PDB numbering"):
        normalize_region(0, field="target_residues", index=0)


def test_valid_pdb_resnum_start_one_is_accepted() -> None:
    """start=1 is the minimum LEGAL PDB residue number — must not raise."""
    out = normalize_region({"chain": "A", "start": 1, "end": 5}, field="target_residues", index=0)
    assert out == {"chain": "A", "start": 1, "end": 5}


def test_hotspot_residues_zero_rejected_with_exact_message() -> None:
    with pytest.raises(LigandAIValidationError) as exc:
        validate_hotspot_residues([45, 0, 47])
    assert str(exc.value) == (
        "hotspot_residues[1]: residue numbers are PDB numbering and start at "
        "1; got start=0. If you meant an array index, that is not supported "
        "— pass the residue number shown in the structure."
    )


def test_hotspot_residues_valid_list_passes_through() -> None:
    assert validate_hotspot_residues([792, 855]) == [792, 855]


def test_hotspot_residues_none_passthrough() -> None:
    assert validate_hotspot_residues(None) is None


def test_hotspot_residues_accepts_residue_dict() -> None:
    """A {"chain","residue"} dict is accepted; only the resnum is kept."""
    assert validate_hotspot_residues([{"chain": "A", "residue": 792}]) == [792]


def test_hotspot_residues_single_bare_int_wrapped() -> None:
    assert validate_hotspot_residues(792) == [792]


# ---------------------------------------------------------------------------
# start/end must be integers
# ---------------------------------------------------------------------------


def test_float_with_fraction_rejected() -> None:
    with pytest.raises(LigandAIValidationError, match=r"must be integers"):
        normalize_region({"chain": "A", "start": 1.5, "end": 10}, field="target_residues", index=0)


def test_nan_rejected() -> None:
    with pytest.raises(LigandAIValidationError, match=r"must be integers"):
        normalize_region({"chain": "A", "start": float("nan"), "end": 10}, field="target_residues", index=0)


def test_string_value_rejected() -> None:
    with pytest.raises(LigandAIValidationError, match=r"must be integers"):
        normalize_region({"chain": "A", "start": "36", "end": 46}, field="avoid_residues", index=0)


def test_bool_value_rejected() -> None:
    """bool is an int subclass in Python — must be explicitly excluded."""
    with pytest.raises(LigandAIValidationError, match=r"must be integers"):
        normalize_region({"chain": "A", "start": True, "end": 10}, field="target_residues", index=0)


def test_integer_valued_float_is_accepted() -> None:
    """36.0 is integer-valued — not the same as 36.5 — should be accepted."""
    out = normalize_region({"chain": "A", "start": 36.0, "end": 46.0}, field="target_residues", index=0)
    assert out == {"chain": "A", "start": 36, "end": 46}


# ---------------------------------------------------------------------------
# malformed region shapes
# ---------------------------------------------------------------------------


def test_unrecognized_shape_rejected() -> None:
    with pytest.raises(LigandAIValidationError) as exc:
        normalize_region({"foo": "bar"}, field="target_residues", index=3)
    assert str(exc.value) == (
        "target_residues[3]: must be an object with start/end (or "
        "startResidue/endResidue), a single residue, or an integer residue "
        "number; got dict."
    )


def test_list_as_region_rejected() -> None:
    with pytest.raises(LigandAIValidationError, match=r"got list"):
        normalize_region([1, 2, 3], field="target_residues", index=0)


def test_string_as_region_rejected() -> None:
    with pytest.raises(LigandAIValidationError, match=r"got str"):
        normalize_region("A:36-46", field="target_residues", index=0)


def test_none_as_region_rejected() -> None:
    with pytest.raises(LigandAIValidationError, match=r"got NoneType"):
        normalize_region(None, field="target_residues", index=0)


def test_start_residue_alias_accepted() -> None:
    out = normalize_region(
        {"chain": "A", "startResidue": 36, "endResidue": 46}, field="avoid_residues", index=0
    )
    assert out == {"chain": "A", "start": 36, "end": 46}


def test_residue_single_key_accepted() -> None:
    out = normalize_region({"chain": "A", "residue": 792}, field="target_residues", index=0)
    assert out == {"chain": "A", "start": 792, "end": 792}


def test_resnum_single_key_accepted() -> None:
    out = normalize_region({"chain": "A", "resnum": 792}, field="target_residues", index=0)
    assert out == {"chain": "A", "start": 792, "end": 792}


def test_residue_range_pydantic_model_accepted() -> None:
    """ResidueRange (pydantic) is duck-typed via model_dump(), no import cycle."""
    out = normalize_region(ResidueRange(chain="A", start=36, end=46), field="target_residues", index=0)
    assert out == {"chain": "A", "start": 36, "end": 46}


# ---------------------------------------------------------------------------
# chain validation
# ---------------------------------------------------------------------------


def test_chain_too_long_rejected() -> None:
    with pytest.raises(LigandAIValidationError) as exc:
        normalize_region({"chain": "ABC", "start": 1, "end": 5}, field="target_residues", index=0)
    assert str(exc.value) == "target_residues[0]: chain must be a 1-2 character identifier; got 'ABC'."


def test_chain_non_alnum_rejected() -> None:
    with pytest.raises(LigandAIValidationError, match=r"chain must be"):
        normalize_region({"chain": "A-", "start": 1, "end": 5}, field="target_residues", index=0)


def test_chain_empty_string_treated_as_missing() -> None:
    """Mirrors residue_targeting.normalize_regions(): "" means no chain."""
    out = normalize_region({"chain": "", "start": 1, "end": 5}, field="target_residues", index=0)
    assert out == {"start": 1, "end": 5}
    assert "chain" not in out


def test_chain_none_omitted_from_output() -> None:
    out = normalize_region({"start": 1, "end": 5}, field="target_residues", index=0)
    assert out == {"start": 1, "end": 5}


def test_two_char_chain_accepted() -> None:
    out = normalize_region({"chain": "AA", "start": 1, "end": 5}, field="target_residues", index=0)
    assert out["chain"] == "AA"


# ---------------------------------------------------------------------------
# start > end — auto-swap + MANDATORY warning
# ---------------------------------------------------------------------------


def test_start_greater_than_end_auto_swaps_with_warning() -> None:
    with pytest.warns(LigandAITargetingWarning, match=r"swapped automatically"):
        out = normalize_region({"chain": "A", "start": 46, "end": 36}, field="target_residues", index=0)
    assert out == {"chain": "A", "start": 36, "end": 46}


def test_start_equals_end_no_warning() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error", LigandAITargetingWarning)
        out = normalize_region({"chain": "A", "start": 5, "end": 5}, field="target_residues", index=0)
    assert out == {"chain": "A", "start": 5, "end": 5}


# ---------------------------------------------------------------------------
# region span limit
# ---------------------------------------------------------------------------


def test_region_span_over_limit_rejected() -> None:
    with pytest.raises(LigandAIValidationError) as exc:
        normalize_region({"chain": "A", "start": 1, "end": 6000}, field="target_residues", index=0)
    assert str(exc.value) == (
        "target_residues[0]: region spans 6000 residues, which exceeds the "
        "5000-residue sanity limit."
    )


def test_region_span_at_limit_accepted() -> None:
    # 5000 residues inclusive: start=1, end=5000 -> span=5000, not > 5000.
    out = normalize_region({"chain": "A", "start": 1, "end": 5000}, field="target_residues", index=0)
    assert out["end"] - out["start"] + 1 == 5000


def test_region_span_one_over_limit_rejected() -> None:
    with pytest.raises(LigandAIValidationError, match=r"5001 residues"):
        normalize_region({"chain": "A", "start": 1, "end": 5001}, field="target_residues", index=0)


def test_custom_max_span_honoured() -> None:
    with pytest.raises(LigandAIValidationError, match=r"exceeds the 10-residue"):
        normalize_region({"start": 1, "end": 20}, field="target_residues", index=0, max_span=10)


# ---------------------------------------------------------------------------
# avoid_shell_angstrom bounds
# ---------------------------------------------------------------------------


def test_avoid_shell_angstrom_none_passthrough() -> None:
    assert validate_avoid_shell_angstrom(None) is None


def test_avoid_shell_angstrom_negative_rejected() -> None:
    with pytest.raises(LigandAIValidationError) as exc:
        validate_avoid_shell_angstrom(-1)
    assert str(exc.value) == "avoid_shell_angstrom must be between 0 and 50 Angstrom; got -1."


def test_avoid_shell_angstrom_over_max_rejected() -> None:
    with pytest.raises(LigandAIValidationError) as exc:
        validate_avoid_shell_angstrom(51)
    assert str(exc.value) == "avoid_shell_angstrom must be between 0 and 50 Angstrom; got 51."


def test_avoid_shell_angstrom_non_numeric_rejected() -> None:
    with pytest.raises(LigandAIValidationError, match=r"must be between 0 and 50"):
        validate_avoid_shell_angstrom("far")


def test_avoid_shell_angstrom_bool_rejected() -> None:
    with pytest.raises(LigandAIValidationError, match=r"must be between 0 and 50"):
        validate_avoid_shell_angstrom(True)


def test_avoid_shell_angstrom_zero_is_legal_but_warns() -> None:
    with pytest.warns(LigandAITargetingWarning, match=r"disables the packing shell"):
        out = validate_avoid_shell_angstrom(0)
    assert out == 0.0


def test_avoid_shell_angstrom_fifty_is_legal_boundary() -> None:
    assert validate_avoid_shell_angstrom(50) == 50.0


def test_avoid_shell_angstrom_default_no_warning() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error", LigandAITargetingWarning)
        out = validate_avoid_shell_angstrom(8.0)
    assert out == 8.0


# ---------------------------------------------------------------------------
# missing chain in a multi-chain request — WARN, not a hard error
# ---------------------------------------------------------------------------


def test_missing_chain_warns_with_two_distinct_chains_named() -> None:
    with pytest.warns(LigandAITargetingWarning, match=r"no chain given"):
        validate_and_normalize_targeting(
            target_residues=[
                {"chain": "A", "start": 10, "end": 20},
                {"chain": "B", "start": 10, "end": 20},
                {"start": 5, "end": 8},  # ambiguous: which chain?
            ],
        )


def test_missing_chain_single_chain_context_no_warning() -> None:
    """Only one chain named anywhere -> no ambiguity -> no warning.

    avoid span (6) is kept well under target span (91) so this doesn't also
    trip the separate degenerate-pocket-risk warning.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("error", LigandAITargetingWarning)
        validate_and_normalize_targeting(
            target_residues=[{"chain": "A", "start": 10, "end": 100}],
            avoid_residues=[{"chain": "A", "start": 30, "end": 35}],
        )


def test_no_chain_at_all_no_warning() -> None:
    """Nobody named a chain -> nothing is ambiguous relative to anything else.

    avoid span (6) is kept well under target span (91) so this doesn't also
    trip the separate degenerate-pocket-risk warning.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("error", LigandAITargetingWarning)
        validate_and_normalize_targeting(
            target_residues=[{"start": 10, "end": 100}],
            avoid_residues=[{"start": 30, "end": 35}],
        )


# ---------------------------------------------------------------------------
# degenerate-pocket-risk warning: avoid span >= target span
# ---------------------------------------------------------------------------


def test_degenerate_pocket_risk_warns() -> None:
    with pytest.warns(LigandAITargetingWarning, match=r"degenerate"):
        validate_and_normalize_targeting(
            target_residues=[{"chain": "A", "start": 1, "end": 10}],  # span 10
            avoid_residues=[{"chain": "A", "start": 1, "end": 10}],  # span 10 >= 10
        )


def test_small_avoid_relative_to_target_no_warning() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error", LigandAITargetingWarning)
        validate_and_normalize_targeting(
            target_residues=[{"chain": "A", "start": 1, "end": 100}],
            avoid_residues=[{"chain": "A", "start": 40, "end": 46}],
        )


# ---------------------------------------------------------------------------
# validate_region_list — bare-value wrapping, None passthrough
# ---------------------------------------------------------------------------


def test_validate_region_list_none() -> None:
    assert validate_region_list(None, field="target_residues") is None


def test_validate_region_list_bare_dict_wrapped() -> None:
    out = validate_region_list({"chain": "A", "start": 1, "end": 5}, field="target_residues")
    assert out == [{"chain": "A", "start": 1, "end": 5}]


def test_validate_region_list_bare_int_wrapped() -> None:
    out = validate_region_list(792, field="target_residues")
    assert out == [{"start": 792, "end": 792}]


def test_validate_region_list_mixed_shapes() -> None:
    out = validate_region_list(
        [
            {"chain": "A", "start": 1, "end": 5},
            ResidueRange(chain="B", start=10, end=20),
            792,
        ],
        field="target_residues",
    )
    assert out == [
        {"chain": "A", "start": 1, "end": 5},
        {"chain": "B", "start": 10, "end": 20},
        {"start": 792, "end": 792},
    ]


def test_validate_region_list_error_includes_correct_index() -> None:
    with pytest.raises(LigandAIValidationError, match=r"target_residues\[2\]"):
        validate_region_list(
            [
                {"chain": "A", "start": 1, "end": 5},
                {"chain": "A", "start": 10, "end": 20},
                {"chain": "A", "start": 0, "end": 5},
            ],
            field="target_residues",
        )


# ---------------------------------------------------------------------------
# validate_and_normalize_targeting — full integration of all four fields
# ---------------------------------------------------------------------------


def test_validate_and_normalize_targeting_all_none() -> None:
    out = validate_and_normalize_targeting()
    assert out == {
        "target_residues": None,
        "avoid_residues": None,
        "hotspot_residues": None,
        "avoid_shell_angstrom": None,
    }


def test_validate_and_normalize_targeting_full() -> None:
    out = validate_and_normalize_targeting(
        target_residues=[{"chain": "A", "start": 1, "end": 100}],
        avoid_residues=[{"chain": "A", "start": 36, "end": 46}],
        hotspot_residues=[45, 47],
        avoid_shell_angstrom=6.0,
    )
    assert out["target_residues"] == [{"chain": "A", "start": 1, "end": 100}]
    assert out["avoid_residues"] == [{"chain": "A", "start": 36, "end": 46}]
    assert out["hotspot_residues"] == [45, 47]
    assert out["avoid_shell_angstrom"] == 6.0


def test_validate_and_normalize_targeting_raises_on_any_bad_field() -> None:
    with pytest.raises(LigandAIValidationError):
        validate_and_normalize_targeting(hotspot_residues=[45, -1])
