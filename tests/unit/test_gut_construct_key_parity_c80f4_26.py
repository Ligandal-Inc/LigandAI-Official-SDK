# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""The SDK's local and remote peptide paths must agree about a CONSTRUCT.  [bd-dre-c80f4.26]

Three silent drops, all found by scoring one macrocycle two ways and comparing
against the platform bridge, and all in the same family: a declared construct
property was accepted without error and then not used.

1. ``score_local({"cyclic_mode": "lactam"})`` resolved to NO modifications at
   all. ``_mods_for`` decided "is this a record or a ``{sequence: record}`` map?"
   from a key tuple that omitted the STRUCTURAL keys, so a construct-only record
   matched no sequence and silently became empty. rescue 0.0 against the
   resolver's 0.36 — and 0.72 is the ``balanced_gi_candidate`` threshold.

2. On the wire, ``_chemistry_both_spellings`` rebuilt the record from the
   chemistry alone, discarding the structural keys. ``{"cyclic_mode": "lactam",
   "declared": {"n_methyl_count": 3}}`` went out as ``{"n_methyl_count": 3,
   ...}`` with the cyclization GONE. A construct-ONLY record survived only by
   accident, through the wrong branch, because its values were not mappings.

3. Structural keys reaching the in-SDK port were ignored rather than resolved.
   They now go through ``resolve_construct_modifications`` or raise.

The assertions below therefore always check BOTH that the two paths agree AND
that the declared chemistry actually MOVES the number. Agreement alone is
satisfied by two equally-ignored inputs, which is how this survived.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
from typing import Any

import pytest

from ligandai.resources import gut_stability as G
from ligandai.resources.gut_stability import (
    PLATFORM_MODULE_PATH_ENV,
    LigandAIPlatformModuleUnavailable,
)

PLATFORM_MODULES = pathlib.Path("/mnt/backup/LIGANDAI_ALPHA_V2/modal_workers")
BRIDGE = PLATFORM_MODULES / "gut_scoring_dispatch_c80f4.py"

#: Terminal Cys pair, zero interior Cys — realisable under the recombinant rule.
CYCLIC_SEQ = "CYIQNAPLGC"

pytestmark = pytest.mark.skipif(
    not BRIDGE.is_file(),
    reason="platform checkout absent; this suite compares against the real bridge",
)


