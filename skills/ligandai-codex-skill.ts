// Copyright © 2025 Ligandal, Inc. All rights reserved.
/**
 * LigandAI Codex Skill — Claude Agent SDK
 *
 * Provides structured peptide design capabilities to Codex agents via the
 * LigandAI Python SDK. Handles generation, folding, BLI synthesis prep, and
 * billing through typed tool interfaces.
 *
 * Usage (Claude Agent SDK):
 *   import { ligandaiSkill } from "./ligandai-codex-skill";
 *   const agent = new Agent({ skills: [ligandaiSkill] });
 */

import type { Skill, ToolDefinition } from "@anthropic-ai/agent-sdk";

// ─────────────────────────────────────────────────────────────────────────────
// Shared type fragments (reused across tools)
// ─────────────────────────────────────────────────────────────────────────────

const SEGMENT_TYPES = ["binding", "linker", "stability", "premade"] as const;
type SegmentType = typeof SEGMENT_TYPES[number];

const TIER_LABELS = {
  free: "Free (100 peptides, 1 trajectory, 15 steps — locked)",
  basic: "Basic (300 peptides, up to 4 trajectories, 50 steps)",
  academia: "Academia (300 peptides, up to 10 trajectories, 1000 steps)",
  pro: "Pro (1000 peptides, up to 10 trajectories, 1000 steps)",
  enterprise: "Enterprise (5000 peptides, up to 10 trajectories, 1000 steps)",
};

// ─────────────────────────────────────────────────────────────────────────────
// Tool: design_peptide
// ─────────────────────────────────────────────────────────────────────────────

