"""Download and pin a saved construct, or verify a returned fold. Never launches compute.

Requires: pip install ligandai biopython
Use the SDK source containing Targets.structure until its package release.
"""
from __future__ import annotations

import argparse
import io
import json
from pathlib import Path

# BRDT ET from Tiburon's rerun; not the sequence for BRD2/3/4 ET.
BRDT_ET = ('PMNYDEKRQLSLNINKLPGDKLGRVVHIIQSREPSLSNSNPDEIEI'
           'DFETLKASTLRELEKYVSACL')


def protein_sequence(content: str, format: str, chain_id: str) -> str:
    from Bio.PDB import MMCIFParser, PDBParser
    from Bio.PDB.Polypeptide import is_aa
    from Bio.Data.PDBData import protein_letters_3to1

    parser = MMCIFParser(QUIET=True) if format == 'cif' else PDBParser(PERMISSIVE=False, QUIET=True)
    structure = parser.get_structure('construct', io.StringIO(content))
    models = list(structure.get_models())
    if len(models) != 1:
        raise ValueError('Select one model explicitly; an ensemble is not a single construct.')
    if chain_id not in models[0]:
        raise ValueError(f'Chain {chain_id!r} is absent.')
    letters = []
    for residue in models[0][chain_id]:
        name = residue.get_resname().strip().upper()
        if residue.is_disordered() == 2:
            raise ValueError('Disordered residue identities require explicit selection.')
        if is_aa(residue, standard=False):
            if name not in protein_letters_3to1:
                raise ValueError(f'Modified amino acid {name} needs an explicit parent mapping.')
            letters.append(protein_letters_3to1[name])
        elif residue.id[0] == ' ':
            raise ValueError(f'Unknown polymer residue {name}; cannot verify the sequence.')
    if not letters:
        raise ValueError('No protein sequence in the requested chain.')
    return ''.join(letters)


def require_sequence(content: str, format: str, chain_id: str, expected: str) -> str:
    actual = protein_sequence(content, format, chain_id)
    if actual != expected:
        raise ValueError(f'Construct mismatch on chain {chain_id}: expected {len(expected)} aa, got {len(actual)} aa; sequence equality failed.')
    return actual


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--name')
    source.add_argument('--upload-id')
    source.add_argument('--check-fold', type=Path)
    parser.add_argument('--expected-sha256')
    parser.add_argument('--chain', default='A')
    parser.add_argument('--expect-sequence', required=True, help='Exact intended sequence; BRDT_ET selects the 67-aa BRDT constant only.')
    parser.add_argument('--output', type=Path, help='New output stem for .pdb/.cif and .pin.json.')
    args = parser.parse_args()
    expected = BRDT_ET if args.expect_sequence == 'BRDT_ET' else args.expect_sequence
    if args.check_fold:
        content = args.check_fold.read_text()
        format = 'cif' if args.check_fold.suffix.lower() in ('.cif', '.mmcif') else 'pdb'
        require_sequence(content, format, args.chain, expected)
        print(f'Exact receptor sequence verified: chain {args.chain}, {len(expected)} aa.')
        return
    if args.output is None:
        parser.error('--output is required for a download')
    from ligandai import LigandAI
    with LigandAI() as client:
        saved = client.targets.structure(args.name, upload_id=args.upload_id, expected_sha256=args.expected_sha256)
    require_sequence(saved.content, saved.format, args.chain, expected)
    path = Path(str(args.output) + '.' + saved.format)
    pin_path = Path(str(args.output) + '.pin.json')
    if path.exists() or pin_path.exists():
        raise FileExistsError('Choose a new output stem; existing constructs are preserved.')
    with path.open('xb') as f:
        f.write(saved.content.encode('utf-8'))
    with pin_path.open('x') as f:
        json.dump(dict(uploadId=saved.upload_id, contentSha256=saved.content_sha256,
                       chainId=args.chain, sequence=expected, residueCount=len(expected)), f, indent=2)
    print(f'Saved {path} and {pin_path}; exact {len(expected)}-aa sequence verified.')


if __name__ == '__main__':
    main()
