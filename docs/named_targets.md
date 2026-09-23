# Named targets — your own uploads, addressed by a name you chose

Gene resolution answers exactly one question: *is this a symbol in the global
gene/UniProt universe?* A domain-scale construct can only ever answer no. The ET
domain of BRD4 is a ~68-residue sub-domain, so `"BRD4ET"` is a real name for a
real structure and a hard 404 as a gene.

Name an upload once and the name targets it:

```python
prot = client.proteins.upload_pdb("Brd4ET.pdb", gene="BRD4", custom_name="BRD4ET")
job  = client.peptides.generate(gene="BRD4ET", num_peptides=200)
#                                     ^ no user_pdb_id needed
```

`client.targets.list()` shows every name your account can target;
`client.targets.resolve("BRD4ET")` shows what one exact string will hit — the
same decision the generate path makes, so the two cannot disagree.

## When one name means several structures

Upload three conformational states of one domain and call them all `BRD4ET`, and
the platform **refuses rather than guessing**. That refusal is the point: a
silently-picked conformer folds, scores and reaches your results table looking
*exactly* like a correct answer, so "most recent upload wins" would be
indistinguishable from working.

What you get instead is a refusal you can answer **without a second call**:

```python
from ligandai import LigandAIAmbiguousTarget

try:
    job = client.peptides.generate(gene="BRD4ET", num_peptides=200)
except LigandAIAmbiguousTarget as e:
    for c in e.candidates:
        print(c.upload_id, c.name, c.residue_count, c.size_bytes,
              f"+{c.duplicate_count} identical re-uploads", c.aliases)
    # userprotein_7    Brd4ET_closed                67  44388  +15 identical re-uploads
    # userprotein_42   Brd4ET_closed_state_form     68 117719  +23 identical re-uploads
    # userprotein_598  Brd4ET_isoform_target_state  67  43296  +0  identical re-uploads
    job = client.peptides.generate(
        gene="BRD4ET", user_pdb_id=e.candidates[2].upload_id, num_peptides=200,
    )
```

The refusal arrives at **submit time**, before any credits are spent — not as a
failed job minutes later.

To look before you call, without submitting anything:

```python
for c in client.targets.candidates("BRD4ET"):
    print(c.upload_id, c.name, c.residue_count)
```

### Duplicates are collapsed, not listed

One structure re-uploaded 38 times is one structure and 38 rows. Byte-identical
re-uploads fold into their representative and are **counted** on
`duplicate_count`, so a name with three real answers shows three candidates
rather than forty-one rows. Nothing is deleted — `duplicate_ids` names every
folded row, and `aliases` carries any *other* name those rows answered to, so
collapsing can never make a name unaddressable.

## Deciding once: the saved choice

```python
client.targets.choose("BRD4ET", "userprotein_598")
client.peptides.generate(gene="BRD4ET", num_peptides=200)   # resolves, no id needed
```

A saved choice is a **decision you made**, and it stays one:

* Nothing infers it. There is no source other than `"user"`.
* It is **visible**. `client.targets.choice("BRD4ET")` and
  `client.targets.choices()` show it, `resolve()` returns it, and a job that
  resolved through it says so.
* It is **reversible** — `client.targets.clear_choice("BRD4ET")` and the name
  goes back to asking.
* It is **re-validated against the live candidates every time**. Delete or rename
  the chosen structure and the choice goes *stale*: the name refuses and tells
  you the choice expired. It never falls through to whatever is left.

`resolve()` keeps reporting `outcome == "ambiguous"` even when a choice applies —
the name *is* ambiguous; the choice is how it gets answered, not evidence it
never was. `usable` is `True` in that case, because the choice is what makes it
usable.

## Three behaviours worth knowing

1. **The global gene universe wins.** Name an upload `"BRD4"` and `BRD4` still
   means the gene. `resolve()` reports the collision (`resolves_to == "gene"`
   with candidates populated) so you can pass the upload id when you meant your
   own structure. A name never silently redefines a real gene for your account.
2. **Ambiguity asks or uses your choice — it never guesses.**
3. **Names are tiered.** `custom_name` beats an inferred gene symbol, which beats
   the filename stem. A lower tier never competes with a higher one, so renaming
   a *file* cannot change which structure a campaign targets.

## Upload ids are store-qualified, always

Pass `userprotein_598` or `userpdb_12`, exactly as a candidate reports it. A bare
integer is rejected: the two upload stores have independent id sequences and 31
numeric ids exist in **both**, pointing at different structures.