const designPeptideTool: ToolDefinition = {
  name: "ligandai_design_peptide",
  description: `Submit a peptide binder design job against a target protein using LigandAI.

**Segment types for scaffold configuration:**
- binding   — generated to contact the target (binding mask active)
- linker    — generated without binding objective (flexible connector)
- stability — generated for intramolecular stability
- premade   — fixed sequence, not generated (use for TAT, NES, GS linkers, etc.)

**Tier limits (server-enforced):**
- free: 100 peptides, 1 trajectory max (15 steps, locked)
- basic: 300 peptides, 4 trajectories max (15-50 steps)
- academia/pro/enterprise: 300-5000 peptides, 10 trajectories max (15-1000 steps)

**Quick sampling (valid):** 15 steps / 1 trajectory — use for broad screening.
**Production:** 50 steps / 4 trajectories — standard for final candidates.

**MSA note:** MSA is fetched for the receptor chain only. Designed peptides
are de novo sequences — no MSA is run for them.

Returns session_id for polling via ligandai_get_job.`,

  inputSchema: {
    type: "object" as const,
    properties: {
      gene: {
        type: "string",
        description: "Target gene symbol (e.g. 'EGFR', 'CD8A', 'IL31RA')",
      },
      num_peptides: {
        type: "integer",
        default: 300,
        minimum: 10,
        maximum: 5000,
        description: "Peptides to generate (tier-gated max)",
      },
      length_min: { type: "integer", default: 20, minimum: 5, maximum: 100 },
      length_max: { type: "integer", default: 70, minimum: 10, maximum: 150 },
      auto_fold: {
        type: "boolean",
        default: true,
        description: "Run Boltz-2 structure prediction after generation",
      },
      max_folds: {
        type: "integer",
        default: 25,
        minimum: 8,
        maximum: 200,
        description: "Peptides to fold (stratified across 16 bins: 4 energy × 4 length)",
      },
      num_trajectories: {
        type: "integer",
        default: 1,
        minimum: 1,
        maximum: 10,
        description: "Boltz-2 diffusion samples per fold. Default=1 (all tiers); 4=standard quality (basic+); 10=max (academia+)",
      },
      sampling_steps: {
        type: "integer",
        default: 50,
        minimum: 15,
        maximum: 1000,
        description: "LigandForge diffusion steps. Free=15 locked, basic=15-50, academia+=15-1000",
      },
      fold_strategy: {
        type: "string",
        enum: ["consolidated", "distributed"],
        default: "consolidated",
        description: "consolidated=all GPUs per target; distributed=split across targets",
      },
      ec_trimming: {
        type: "object",
        description: "Structure preparation — topology-aware EC domain trimming",
        properties: {
          remove_signal_peptide: { type: "boolean", default: true },
          generation_mode: {
            type: "string",
            enum: ["ec_only", "ec_tm", "full"],
            default: "ec_only",
            description: "ec_only=single-pass TM default; ec_tm=multi-pass TM (GPCRs require this)",
          },
          folding_mode: {
            type: "string",
            enum: ["ec_only", "trim_terminal_ic", "full"],
            default: "ec_only",
            description: "ec_only=single-pass; trim_terminal_ic=multi-pass TM. CAUTION: full often yields iPSAE=0 for TM proteins",
          },
        },
      },
      segment_config: {
        type: "object",
        description: "Multi-segment scaffold (simple=single binding domain, custom=explicit segments)",
        properties: {
          mode: { type: "string", enum: ["simple", "custom"], default: "simple" },
          segments: {
            type: "array",
            items: {
              type: "object",
              properties: {
                type: { type: "string", enum: ["binding", "linker", "stability", "premade"] },
                length_min: { type: "integer" },
                length_max: { type: "integer" },
                sequence: { type: "string", description: "Required when type=premade" },
                label: { type: "string" },
              },
              required: ["type"],
            },
          },
        },
      },
      pdc_config: {
        type: "object",
        description: "Peptide-Drug Conjugate (Pro+ tier). Drug is co-folded with Boltz-2.",
        properties: {
          drug_name: {
            type: "string",
            description: "Built-in: ciprofloxacin, doxorubicin, MMAE, maytansine, FITC, biotin, SN-38, gemcitabine",
          },
          linker_sequence: {
            type: "string",
            enum: ["GSGSG", "GSGSGSGGS", "PLGLAG", "GFLG", "DEVDG", "EAAAKEAAAKEAAAK"],
            default: "GSGSG",
            description: "PLGLAG=MMP-cleavable, GFLG=cathepsin-cleavable, DEVDG=caspase-cleavable",
          },
          linker_position: {
            type: "string",
            enum: ["c_terminal", "n_terminal"],
            default: "c_terminal",
          },
        },
      },
      // Charge & solubility (pro+)
      charge_mode: {
        type: "string",
        enum: ["off", "lt", "gt", "between"],
        description: "Net charge filter at pH 7.4. lt=charge<value, gt=charge>value, between=range",
      },
      charge_value: { type: "number" },
      charge_min: { type: "number" },
      charge_max: { type: "number" },
      min_solubility: {
        type: "number",
        enum: [0, 0.5, 1.0, 1.5],
        description: "0=none, 0.5=low, 1.0=medium, 1.5=high",
      },
      // Quality-guided generation (basic+ default ON)
      quality_guided: {
        type: "boolean",
        default: false,
        description: "Quality-guided generation using LigandForge v6.5 (basic+ default ON, +20 credits/peptide surcharge). Disabled automatically for cyclic modes.",
      },
      quality_guidance_scale: { type: "number", default: 1.0 },
      // Immune guidance (academia+) — MHC-I/II anchor avoidance + scoring during diffusion
      immunogenicity: {
        type: "boolean",
        default: false,
        description: "Immune guidance: steers generation away from MHC-binding motifs toward low predicted immunogenicity. Uses MHC-I/II anchor avoidance + scoring during diffusion (academia+).",
      },
      immuno_strength: {
        type: "number",
        default: 2.0,
        minimum: 0.5,
        maximum: 4.0,
        description: "Immune guidance strength: 0.5=mild, 2.0=standard, 4.0=aggressive (enterprise unlocks >3.0)",
      },
      immuno_modules: {
        type: "object",
        description: "Per-module override: {mhc_i, mhc_ii} each boolean",
      },
      // Stability guidance (academia+) — proteolytic stability, resists serum protease cleavage
      serum_stability: {
        type: "boolean",
        default: false,
        description: "Stability guidance: resist serum protease cleavage (academia+)",
      },
      stability_mode: {
        type: "string",
        enum: ["resist", "target"],
        default: "resist",
        description: "resist=avoid cleavage sites, target=prodrug (expose cleavage sites)",
      },
      glycosylation_enabled: {
        type: "boolean",
        default: false,
        description: "Cell-type-specific glycan modeling (pro+)",
      },
      clash_resolution_enabled: {
        type: "boolean",
        default: true,
        description: "Auto-resolve steric clashes before DeltaForge scoring (basic+)",
      },
      md_relaxation_enabled: {
        type: "boolean",
        default: false,
        description: "FastMD post-Boltz2 relaxation <100ms/structure (pro+)",
      },
      cyclic_mode: {
        type: "string",
        enum: ["disulfide", "lactam", "head_tail_contact"],
        description: "Cyclization: disulfide=Cys-Cys bridge (academia+)",
      },
      cysteine_mode: {
        type: "string",
        enum: ["disulfide_only", "allow_all", "exclude_all"],
        default: "disulfide_only",
      },
      program_id: { type: "integer", description: "Associate with program workstream" },
    },
    required: ["gene"],
  },

  handler: async (params: Record<string, unknown>): Promise<string> => {
    const { execSync } = await import("child_process");
    const script = buildGenerateScript(params);
    const result = execSync(`python3 -c "${script.replace(/"/g, '\\"')}"`, {
      env: { ...process.env },
      encoding: "utf-8",
      timeout: 30_000,
    });
    return result.trim();
  },
};

