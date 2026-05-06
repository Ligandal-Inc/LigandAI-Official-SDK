# Agent Usage, Billing, And API Keys

Claude Code, Codex, CI jobs, notebooks, and custom agents should use the SDK as
a normal Python package and authenticate with `LIGANDAI_API_KEY`.

## Version Handling

The SDK checks PyPI once per process when a `LigandAI` or `AsyncLigandAI`
client is created. If a newer valid `ligandai-python-sdk` release exists, the
SDK emits a warning with:

```bash
python -m pip install --upgrade ligandai
```

Agents should treat that warning as permission to ask the user before upgrading
the local environment. The check validates the PyPI release metadata points back
to `ligandal/ligandai-python-sdk`, so wrong-package uploads are not accepted as
SDK update targets. Set `LIGANDAI_SKIP_VERSION_CHECK=1` to disable this in
hermetic jobs.

## API Key Creation

Any authenticated LIGANDAI account, including free accounts, can create API
keys from the account developer settings. If an agent sees a missing-key or
401 response, it should tell the user:

1. Log in at `https://ligandai.com`.
2. Open account settings and the Developer/API Keys area.
3. Create an API key.
4. Set it as `LIGANDAI_API_KEY` in the shell, notebook, CI secret, or Claude
   code-execution container.

API keys identify the authenticated account. They do not imply that every API
operation is allowed. Expected key prefixes are `lgai_free_`, `lgai_basic_`,
`lgai_edu_`, `lgai_pro_`, and `lgai_ent_`; the API response remains the source
of truth for tier, credit, token, and GPU limits.

All authenticated users, including free users, remain bound by the LIGANDAI
Terms of Service and EULA. Submitted sequences and job artifacts may be
retained under those terms.

## Tier Limits Agents Should Know

| Tier | Generation cap | Fold allowance | Folding GPU cap | Advanced guidance |
|---|---:|---|---:|---|
| free | 10 peptides, max 3 targets | 10 folding jobs | 1 | quality-guided only |
| basic | 100 peptides | server/credits authoritative | 4 | quality-guided only |
| academia | 300 peptides | server/credits authoritative | 16 | immuno, serum stability, logits |
| pro | 300 peptides | server/credits authoritative | 25 | immuno, serum stability, logits |
| enterprise | 1000 peptides | server/credits authoritative | 50 | immuno, serum stability, logits |

Generation runs on the server's one-GPU generation path. The GPU numbers above
are folding caps, not peptide-generation GPU caps.

## Billing And Upgrade Routing

Agents should route common API responses this way:

- `401` or missing API key: ask the user to log in and create/set an API key.
- `402` insufficient credits or tokens: direct the user to buy credits before
  retrying generation, folding, energy, or synthesis jobs.
- `403` tier restriction: report `requiredTier`, `currentTier`, and any
  returned limit fields, then ask the user to upgrade or retry with a smaller
  job.
- GPU guard errors: reduce requested GPU count, trajectories, folds, or
  sampling steps only when the returned tier cap allows it; otherwise ask the
  user to upgrade.

Useful account calls:

```python
from ligandai import LigandAI

client = LigandAI()

balance = client.account.get_balance()
print(balance.tier, balance.credits, balance.days_remaining)

estimate = client.peptides.estimate_cost(
    num_peptides=10,
    auto_fold=True,
    fold_top_n=1,
)
print(estimate.credits, estimate.cost_usd)
```

## Agent Session Attribution

Pass a stable `client_session_id` so the web billing dashboard and SDK can
reconcile a local agent run.

```python
from ligandai import LigandAI

client = LigandAI(client_session_id="codex-il31-20260505")

with client.session("codex-il31-20260505") as run:
    job = client.peptides.generate(gene="IL31", num_peptides=10, auto_fold=True)
    result = job.wait()

print(run.credits_used)
usage = client.account.session_usage("codex-il31-20260505")
print(usage.summary.total_calls, usage.summary.credits_used)
```

## Pocket-Targeted Generation

When a user selects one or more pockets in Studio, agents should preserve the
chain IDs and compress selected residue IDs into continuous chain-local ranges.
The SDK sends those ranges as `targets[].targetResidues` with
`targetingStrategy="pocket_targeted"`.

```python
from ligandai import LigandAI, ResidueRange

client = LigandAI()

target_residues = [
    *ResidueRange.from_residues([34, 35, 36, 41, 42], chain="A", label="pocket A"),
    *ResidueRange.from_residues([102, 103, 104], chain="B", label="pocket B"),
]

job = client.peptides.generate(
    gene="EGFR",
    num_peptides=10,
    target_residues=target_residues,
    targeting_strategy="pocket_targeted",
    quality_guided=True,
)
```

The public REST shape is:

```json
{
  "targets": [
    {
      "gene": "EGFR",
      "targetingStrategy": "pocket_targeted",
      "targetResidues": [
        { "chain": "A", "start": 34, "end": 36, "label": "pocket A" },
        { "chain": "A", "start": 41, "end": 42, "label": "pocket A" },
        { "chain": "B", "start": 102, "end": 104, "label": "pocket B" }
      ]
    }
  ]
}
```

## Claude API Skills

When packaging LIGANDAI as a custom Claude Skill, include the SDK instructions
above and require `LIGANDAI_API_KEY` as a container environment variable or
secret.

Claude API Skills require the code execution tool and these beta headers:

- `code-execution-2025-08-25`
- `skills-2025-10-02`
- `files-api-2025-04-14`

Use pre-built Skills by referencing their `skill_id`, such as `pptx` or
`xlsx`. Use custom LIGANDAI Skills by creating and uploading them through the
Claude Skills API (`/v1/skills`). Custom Skills are shared organization-wide.