@pytest.fixture
def platform_modules(monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    monkeypatch.setenv(PLATFORM_MODULE_PATH_ENV, str(PLATFORM_MODULES))
    return PLATFORM_MODULES


def bridge_rescue(sequence: str, modifications: Any) -> float:
    """``modification_rescue`` straight from the platform bridge."""
    proc = subprocess.run(
        [sys.executable, BRIDGE.name],
        input=json.dumps(
            {
                "op": "gut_stability",
                "payload": {"sequences": [sequence], "modifications": modifications},
            }
        ),
        capture_output=True,
        text=True,
        cwd=str(PLATFORM_MODULES),
        timeout=120,
    )
    payload = json.loads(proc.stdout)
    assert payload["ok"] is True, payload
    return payload["result"]["results"][0]["modification_rescue"]


def local_rescue(sequence: str, modifications: Any) -> float:
    return G._score_batch_local([sequence], modifications)["results"][0][
        "modification_rescue"
    ]


# Every shape a caller might plausibly write for one N-methylated macrocycle.
CONSTRUCT_SHAPES: dict[str, dict[str, Any]] = {
    "structural only: cyclic_mode": {"cyclic_mode": "lactam"},
    "structural only: modality": {"modality": "cyclic_lactam"},
    "structural + nested chemistry": {
        "cyclic_mode": "lactam",
        "declared": {"n_methyl_count": 3},
    },
    "structural + flat chemistry": {"cyclic_mode": "lactam", "n_methyl_count": 3},
    "pre-resolved flat chemistry": {
        "cyclized": True,
        "n_terminal_protected": True,
        "c_terminal_protected": True,
    },
    "disulfide route": {"cyclic_mode": "disulfide"},
}


@pytest.mark.parametrize("shape", list(CONSTRUCT_SHAPES), ids=list(CONSTRUCT_SHAPES))
def test_local_and_bridge_agree_on_every_construct_shape(
    shape: str, platform_modules: pathlib.Path
) -> None:
    mods = CONSTRUCT_SHAPES[shape]
    assert local_rescue(CYCLIC_SEQ, mods) == bridge_rescue(CYCLIC_SEQ, mods)


def test_a_structural_key_actually_moves_the_number(
    platform_modules: pathlib.Path,
) -> None:
    """Agreement is not enough: two ignored inputs also agree."""
    bare = local_rescue(CYCLIC_SEQ, None)
    lactam = local_rescue(CYCLIC_SEQ, {"cyclic_mode": "lactam"})
    assert bare == 0.0
    assert lactam == pytest.approx(0.36)
    assert lactam > bare


def test_nested_chemistry_no_longer_evicts_the_cyclization(
    platform_modules: pathlib.Path,
) -> None:
    """⛔ THE WIRE REGRESSION. Adding chemistry used to delete the structure."""
    both = {"cyclic_mode": "lactam", "declared": {"n_methyl_count": 3}}
    structure_only = local_rescue(CYCLIC_SEQ, {"cyclic_mode": "lactam"})
    assert local_rescue(CYCLIC_SEQ, both) > structure_only


@pytest.mark.parametrize("shape", list(CONSTRUCT_SHAPES), ids=list(CONSTRUCT_SHAPES))
def test_the_wire_body_never_drops_a_structural_key(shape: str) -> None:
    mods = CONSTRUCT_SHAPES[shape]
    wire = G._score_body([CYCLIC_SEQ], mods)["modifications"]
    for key in G._CONSTRUCT_STRUCTURAL_KEYS:
        if key in mods:
            assert wire.get(key) == mods[key], f"{key} lost on the wire"


def test_a_construct_only_record_is_a_record_not_a_sequence_map() -> None:
    """The container discrimination that caused drop #1."""
    assert G._raw_record_for(0, CYCLIC_SEQ, {"cyclic_mode": "lactam"}) == {
        "cyclic_mode": "lactam"
    }
    # A genuine sequence-keyed map still resolves per sequence.
    keyed = {CYCLIC_SEQ: {"cyclic_mode": "lactam"}, "AAAA": None}
    assert G._raw_record_for(0, CYCLIC_SEQ, keyed) == {"cyclic_mode": "lactam"}


def test_structural_keys_without_the_platform_module_raise_rather_than_score_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⛔ The invariant: an unavailable resolver must not become rescue 0.0.

    A structural key the SDK cannot resolve is an unanswered question. Scoring it
    as an unmodified linear peptide would be a confident wrong answer, and a
    pessimistic one — the caller would discard a macrocycle that scores well.
    """
    # Monkeypatched rather than merely unsetting the env var: a sibling test that
    # imports the platform modules leaves their directory on sys.path for the rest
    # of the session, so "the module is absent" is not reproducible by environment
    # alone. That leak is the same class as bd-dre-c80f4.10 / .13.
    monkeypatch.delenv(PLATFORM_MODULE_PATH_ENV, raising=False)

    def _unavailable(name: str, **kwargs: Any) -> Any:
        raise LigandAIPlatformModuleUnavailable(
            "platform modules absent",
            module=name,
            needed_by=kwargs.get("needed_by", "test"),
        )

    monkeypatch.setattr(G, "_import_platform_module", _unavailable)
    with pytest.raises(LigandAIPlatformModuleUnavailable):
        G._score_batch_local([CYCLIC_SEQ], {"cyclic_mode": "lactam"})


def test_pre_resolved_chemistry_still_scores_fully_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The offline promise is kept for the documented flat spelling: no platform
    module, no network, still a number."""
    monkeypatch.delenv(PLATFORM_MODULE_PATH_ENV, raising=False)
    monkeypatch.setattr(
        G,
        "_import_platform_module",
        lambda *a, **k: pytest.fail("the flat path must not need the platform module"),
    )
    out = G._score_batch_local([CYCLIC_SEQ], {"cyclized": True})
    assert out["results"][0]["modification_rescue"] == pytest.approx(0.18)


def test_unknown_cyclization_is_distinguishable_from_declared_linear(
    platform_modules: pathlib.Path,
) -> None:
    """Both score the cyclized term at 0.0. Only the resolution record says which
    happened, so the two must not be indistinguishable in the output."""
    linear = G._score_batch_local(
        [CYCLIC_SEQ], {"cyclic_mode": "none", "modality": "linear_l"}
    )["results"][0]
    unknown = G._score_batch_local([CYCLIC_SEQ], {"strict_recombinant": True})[
        "results"
    ][0]

    assert linear["modification_rescue"] == unknown["modification_rescue"] == 0.0
    assert linear["modification_resolution"]["cyclized_state"] is False
    assert unknown["modification_resolution"]["cyclized_state"] is None
    assert any(
        "cyclized" in term
        for term in unknown["modification_resolution"]["rescue_terms_omitted"]
    )


def test_the_resolution_record_is_absent_when_nothing_structural_was_sent(
    platform_modules: pathlib.Path,
) -> None:
    """No structural key means no resolution to report — the key is omitted
    rather than present-and-empty."""
    rec = G._score_batch_local([CYCLIC_SEQ], {"cyclized": True})["results"][0]
    assert "modification_resolution" not in rec


def test_a_disulfide_route_is_refused_when_the_peptide_cannot_carry_it(
    platform_modules: pathlib.Path,
) -> None:
    """Cyclicity comes from the REALISED peptide: interior Cys under recombinant
    expression defeats the terminal bridge, so the +0.18 cannot be had by asking."""
    interior_cys = "CYIQNCPLGC"
    rec = G._score_batch_local([interior_cys], {"cyclic_mode": "disulfide"})["results"][0]
    assert rec["modification_resolution"]["cyclized_state"] is False
    assert rec["modification_rescue"] == 0.0
    assert local_rescue(interior_cys, {"cyclic_mode": "disulfide"}) == bridge_rescue(
        interior_cys, {"cyclic_mode": "disulfide"}
    )