// ─────────────────────────────────────────────────────────────────────────────
// Tool: get_job
// ─────────────────────────────────────────────────────────────────────────────

const getJobTool: ToolDefinition = {
  name: "ligandai_get_job",
  description: `Poll a LigandAI generation or fold job for status and results.

For generation jobs, returns status + peptide count when complete.
For fold jobs, returns per-peptide metrics: iPSAE, predicted Kd (nM), dG.

**iPSAE tiers:** elite ≥ 0.80 | great+ ≥ 0.66 (primary hit rate) | good ≥ 0.50
**Kd interpretation:** < 1 nM = picomolar | 1-10 nM = strong | 10-100 nM = moderate | 100-1000 nM = weak | > 1000 nM = non-binder
Always report Kd in appropriate units (pM / nM / µM), not raw dG.`,

  inputSchema: {
    type: "object" as const,
    properties: {
      session_id: {
        type: "string",
        description: "Session/job ID returned by design_peptide or submit_fold_job",
      },
      top_n: {
        type: "integer",
        default: 10,
        description: "Return top N results sorted by iPSAE",
      },
    },
    required: ["session_id"],
  },

  handler: async (params: Record<string, unknown>): Promise<string> => {
    const { execSync } = await import("child_process");
    const sid = params.session_id as string;
    const topN = (params.top_n as number) ?? 10;
    const script = `
from ligandai import LigandAI
import json
c = LigandAI()
status = c._transport.request("GET", f"/api/ptf/parallel/${sid}/status") or {}
print(json.dumps({"session_id": "${sid}", "status": status.get("status"), "progress": status.get("progress")}))
`;
    const result = execSync(`python3 -c '${script}'`, {
      env: { ...process.env },
      encoding: "utf-8",
      timeout: 15_000,
    });
    return result.trim();
  },
};

// ─────────────────────────────────────────────────────────────────────────────
// Tool: recommend_linker
// ─────────────────────────────────────────────────────────────────────────────

const recommendLinkerTool: ToolDefinition = {
  name: "ligandai_recommend_linker",
  description: `Get a BLI biotinylation linker recommendation for a candidate peptide.

The server analyses terminus composition and (when a fold job ID is provided)
performs contact-map analysis to determine which end of the peptide contacts
the receptor. The OPPOSITE terminus should be biotinylated to expose the
binding interface on the BLI sensor surface.

Returns: recommended linker, alternatives, binding orientation, and the
generation_constraints dict to pass into the next design run so the generator
avoids placing binding contacts near the tethered terminus.`,

  inputSchema: {
    type: "object" as const,
    properties: {
      sequence: { type: "string", description: "Peptide amino acid sequence" },
      gene: { type: "string", description: "Target gene for context" },
      pdb_job_id: {
        type: "string",
        description: "Fold job ID — enables contact-map-based orientation analysis",
      },
      intended_application: {
        type: "string",
        enum: ["bli_validation", "therapeutic", "conjugation", "research"],
        default: "bli_validation",
      },
    },
    required: ["sequence"],
  },

  handler: async (params: Record<string, unknown>): Promise<string> => {
    const { execSync } = await import("child_process");
    const body = JSON.stringify({
      sequence: params.sequence,
      gene: params.gene,
      pdbJobId: params.pdb_job_id,
      intendedApplication: params.intended_application ?? "bli_validation",
    });
    const script = `
from ligandai import LigandAI
import json
c = LigandAI()
result = c._transport.request("POST", "/api/synthesis-checkout/recommend-linker", json=${JSON.stringify(JSON.parse(body))}) or {}
print(json.dumps(result))
`;
    const result = execSync(`python3 -c '${script}'`, {
      env: { ...process.env },
      encoding: "utf-8",
      timeout: 15_000,
    });
    return result.trim();
  },
};

