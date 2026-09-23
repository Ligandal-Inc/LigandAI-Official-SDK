# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""[bd-LIGANDAI_ALPHA_V2-jb73b] `replicate()` takes **panel_kwargs while
`run_round()` takes panel_kwargs={...}. A caller following the other method's
convention had the dict forwarded as a keyword literally named `panel_kwargs`,
through fold_panel's **fold_kwargs into fold_batch, dying as

    TypeError: Peptides.fold_batch() got an unexpected keyword argument 'panel_kwargs'

with nothing naming the real mistake. Caught by running the refinement surface
end to end post-deploy, not by reading the signature.
"""
from __future__ import annotations

import pytest

from ligandai.errors import LigandAIValidationError
from ligandai.resources.campaigns import Campaigns


class _Recorder:
    """Captures what replicate() forwards to fold_panel."""

    def __init__(self):
        self.seen: dict | None = None


def _replicate_with(recorder, **kwargs):
    """Drive Campaigns.replicate far enough to capture the forwarded kwargs."""
    camp = Campaigns.__new__(Campaigns)

    def fake_fold_panel(sequences, **kw):
        recorder.seen = kw
        raise _Stop()

    camp.fold_panel = fake_fold_panel  # type: ignore[method-assign]
    with pytest.raises(_Stop):
        Campaigns.replicate(camp, ["SEQ"], target="BRD4", off_targets=["BRD2"],
                            band=2.0, target_leg_min=0.67, **kwargs)
    return recorder.seen


class _Stop(Exception):
    pass


def test_the_dict_spelling_is_unwrapped_not_forwarded_verbatim():
    r = _Recorder()
    seen = _replicate_with(r, panel_kwargs={"diffusion_samples": 2, "sampling_steps": 25})
    # The bug: `panel_kwargs` itself arriving as a keyword.
    assert "panel_kwargs" not in seen
    assert seen["diffusion_samples"] == 2
    assert seen["sampling_steps"] == 25


def test_the_keyword_spelling_still_works():
    r = _Recorder()
    seen = _replicate_with(r, diffusion_samples=4)
    assert seen["diffusion_samples"] == 4
    assert "panel_kwargs" not in seen


def test_force_fresh_still_defaults_true_through_the_dict_form():
    # replicate's whole purpose is an INDEPENDENT draw; the 24h dedupe would
    # otherwise hand back the selecting panel's own cached batch.
    r = _Recorder()
    seen = _replicate_with(r, panel_kwargs={"diffusion_samples": 1})
    assert seen["force_fresh"] is True


def test_an_explicit_keyword_beats_the_same_key_inside_the_dict():
    r = _Recorder()
    seen = _replicate_with(r, diffusion_samples=8, panel_kwargs={"diffusion_samples": 2})
    assert seen["diffusion_samples"] == 8


def test_force_fresh_false_inside_the_dict_is_respected():
    r = _Recorder()
    seen = _replicate_with(r, panel_kwargs={"force_fresh": False})
    assert seen["force_fresh"] is False


@pytest.mark.parametrize("bad", ["nope", 7, ["a"]])
def test_a_non_dict_panel_kwargs_is_a_named_error_not_a_deep_typeerror(bad):
    camp = Campaigns.__new__(Campaigns)
    camp.fold_panel = lambda *a, **k: None  # type: ignore[method-assign]
    with pytest.raises(LigandAIValidationError) as ei:
        Campaigns.replicate(camp, ["SEQ"], target="BRD4", off_targets=["BRD2"],
                            band=2.0, target_leg_min=0.67, panel_kwargs=bad)
    assert "panel_kwargs must be a dict" in str(ei.value)
