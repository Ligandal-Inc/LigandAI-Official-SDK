"""Requested candidate x target ledger, using actual SDK batch serialization."""
import hashlib
from types import SimpleNamespace
import pytest
from ligandai.errors import LigandAIValidationError
from ligandai.multitarget import CoOptimization, TargetSpec
from ligandai.resources.campaigns import Campaigns
from ligandai.resources.peptides import Peptides

A='ACDEFGHIKLMNP'; B='WYVTSRQNKHGFE'; OTHER='LLLLIIIVVVAAA'


class Transport:
    def __init__(self, rows): self.rows=rows; self.calls=[]
    def request(self, method, path, **kwargs):
        assert method == 'POST' and path == '/api/v1/folding/predict-batch'
        body=kwargs['json']; self.calls.append(body); n=len(self.calls)
        entries=self.rows(body,n)
        return {'batch_id':f'batch-{n}','jobs':entries,'peptide_count':len(body['peptides'])}


def make_panel(rows, sequences=(A,B), targets=('EGFR','ERBB2'), replicates=1):
    transport=Transport(rows)
    client=SimpleNamespace(peptides=Peptides(transport=transport))
    resource=Campaigns(transport=transport,client=client)
    panel=resource.fold_panel(sequences,targets=CoOptimization(targets,aggregate='worst_case'),
        off_targets=['ERBB3'],replicates=replicates,force_fresh=True,wait=False)
    return panel,transport


def good_rows(body,n):
    return [{'job_id':f'job-{n}-{i}','sequence':s,'peptide_index':i} for i,s in enumerate(body['peptides'])]


def test_actual_requests_keep_dedup_aliases_and_reordered_rows_join_correctly():
    panel,t=make_panel(lambda b,n:list(reversed(good_rows(b,n))),sequences=[A,A.lower(),B])
    assert all(b['peptides']==[A,B] for b in t.calls)
    assert [b['target_gene'] for b in t.calls]==['EGFR','ERBB2','ERBB3']
    assert panel.sequences==[A,B]
    assert panel.candidate_input_indices=={A:(0,1),B:(2,)}
    assert panel.candidate_sha256[A]==hashlib.sha256(A.encode()).hexdigest()
    assert panel.leg(A,'EGFR').fold_job_ids==['job-1-0']
    assert panel.leg(B,'EGFR').fold_job_ids==['job-1-1']


@pytest.mark.parametrize('kind',['all_missing','one_candidate_missing','missing_job','unscorable'])
@pytest.mark.parametrize('targets',[('EGFR',),('EGFR','ERBB2')])
def test_every_requested_candidate_survives_missing_or_unscorable_results(kind,targets):
    def rows(body,n):
        if kind=='all_missing': return []
        result=good_rows(body,n)
        if kind=='one_candidate_missing': return result[:1]
        if kind=='missing_job':
            for row in result: row.pop('job_id')
        return result
    panel,_=make_panel(rows,targets=targets)
    complete,partial,summary=panel.deltas(band=.1,target_leg_min=.66)
    assert panel.sequences==[A,B]
    assert complete==[] and {r.sequence for r in partial}=={A,B}
    assert all(r.delta_upper_bound is None and r.bound_kind=='indeterminate' for r in partial)
    assert summary['n_requested_candidates']==2 and summary['n_partial']==2
    assert summary['n_partial_upper_bounds_not_verdicts']==0
    assert summary['n_pass_both']==0 and summary['n_partial_indeterminate']==2
    if len(targets)==1: assert all(r.target_ipsae is None for r in partial)
    if kind=='all_missing': assert len(panel.submission_issues)==2*(len(targets)+1)
    if kind=='one_candidate_missing':
        assert all(x['sequence']==B for x in panel.submission_issues)


@pytest.mark.parametrize('entry',[
    {'sequence':OTHER,'peptide_index':0,'job_id':'x'},
    {'sequence':B,'peptide_index':0,'job_id':'x'},
    {'sequence':A,'peptide_index':True,'job_id':'x'},
    {'sequence':A,'peptide_index':-1,'job_id':'x'},
    {'sequence':A,'peptide_index':2,'job_id':'x'},
    {'sequence':A,'peptide_index':0,'peptideIndex':1,'job_id':'x'},
    {'sequence':A,'peptide_index':0,'job_id':'x','jobId':'different'},
    {'job_id':'x'},
    {'sequence':123,'job_id':'x'},
])
def test_unknown_ambiguous_or_inconsistent_response_cannot_enter_matrix(entry):
    with pytest.raises(LigandAIValidationError): make_panel(lambda b,n:[entry])


def test_identical_response_aliases_and_duplicates_are_idempotent_and_recorded():
    def rows(body,n):
        r={'sequence':A,'peptide_index':0,'peptideIndex':0,'job_id':f'x{n}','jobId':f'x{n}'}
        return [r,dict(r)]
    panel,_=make_panel(rows)
    assert panel.leg(A,'EGFR').fold_job_ids==['x1']
    assert len([x for x in panel.submission_issues if x['kind']=='duplicate_response_row'])==3
    assert panel.sequences==[A,B]


def test_two_jobs_for_same_candidate_draw_are_conflicting_not_extra_evidence():
    with pytest.raises(LigandAIValidationError,match='conflicting duplicate'):
        make_panel(lambda b,n:[{'sequence':A,'job_id':'one'},{'sequence':A,'job_id':'two'}])


def test_job_reuse_across_species_is_rejected():
    with pytest.raises(LigandAIValidationError,match='reused'):
        make_panel(lambda b,n:[{'sequence':A,'job_id':'same-job'}])


def test_job_reuse_across_independent_draws_is_rejected():
    with pytest.raises(LigandAIValidationError,match='reused'):
        make_panel(lambda b,n:[{'sequence':A,'job_id':'same-job'}],replicates=2)


def test_sequence_only_and_index_only_results_join_to_same_requested_identity():
    def rows(body,n):
        return [{'sequence':A,'jobId':f'{n}-A'},{'peptideIndex':1,'jobId':f'{n}-B'}]
    panel,_=make_panel(rows)
    assert set(panel.legs)=={(s,t) for s in (A,B) for t in ('EGFR','ERBB2','ERBB3')}


def test_same_gene_different_ortholog_sequences_remain_different_receptor_legs():
    specs=[TargetSpec(gene='EGFR',organism='human',receptor_sequence='AAAA'),
           TargetSpec(gene='EGFR',organism='mouse',receptor_sequence='CCCC')]
    panel,t=make_panel(good_rows,targets=specs)
    assert [b.get('receptor_sequence') for b in t.calls[:2]]==['AAAA','CCCC']
    assert panel.leg(A,'EGFR@human').fold_job_ids!=panel.leg(A,'EGFR@mouse').fold_job_ids