// ─────────────────────────────────────────────────────────────────────────────
// Tool: estimate_cost
// ─────────────────────────────────────────────────────────────────────────────

const estimateCostTool: ToolDefinition = {
  name: "ligandai_estimate_cost",
  description: "Estimate credit cost before submitting a generation + fold run.",

  inputSchema: {
    type: "object" as const,
    properties: {
      gene: { type: "string" },
      num_peptides: { type: "integer", default: 300 },
      max_folds: { type: "integer", default: 25 },
      include_bli: { type: "boolean", default: false },
      include_deltaforge: { type: "boolean", default: true },
    },
    required: ["gene"],
  },

  handler: async (params: Record<string, unknown>): Promise<string> => {
    const { execSync } = await import("child_process");
    const script = `
from ligandai import LigandAI
c = LigandAI()
est = c.synthesis.estimate_cost(
    gene="${params.gene}",
    num_peptides=${params.num_peptides ?? 300},
    max_folds=${params.max_folds ?? 25},
    include_bli=${params.include_bli ? "True" : "False"},
    include_deltaforge=${params.include_deltaforge !== false ? "True" : "False"},
)
print(f"{est.credits} credits (${"{est.cost_usd:.2f}"})")
`;
    const result = execSync(`python3 -c '${script}'`, {
      env: { ...process.env },
      encoding: "utf-8",
      timeout: 15_000,
    });
    return result.trim();
  },
};

// ─────────────────────────────────────────────────────────────────────────────
// Tool: get_credits
// ─────────────────────────────────────────────────────────────────────────────

const getCreditsTool: ToolDefinition = {
  name: "ligandai_get_credits",
  description: "Get current credit balance, tier, and 30-day burn rate.",

  inputSchema: {
    type: "object" as const,
    properties: {
      include_transactions: {
        type: "boolean",
        default: false,
        description: "Include recent credit transaction history",
      },
    },
    required: [],
  },

  handler: async (): Promise<string> => {
    const { execSync } = await import("child_process");
    const script = `
from ligandai import LigandAI
c = LigandAI()
bal = c.account.get_balance()
print(f"Balance: {bal.credits} credits | Tier: {bal.tier} | Auto-topup: {bal.auto_topup_enabled}")
if bal.burn_rate_30d:
    print(f"Burn rate (30d): {bal.burn_rate_30d} credits/day | Days remaining: {bal.days_remaining:.0f}")
`;
    const result = execSync(`python3 -c '${script}'`, {
      env: { ...process.env },
      encoding: "utf-8",
      timeout: 10_000,
    });
    return result.trim();
  },
};

// ─────────────────────────────────────────────────────────────────────────────
// Tool: adaptyv_submit
// ─────────────────────────────────────────────────────────────────────────────

