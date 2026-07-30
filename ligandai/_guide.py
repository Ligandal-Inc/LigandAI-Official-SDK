# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Self-surfacing workflow map for agents.

``ligandai.guide()`` (and the ``LigandAI.guide()`` / ``AsyncLigandAI.guide()``
methods that delegate here) print a concise map of the canonical LigandAI
workflows so an LLM agent that pokes the API instead of reading docs is told,
at runtime, what to reach for — including the built-in target-discovery
funnel — and where the full agent docs live.
"""

from __future__ import annotations

_GUIDE = """\
LigandAI SDK — canonical workflows (call ligandai.guide() / client.guide())

The platform already does the heavy lifting server-side. Reach for the built-in
namespaces below FIRST — don't hand-stitch external data sources.

START HERE for TARGET DISCOVERY (find/rank targets) — client.discovery:
  The transcriptomics funnel is native. Do NOT hand-stitch GTEx + CellGuide /
  CellxGene — the specificity-index (SI) ranking is already computed server-side.
    1. client.discovery.tissues() / .organ_systems()      # resolve identifiers
    2. client.discovery.tissue_markers(                    # SI-ranked surface receptors (GTEx)
           target_tissues=["Kidney - Cortex"],
           exclude_tissues=[...], receptor_only=True, top_n=200)
       -> markers.top[0].gene   (receptor_only=True is the cell-surface filter)
    3. client.discovery.cell_type_markers(...)             # single-cell resolution (Academia+)
       client.discovery.compare_targets([target, ref, ...])# SHARED vs DIFFERENTIAL selectivity
       client.discovery.transport_vasculome(modality=...)  # BBB transcytosis shuttles (Enterprise)
    Custom data:
       ds = client.discovery.upload_dataset("counts.h5ad", dataset_type="bulk")
       client.discovery.tissue_markers(
           custom_dataset_targets=[{"datasetId": ds.id}], receptor_only=True)

DESIGN (generate binders) — client.peptides:
    job = client.peptides.generate(gene=<gene>, num_peptides=50,
                                   auto_fold=True, top_n_fold=10,
                                   fold_gpus=<tier cap>)
    result = job.wait()                      # designs against a known gene / PDB ID / upload

FOLD (Boltz-2 complex) — client.peptides.fold(...) / client.folds.* (hotspot partition)

SCORE (DeltaForge dG/Kd) — client.deltaforge.score_pdb(...) / .score_fold(...)
    client.peptides.score_complex(...)       # score-only, skip folding

SYNTHESIS (Adaptyv BLI / SPPS) — client.synthesis.*

CANONICAL CHAIN:  discovery -> pick gene -> generate -> fold -> score -> synthesize

Full agent docs ship inside the package:
  - AGENTS.md                     (workflows, tier caps, error handling, job lifecycle)
  - .claude/skills/ligandai/      (discovery.md, generate.md, fold.md, synthesis.md,
                                   program.md, SKILL.md — drop into .claude/skills/)
Read AGENTS.md before writing SDK code.
"""


def guide(print_it: bool = True) -> str:
    """Return (and, by default, print) the canonical-workflow map.

    :param print_it: when ``True`` (default) also print the map to stdout, so a
        bare ``ligandai.guide()`` in a REPL / notebook is useful; the text is
        always returned for programmatic use.
    """
    if print_it:
        print(_GUIDE)
    return _GUIDE
