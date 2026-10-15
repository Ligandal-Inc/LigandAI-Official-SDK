from types import SimpleNamespace

import pytest

from ligandai import BliConfig
from ligandai.resources.peptides import Peptides, AsyncPeptides
from ligandai.resources.synthesis import Synthesis, AsyncSynthesis

CORE = 'QGVPHNYSRVSVQQLTPHDGRGLAEEQYLQFGIRGDVL'
LONG_SPACER = 'GGGGSGGGGSGGGGSGGGGS'


@pytest.mark.parametrize('position', ['c_terminal', 'n_terminal'])
def test_literal_segments_and_exact_construct_roundtrip(position):
    config = BliConfig(tag_position=position, linker_sequence=LONG_SPACER)
    segments = config.segment_config((40, 60)).model_dump(by_alias=True)['segments']
    assert [s['lengthRange'] for s in segments if s['type'] == 'binding'] == [(40, 60)]
    assert all(s['locked'] for s in segments if s['type'] == 'premade')
    assert any(s['sequence'] == LONG_SPACER and s['type'] == 'premade' for s in segments)
    construct = config.construct(CORE)
    assert config.binder_from_construct(construct) == CORE
    opposite = BliConfig(tag_position='n_terminal' if position == 'c_terminal' else 'c_terminal', linker_sequence=LONG_SPACER)
    with pytest.raises(ValueError, match='attachment end'):
        opposite.binder_from_construct(construct)


@pytest.mark.parametrize('position', ['c_terminal', 'n_terminal'])
def test_fold_frames_keep_spacer_and_exclude_capture_tag_from_binder_mask(position):
    config = BliConfig(tag_position=position, linker_sequence=LONG_SPACER)
    plan = config.fold_plan({'s0115': CORE})
    assert len(plan.frames) == 3
    assert plan.frames[0].sequence == CORE
    assert plan.frames[1].sequence == (CORE + LONG_SPACER if position == 'c_terminal' else LONG_SPACER + CORE)
    assert plan.frames[2].sequence == config.construct(CORE)
    for frame in plan.frames:
        mask = frame.binder_mask(frame.sequence)
        assert sum(mask) == len(CORE)
        assert ''.join(aa for aa, keep in zip(frame.sequence, mask) if keep) == CORE
    with pytest.raises(ValueError, match='align residues'):
        plan.frames[2].binder_mask(CORE)


def test_double_tagging_and_invalid_residues_refused():
    config = BliConfig()
    with pytest.raises(ValueError, match='binder_from_construct'):
        config.fold_plan({'already-tagged': config.construct(CORE)})
    with pytest.raises(ValueError):
        BliConfig(linker_sequence='GS--XX')


def test_batch_dedup_keeps_design_and_frame_aliases():
    config = BliConfig(linker_sequence='')
    plan = config.fold_plan({'one': CORE, 'same-core': CORE})
    assert len(plan.frames) == 6
    assert len(plan.sequences) == 2
    assert {f.design_id for f in plan.frames} == {'one', 'same-core'}


class CaptureTransport:
    def __init__(self):
        self.requests = []

    def request(self, method, path, **kwargs):
        self.requests.append((method, path, kwargs))
        if path == '/api/v1/folding/predict-batch':
            return {'batch_id': 'offline-bli-batch', 'jobs': [],
                    'peptide_count': len(kwargs['json'].get('peptides', kwargs['json'].get('groups', []))), 'total_cost_credits': 0}
        if path == '/api/adaptyv/experiments':
            return {'id': 'offline-bli-draft', 'name': 'BLI test', 'status': 'draft'}
        raise AssertionError(path)


def test_plan_submits_actual_sequences_via_real_sdk_batch_serializer():
    config = BliConfig(linker_sequence=LONG_SPACER)
    plan = config.fold_plan({'s0115': CORE})
    transport = CaptureTransport()
    # A transport fixture exercises actual SDK serialization without HTTP/spend.
    client = SimpleNamespace(peptides=Peptides(transport))
    run = plan.submit(client, target_gene='EGFR', diffusion_samples=4, seed=7)
    assert run.plan is plan
    assert run.job.batch_id == 'offline-bli-batch'
    method, path, kwargs = transport.requests[0]
    assert (method, path) == ('POST', '/api/v1/folding/predict-batch')
    assert [g['partners'][0]['sequence'] for g in kwargs['json']['groups']] == plan.sequences
    assert all(g['partners'][0]['linker_metadata']['sequence_sha256'] == f.sequence_sha256
               for g, f in zip(kwargs['json']['groups'], plan.frames))
    assert kwargs['json']['diffusion_samples'] == 4
    assert kwargs['json']['seed'] == 7
    assert LONG_SPACER in kwargs['json']['groups'][2]['partners'][0]['sequence']
    with pytest.raises(ValueError, match='do not override'):
        plan.submit(client, target_gene='EGFR', peptides=[CORE])


@pytest.mark.asyncio
async def test_async_plan_uses_same_frames():
    class AsyncCapture(CaptureTransport):
        async def request(self, *args, **kwargs):
            return super().request(*args, **kwargs)
    transport = AsyncCapture()
    plan = BliConfig().fold_plan({'s0115': CORE})
    run = await plan.submit_async(SimpleNamespace(peptides=AsyncPeptides(transport)), target_gene='EGFR')
    assert run.plan == plan
    assert [g['partners'][0]['sequence'] for g in transport.requests[0][2]['json']['groups']] == plan.sequences


@pytest.mark.parametrize('position,letter', [(None, 'C'), ('n_term', 'N'), ('c_term', 'C')])
def test_synthesis_wire_default_c_and_explicit_orientation(position, letter):
    transport = CaptureTransport()
    resource = Synthesis(transport)
    args = {} if position is None else {'tag_position': position}
    resource.adaptyv_create('BLI test', sequences=[CORE], **args)
    body = transport.requests[0][2]['json']
    assert body['tag_position'] == (position or 'c_term')
    assert next(iter(body['experiment_spec']['sequences'].values()))['metadata']['tag_location'] == letter


def test_synthesis_preserves_metadata_and_explicit_per_design_orientation():
    transport = CaptureTransport()
    resource = Synthesis(transport)
    resource.adaptyv_create('BLI test', sequences={
        'default': {'sequence': CORE, 'metadata': {'notes': 'custom spacer'}},
        'explicit': {'sequence': CORE, 'metadata': {'tag_location': 'N'}},
    })
    sequences = transport.requests[0][2]['json']['experiment_spec']['sequences']
    assert sequences['default']['metadata'] == {'notes': 'custom spacer', 'tag_location': 'C'}
    assert sequences['explicit']['metadata']['tag_location'] == 'N'
    assert sequences['default']['aa_string'] == CORE
    with pytest.raises(ValueError, match='tag_position'):
        resource.adaptyv_create('invalid', sequences=[CORE], tag_position='unknown')
    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_async_synthesis_uses_c_terminal_default():
    class AsyncCapture(CaptureTransport):
        async def request(self, *args, **kwargs):
            return super().request(*args, **kwargs)
    transport = AsyncCapture()
    await AsyncSynthesis(transport).adaptyv_create('BLI test', sequences=[CORE])
    body = transport.requests[0][2]['json']
    assert body['tag_position'] == 'c_term'
    assert next(iter(body['experiment_spec']['sequences'].values()))['metadata']['tag_location'] == 'C'