const adaptyvSubmitTool: ToolDefinition = {
  name: "ligandai_adaptyv_submit",
  description: `Submit top peptide candidates to Adaptyv Foundry for synthesis + BLI affinity validation.

Adaptyv performs solid-phase peptide synthesis (SPPS) and biolayer interferometry
(BLI) to measure actual binding kinetics (kon, koff, KD) against the target protein.

Run ligandai_recommend_linker first to confirm correct biotinylation placement
before submitting.`,

  inputSchema: {
    type: "object" as const,
    properties: {
      experiment_name: { type: "string" },
      gene: { type: "string", description: "Target gene to look up in Adaptyv catalogue" },
      sequences: {
        type: "array",
        items: {
          type: "object",
          properties: {
            name: { type: "string" },
            sequence: { type: "string" },
          },
          required: ["name", "sequence"],
        },
        minItems: 1,
        maxItems: 96,
      },
      include_bli: { type: "boolean", default: true },
    },
    required: ["experiment_name", "gene", "sequences"],
  },

  handler: async (params: Record<string, unknown>): Promise<string> => {
    const { execSync } = await import("child_process");
    const seqs = JSON.stringify(params.sequences);
    const script = `
from ligandai import LigandAI
from ligandai.types import AdaptyvSequence
import json
c = LigandAI()
targets = c.synthesis.adaptyv_search_targets("${params.gene}")
if not targets:
    print("ERROR: No Adaptyv targets found for gene ${params.gene}")
else:
    raw_seqs = json.loads('''${seqs}''')
    seqs = [AdaptyvSequence(name=s["name"], sequence=s["sequence"]) for s in raw_seqs]
    exp = c.synthesis.adaptyv_create(
        name="${params.experiment_name}",
        target_id=targets[0].id,
        sequences=seqs,
        include_bli=${params.include_bli !== false ? "True" : "False"},
    )
    submitted = c.synthesis.adaptyv_submit(exp.id)
    print(f"Submitted experiment {submitted.id} | Status: {submitted.status}")
`;
    const result = execSync(`python3 -c '${script}'`, {
      env: { ...process.env },
      encoding: "utf-8",
      timeout: 30_000,
    });
    return result.trim();
  },
};

// ─────────────────────────────────────────────────────────────────────────────
// Helpers
// ─────────────────────────────────────────────────────────────────────────────

function buildGenerateScript(params: Record<string, unknown>): string {
  const parts: string[] = [
    "from ligandai import LigandAI",
    "from ligandai.types import SegmentConfig, PeptideSegment, PdcConfig, EcTrimmingConfig",
    "c = LigandAI()",
    "kwargs = {}",
  ];

  if (params.segment_config) {
    const sc = params.segment_config as Record<string, unknown>;
    parts.push(`kwargs['segment_config'] = ${JSON.stringify(sc)}`);
  }
  if (params.pdc_config) {
    parts.push(`kwargs['pdc_config'] = ${JSON.stringify(params.pdc_config)}`);
  }
  if (params.ec_trimming) {
    parts.push(`kwargs['ec_trimming_config'] = ${JSON.stringify(params.ec_trimming)}`);
  }
  for (const k of ["quality_guided", "quality_guidance_scale",
                    "charge_mode", "charge_value", "charge_min", "charge_max", "min_solubility",
                    "immunogenicity", "immuno_strength",
                    "serum_stability", "stability_mode", "stability_strength",
                    "glycosylation_enabled", "clash_resolution_enabled", "md_relaxation_enabled",
                    "cyclic_mode", "cysteine_mode", "program_id"]) {
    if (params[k] !== undefined) {
      const v = typeof params[k] === "string" ? `"${params[k]}"` : params[k];
      parts.push(`kwargs['${k}'] = ${v}`);
    }
  }
  if (params.immuno_modules) {
    parts.push(`kwargs['immuno_modules'] = ${JSON.stringify(params.immuno_modules)}`);
  }

  parts.push(`job = c.peptides.generate(
    gene="${params.gene}",
    num_peptides=${params.num_peptides ?? 300},
    length_range=(${params.length_min ?? 20}, ${params.length_max ?? 70}),
    auto_fold=${params.auto_fold !== false ? "True" : "False"},
    max_folds_per_target=${params.max_folds ?? 25},
    num_trajectories=${params.num_trajectories ?? 1},
    sampling_steps=${params.sampling_steps ?? 50},
    fold_strategy="${params.fold_strategy ?? "consolidated"}",
    **kwargs,
)`);
  parts.push(`print(f"Job submitted: {job.session_id}")`);
  return parts.join("\n");
}

// ─────────────────────────────────────────────────────────────────────────────
// Skill export
// ─────────────────────────────────────────────────────────────────────────────

export const ligandaiSkill: Skill = {
  name: "ligandai",
  description:
    "LigandAI peptide design: generate binders, fold complexes, score binding affinity, " +
    "plan BLI linkers, and submit to Adaptyv for synthesis validation.",
  tools: [
    designPeptideTool,
    getJobTool,
    recommendLinkerTool,
    estimateCostTool,
    getCreditsTool,
    adaptyvSubmitTool,
  ],
};

export default ligandaiSkill;
