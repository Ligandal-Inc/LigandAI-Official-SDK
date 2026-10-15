"""Download contract; transport doubles with public RCSB 1CRN coordinates.

Fixture: https://files.rcsb.org/download/1CRN.pdb, downloaded 2026-09-27.
Account/name labels below test transport identity, not biological annotation.
"""
import asyncio
import hashlib
from pathlib import Path

import pytest

from ligandai.errors import LigandAIValidationError
from ligandai.resources.targets import Targets, AsyncTargets

CONTENT = (Path(__file__).parent / 'fixtures' / 'saved-target-1crn.pdb').read_text()
HASH = hashlib.sha256(CONTENT.encode()).hexdigest()


def response():
    return dict(source='user_upload', format='pdb', content=CONTENT,
                contentSha256=HASH, byteLength=len(CONTENT.encode()),
                uploadId='userprotein_7', requestedName='BRDTET')


class Transport:
    def __init__(self, payload=None):
        self.payload = response() if payload is None else payload
        self.calls = []

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.payload


def test_download_by_name_preserves_bytes_and_identity():
    t = Transport()
    saved = Targets(t).structure(' BRDTET ')
    assert saved.content == CONTENT
    assert saved.content_sha256 == HASH
    assert saved.upload_id == 'userprotein_7'
    assert t.calls == [(('GET', '/api/targets/structure'), {'params': {'name': 'BRDTET'}})]


def test_exact_id_and_hash_are_sent_without_gene_lookup():
    t = Transport()
    Targets(t).structure(upload_id='userprotein_7', expected_sha256=HASH.upper())
    assert t.calls[0][1]['params'] == dict(uploadId='userprotein_7', expectedSha256=HASH)


@pytest.mark.parametrize('kwargs', [{}, {'name':'BRDT','upload_id':'userprotein_7'},
    {'name':''}, {'upload_id':'7'}, {'upload_id':'userprotein_7wrong'},
    {'name':'BRDT','expected_sha256':'bad'}])
def test_invalid_selection_never_calls_transport(kwargs):
    t = Transport()
    with pytest.raises(LigandAIValidationError):
        Targets(t).structure(**kwargs)
    assert not t.calls


@pytest.mark.parametrize('change', [dict(content=CONTENT+'\n'), dict(contentSha256='0'*64),
    dict(source='alphafold'), dict(uploadId='userpdb_7'), dict(format='html'),
    dict(byteLength=0), dict(content='')])
def test_changed_or_substituted_download_refuses(change):
    p = response(); p.update(change)
    with pytest.raises(LigandAIValidationError):
        Targets(Transport(p)).structure(upload_id='userprotein_7', expected_sha256=HASH)


def test_server_hash_cannot_override_callers_pin():
    with pytest.raises(LigandAIValidationError):
        Targets(Transport()).structure(upload_id='userprotein_7', expected_sha256='0'*64)


def test_server_cannot_change_requested_name():
    with pytest.raises(LigandAIValidationError):
        Targets(Transport()).structure('BRD4ET')


def test_async_uses_same_endpoint_and_validation():
    class AsyncTransport(Transport):
        async def request(self, *args, **kwargs):
            return super().request(*args, **kwargs)
    async def check():
        t = AsyncTransport()
        saved = await AsyncTargets(t).structure(upload_id='userprotein_7', expected_sha256=HASH)
        assert saved.content == CONTENT
        assert t.calls[0][0] == ('GET', '/api/targets/structure')
        with pytest.raises(LigandAIValidationError):
            await AsyncTargets(t).structure(upload_id='userprotein_7', expected_sha256='0'*64)
    asyncio.run(check())


def test_example_verifies_whole_sequence_and_rejects_same_length_mismatch():
    pytest.importorskip('Bio')
    import importlib.util
    spec = importlib.util.spec_from_file_location('saved_et_example', Path(__file__).parents[1] / 'examples' / 'saved_et_construct.py')
    example = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(example)
    expected = 'TTCCPSIVARSNFNVCRLPGTPEAICATYTGCIIIPGATCPGDYAN'
    assert example.require_sequence(CONTENT, 'pdb', 'A', expected) == expected
    with pytest.raises(ValueError, match='sequence equality failed'):
        example.require_sequence(CONTENT, 'pdb', 'A', 'A' + expected[1:])
    with pytest.raises(ValueError, match='absent'):
        example.require_sequence(CONTENT, 'pdb', 'Z', expected)
    assert len(example.BRDT_ET) == 67
