# Retrieve a saved domain without resolving the full gene

`client.targets.structure()` downloads an account-owned upload by saved name or
store-qualified upload ID. It does not use `/api/structure/:gene`, substitute a
canonical structure, trim coordinates, or launch a fold. The helper is available
in this SDK source; package publication is a separate release step.

```python
from pathlib import Path
from ligandai import LigandAI

with LigandAI() as client:
    for c in client.targets.candidates('BRD4ET'):
        print(c.upload_id, c.name, c.residue_count, c.content_sha256)
    # Choose the intended conformer, then provide its actual ID and saved hash.
    saved = client.targets.structure(upload_id=chosen_id, expected_sha256=pinned_hash)
    Path('receptor.' + saved.format).write_bytes(saved.content.encode('utf-8'))
```

To retrieve an unambiguous saved name, use `client.targets.structure('BRDTET')`.
Different structures sharing a name return HTTP 409 with candidates unless an
explicit saved choice applies. A missing upload returns 404. Changed pinned
content returns 409. None of these failures falls back to a gene lookup.
`client.targets.choose(name, upload_id)` stores a deliberate choice; downloads
report it through `saved.choice`. Prefer ID plus hash for reproducible runs.

The equivalent HTTP request is:

```text
GET /api/targets/structure?uploadId=userprotein_<id>&expectedSha256=<sha256>
Authorization: Bearer <your API key>
```

For name discovery use `GET /api/targets?name=BRD4ET`; use the returned candidate
IDs rather than inventing IDs or taking the first candidate. The content response
contains `uploadId`, `content`, `format`, `contentSha256`, `byteLength`, and `choice`.
The SHA-256 covers UTF-8 bytes of the returned content; SDK verifies it locally.

## Verify what will be folded

A gene label is not a domain boundary. A structure's length alone is also not
proof of sequence identity: two paralogs can have the same length. Compare the
complete intended receptor sequence before submitting, then repeat the check on
the receptor chain of each returned fold. `examples/saved_et_construct.py` does
both checks without launching compute, using Biopython as an optional dependency.
It refuses multiple models, absent chains, unsupported residues and sequence
mismatches. Preserve PDB residue numbers for targeting; do not reinterpret them
as canonical UniProt positions.

```sh
python examples/saved_et_construct.py --upload-id userprotein_<id> \
  --expected-sha256 <sha256> --chain A --expect-sequence BRDT_ET --output ./brdt_et
python examples/saved_et_construct.py --check-fold returned.pdb \
  --chain A --expect-sequence BRDT_ET
```

`BRDT_ET` is specifically the 67-residue BRDT rerun sequence supplied by Andre.
It must not be reused as the expected BRD2, BRD3, or BRD4 ET sequence. Supply each
of those exact sequences separately. For ensembles, verify each output model
individually. Missing modeled residues are reported as a mismatch, not imputed.
