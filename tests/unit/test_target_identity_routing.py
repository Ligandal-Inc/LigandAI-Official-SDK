"""Offline request-boundary regression: no ortholog identity fallback or paid calls."""
from types import SimpleNamespace

import pytest

from ligandai.errors import LigandAIValidationError
from ligandai.multitarget import CoOptimization, TargetSpec, coerce_target
from ligandai.resources.campaigns import Campaigns, RecursiveCampaign
from ligandai.resources.peptides import Peptides

PDB = (
    'ATOM      1  N   MET A   1      11.104   6.134  -6.504  1.00 00.00           N\n'
    'ATOM      2  CA  MET A   1      12.104   6.134  -6.504  1.00 00.00           C\n'
)


class Transport:
    def __init__(self): self.calls = []
    def request(self, *args, **kwargs):
        self.calls.append(kwargs['json'])
        return {'sessionId': f'test-{len(self.calls)}'}


def test_same_gene_human_mouse_actual_generation_serialization_keeps_exact_structure():
    transport = Transport(); resource = Peptides(transport=transport)
    for species, offset in [('human', '11.104'), ('mouse', '21.104')]:
        pdb = PDB.replace('11.104', offset)
        spec = coerce_target({'gene': 'EGFR', 'organism': species, 'receptor_pdb': pdb})
        resource.generate(**spec.generate_kwargs(), num_peptides=1, auto_fold=False)
        wire = transport.calls[-1]['targets'][0]
        assert wire['pdbData'] == pdb
        assert wire['organism'] == species
        assert wire['targetKey'] == f'EGFR@{species}'
        assert spec.fold_kwargs()['receptor_pdb'] == pdb
    assert transport.calls[0]['targets'] != transport.calls[1]['targets']


@pytest.mark.parametrize('selector,wire_key', [('user_pdb_id', 'userPdbId'), ('pdb_code', 'pdbId')])
def test_identifier_generation_reaches_actual_wire_and_folding_refuses_fallback(selector, wire_key):
    value = 4212 if selector == 'user_pdb_id' else '3MXF'
    spec = TargetSpec(gene='EGFR', organism='mouse', **{selector: value})
    transport = Transport(); resource = Peptides(transport=transport)
    resource.generate(**spec.generate_kwargs(), num_peptides=1, auto_fold=False)
    assert transport.calls[0]['targets'][0][wire_key] == value
    with pytest.raises(LigandAIValidationError, match='canonical-gene fallback'):
        spec.fold_kwargs()


def test_sequence_route_is_exact_for_fold_and_rejected_for_generation():
    spec = TargetSpec(gene='EGFR', organism='mouse', receptor_sequence='ACDEFGHIKLMNP', msa_source_gene='EGFR_MOUSE')
    assert spec.fold_kwargs() == {'receptor_sequence': 'ACDEFGHIKLMNP', 'receptor_name': 'EGFR@mouse', 'msa_source_gene': 'EGFR_MOUSE'}
    with pytest.raises(LigandAIValidationError, match='sequence-bound'):
        spec.generate_kwargs()


@pytest.mark.parametrize('kwargs', [
    {'receptor_pdb': PDB, 'receptor_sequence': 'ACD'},
    {'user_pdb_id': 4212, 'pdb_code': '3MXF'},
    {'receptor_pdb': PDB, 'user_pdb_id': 4212},
    {'receptor_pdb': ''}, {'receptor_sequence': ' '}, {'user_pdb_id': 0}, {'user_pdb_id': True},
])
def test_ambiguous_or_empty_identity_is_rejected(kwargs):
    with pytest.raises(LigandAIValidationError): TargetSpec(gene='EGFR', **kwargs)


def test_inline_path_is_resolved_without_replacing_bytes(tmp_path):
    path = tmp_path / 'mouse.pdb'; path.write_text(PDB)
    spec = TargetSpec(gene='EGFR', organism='mouse', receptor_pdb=str(path))
    assert spec.generate_kwargs()['targets'][0]['pdbData'] == PDB


@pytest.mark.parametrize('pdb', ['ATOM      1  N   MET A   1\n', 'not a PDB' * 30])
def test_inline_that_server_would_ignore_is_rejected(pdb):
    with pytest.raises(LigandAIValidationError, match='dispatchable'):
        TargetSpec(gene='EGFR', receptor_pdb=pdb).generate_kwargs()


def make_run(specs, kwargs=None):
    calls=[]
    def generate(**kw):
        calls.append(kw); return SimpleNamespace(peptides=[])
    run = RecursiveCampaign(campaigns=SimpleNamespace(_client=SimpleNamespace(peptides=SimpleNamespace(generate=generate))),
        co_optimization=CoOptimization(specs, aggregate='worst_case'), off_targets=['ERBB3'],
        band=.1, target_leg_min=.66, generate_kwargs=kwargs)
    return run, calls


@pytest.mark.parametrize('key', ['gene', 'targetGene', 'targets', 'pdb_data', 'pdbData', 'userPdbId', 'receptor_sequence', 'species',
    'hotspot_residues', 'hotspotResidues', 'target_chains', 'targetChains', 'coOptimization'])
def test_shared_identity_or_frame_alias_cannot_override_target(key):
    with pytest.raises(LigandAIValidationError, match='cannot override target identity'):
        make_run(['EGFR', 'ERBB2'], {key: 'OTHER'})


def test_mutated_shared_identity_rejected_again_at_request_boundary():
    run,calls=make_run(['EGFR', 'ERBB2'])
    run.generate_kwargs['gene']='OTHER'
    with pytest.raises(LigandAIValidationError): run._generate([])
    assert calls == []


def test_all_generation_routes_preflight_before_any_leg_is_submitted():
    run,calls=make_run(['EGFR', TargetSpec(gene='EGFR_MOUSE', organism='mouse', receptor_sequence='ACD')])
    with pytest.raises(LigandAIValidationError, match='sequence-bound'): run._generate([])
    assert calls == []


def test_shared_nonidentity_controls_are_retained_and_targets_remain_distinct():
    run,calls=make_run(['EGFR', 'ERBB2'], {'sampling_steps': 42})
    run._generate([])
    assert [c['gene'] for c in calls] == ['EGFR','ERBB2']
    assert all(c['sampling_steps'] == 42 for c in calls)
    assert [c['targets'][0]['gene'] for c in calls] == ['EGFR','ERBB2']


def test_all_fold_routes_preflight_before_any_leg_is_submitted():
    calls=[]
    campaign=Campaigns(transport=None, client=SimpleNamespace(peptides=SimpleNamespace(fold_batch=lambda **kw: calls.append(kw))))
    with pytest.raises(LigandAIValidationError, match='cannot resolve'):
        campaign.fold_panel(['ACD'], targets=CoOptimization([
            TargetSpec(gene='EGFR', organism='human'),
            TargetSpec(gene='EGFR', organism='mouse', user_pdb_id=4212),
        ], aggregate='worst_case'), off_targets=['ERBB2'], wait=False)
    assert calls == []
