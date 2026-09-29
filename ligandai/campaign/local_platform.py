# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""``LocalPlatform`` — the campaign engine, backed entirely by our own hardware.

``SDKPlatform`` submits to the hosted API and needs ``LIGANDAI_API_KEY``.  When a
campaign must run on our own machines, that is not an option.  This module is the
fourth :class:`~ligandai.campaign.platform.Platform` implementation:

    generate              LigandForge, loaded from a local checkpoint onto a local
                          CUDA device.  No network.
    fold                  Boltz-2 from a local virtualenv, on a local GPU, with a
                          receptor MSA read off local disk.  No MSA server.
    score_developability  the platform's own stability / immunogenicity scorers,
                          imported in-process.
    score_affinity        DeltaForge v10.2, in-process.

Everything the engine relies on is preserved: the gates, the Wilson
certification, the stop metrics, the ledger and — most importantly — the guards.

⛔ THE GUARDS THIS FILE IS OBLIGED TO KEEP

G7  Identity comes from the STRUCTURE, never from a name.  Every pose is put
    through :func:`~ligandai.campaign.identity.identify_pose` against the
    constructs the spec DECLARES, exactly as ``ReplayPlatform`` does.  An arm
    label never becomes a receptor.

G8  Zero rows is a FAILURE, not a result.  A fold that was asked for N sequences
    and recorded none raises, because the engine cannot distinguish "nothing
    scored" from "nothing ran" and will re-queue forever while paying for it.

(absent != zero != failing)  Every metric is a :class:`Measured`.  A pose with no
    interface contact carries ``Measured.absent``, never 0.0.

Two further rules this module enforces because they are LOCAL failure modes the
hosted platform cannot have:

⛔ AN MSA MUST MATCH ITS RECEPTOR, RESIDUE FOR RESIDUE.  An a3m is applied to a
   sequence positionally.  Point it at a receptor whose query row differs by even
   one residue and every column is shifted: the fold still succeeds, the metrics
   are still produced, and they are garbage.  ``_resolve_msa`` compares the a3m
   query row against the construct sequence and REFUSES on any mismatch.  A
   receptor with no registered MSA is refused outright rather than folded
   MSA-free, because an MSA-free receptor fold produces garbage ipSAE.

⛔ A POCKET IS A SET OF RESIDUES IN A STATED FRAME.  Pocket residue numbers are
   meaningless without the numbering frame of the PDB they index.  This campaign
   alone carries three frames (UniProt P00533, 6ARU author numbering = UniProt -
   24, and a domIII construct index = PDB - 309).  ``PocketSpec`` therefore
   requires the caller to state the frame AND, optionally, the residue letters it
   expects to find there; it verifies both against the structure before a single
   design is generated.  A silent frame error steers generation at the wrong face
   of the protein and is invisible in every downstream number.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from .identity import identify_pose, sha256_seq
from .platform import GeneratedDesign, PoseResult
from .spec import CampaignSpec, Construct, PeptideClass
from .values import Measured, Status

# ---------------------------------------------------------------------------
# Where the local pieces live.  Every one of these is a constructor default, not
# a hard-coded path: the class takes overrides so this module is testable and so
# a second machine with a different layout can use it.
# ---------------------------------------------------------------------------

DEFAULT_BOLTZ_BIN = "/home/dre/boltz2_local/bin/boltz"
DEFAULT_BOLTZ_CACHE = "/home/dre/boltz2_local/models"
DEFAULT_BOLTZ_SITE_PACKAGES = "/home/dre/boltz2_local/lib/python3.11/site-packages"

# ⛔ The ipSAE implementation is PINNED to this copy.  It is the one whose output
# was validated against a real complex: 8C2E scores 0.6934 here, and the campaign
# gate (peptide ipSAE >= 0.66) is calibrated on that scale.  A different copy of
# ipsae_scoring.py is not interchangeable with it, and swapping one in silently
# re-scales every gate in the spec.  `LocalPlatform.selftest()` re-derives the
# 8C2E value and refuses to run if it has moved.
DEFAULT_IPSAE_DIR = ("/mnt/backup/LIGANDAI_ALPHA_V2/archive/"
                     "modal_workers_pre_org_scoping_20260312")
# The newer copy of the SAME scorer. It is NOT the gate source -- it is loaded
# only for ipsae_d0ref / d0res_clamped_frac / ipae, which the pinned copy
# predates, and every use cross-checks that both still agree on ipsae_d0res.
DEFAULT_IPSAE_DIAGNOSTIC = "/mnt/backup/LIGANDAI_ALPHA_V2/modal_workers/ipsae_scoring.py"

IPSAE_REFERENCE = {
    "pdb": ("/home/dre/research_work/boltz_ref_v1/fold1/pred/boltz_results_in/"
            "predictions/8C2E/8C2E_model_0.pdb"),
    "pae": ("/home/dre/research_work/boltz_ref_v1/fold1/pred/boltz_results_in/"
            "predictions/8C2E/pae_8C2E_model_0.npz"),
    "pair": "A-B",
    "expected": 0.6934,
    "tolerance": 0.002,
}

DEFAULT_LF_ROOT = "/home/dre/egfr_ph_campaign/gen/lfroot"
DEFAULT_DELTA_NODE = "/mnt/backup/LIGANDAI_ALPHA_V2/delta_node"
DEFAULT_FASTAPI_BACKEND = "/mnt/backup/LIGANDAI_ALPHA_V2/fastapi_backend"

# ⛔ ligandgen_v6.5_final.pt is POCKET-BLIND — its cross-attention entropy is
# exactly 100.0000% of ln(205) and destroying the pocket moves the logits by
# 0.097%.  Generating on it produces designs that are not conditioned on the
# target at all, which no downstream gate can detect.  v6.5.21 is
# pocket-responsive (10.12 sigma between-pocket separation against a
# within-pocket noise floor of 6.05 +- 0.85 L1 over 5 replicates).
DEFAULT_LF_MODEL_ID = "v6.5.21"
POCKET_BLIND_CHECKPOINTS = {"ligandgen_v6.5_final", "v6.5_final", "v6.5-final"}

CANONICAL_AA = frozenset("ACDEFGHIKLMNPQRSTVWY")

_AA3 = {
    "ALA": "A", "ARG": "R", "ASN": "N", "ASP": "D", "CYS": "C", "GLN": "Q",
    "GLU": "E", "GLY": "G", "HIS": "H", "ILE": "I", "LEU": "L", "LYS": "K",
    "MET": "M", "PHE": "F", "PRO": "P", "SER": "S", "THR": "T", "TRP": "W",
    "TYR": "Y", "VAL": "V", "MSE": "M", "HIE": "H", "HID": "H", "HIP": "H",
    "CYX": "C", "SEC": "U", "PYL": "O",
}


def _noop(*_a: Any, **_k: Any) -> None:
    return None


# ---------------------------------------------------------------------------
# MSA registry
# ---------------------------------------------------------------------------

@dataclass
class MSAEntry:
    """One receptor alignment, bound to the exact sequence it aligns."""

    path: str
    n_rows: int
    query: str

    @property
    def sha256(self) -> str:
        return sha256_seq(self.query)


def read_a3m_query(path: str) -> tuple[str, int]:
    """The query row of an a3m, and the alignment depth.

    The query is the FIRST record with insertions (lower-case) stripped — that is
    the row Boltz aligns the receptor against, so it is the row that must equal
    the construct.
    """
    query_parts: list[str] = []
    n_rows = 0
    started = False
    done = False
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.startswith("#"):
                continue
            if line.startswith(">"):
                n_rows += 1
                if started:
                    done = True
                started = True
                continue
            if started and not done:
                query_parts.append(line.strip())
    query = "".join(query_parts)
    # a3m insertions are lower-case and are NOT part of the query.
    query = "".join(c for c in query if not c.islower()).replace("-", "")
    return query.upper(), n_rows


class MSARegistry:
    """Receptor MSAs, keyed by the SHA-256 of the sequence they actually align.

    Keying on the SEQUENCE HASH rather than on a construct name is the same
    discipline as G7: a name is metadata, the sequence is identity.  Registering
    an a3m whose query row differs from the sequence you claim it covers raises
    HERE, before a fold, rather than producing a positionally-shifted alignment
    that quietly corrupts every metric downstream.
    """

    def __init__(self) -> None:
        self._by_hash: dict[str, MSAEntry] = {}
        self._names: dict[str, str] = {}      # friendly name -> hash

    def register(self, name: str, sequence: str, a3m_path: str) -> MSAEntry:
        if not os.path.exists(a3m_path):
            raise FileNotFoundError(f"MSA for {name!r} not found: {a3m_path}")
        query, n_rows = read_a3m_query(a3m_path)
        want = "".join(sequence.split()).upper()
        if query != want:
            # Say exactly HOW it differs.  "MSA mismatch" sends someone hunting;
            # the first differing position and the two lengths do not.
            diff_at = next((i for i, (a, b) in enumerate(zip(query, want)) if a != b),
                           min(len(query), len(want)))
            raise ValueError(
                f"MSA {a3m_path} does not match construct {name!r}: a3m query is "
                f"{len(query)} aa, construct is {len(want)} aa, first difference at "
                f"position {diff_at + 1} (a3m {query[diff_at:diff_at + 8]!r} vs "
                f"construct {want[diff_at:diff_at + 8]!r}). An a3m is applied "
                f"POSITIONALLY — folding with this alignment would shift every "
                f"column and produce metrics that look normal and mean nothing."
            )
        entry = MSAEntry(path=a3m_path, n_rows=n_rows, query=query)
        self._by_hash[entry.sha256] = entry
        self._names[name] = entry.sha256
        return entry

    def get(self, sequence: str) -> MSAEntry | None:
        return self._by_hash.get(sha256_seq(sequence))

    def summary(self) -> list[dict[str, Any]]:
        out = []
        for name, h in sorted(self._names.items()):
            e = self._by_hash[h]
            out.append({"construct": name, "a3m": e.path, "rows": e.n_rows,
                        "query_len": len(e.query), "receptor_sha256": h})
        return out


# ---------------------------------------------------------------------------
# Pocket
# ---------------------------------------------------------------------------

@dataclass
class PocketSpec:
    """The residues generation is steered at, in a STATED numbering frame.

    ``expect_residues`` is the load-bearing field.  Residue numbers alone cannot
    tell you whether they were meant in UniProt, author, or construct-index
    numbering, and every one of those frames exists in this campaign.  Giving the
    one-letter codes you expect to find turns an invisible frame error into an
    immediate, specific refusal.
    """

    pdb_path: str
    residue_numbers: Sequence[int]
    frame: str                                  # human-readable, recorded
    chain_id: str = "A"
    expect_residues: str | None = None          # e.g. "DEHEDE"
    # ⛔ None means "ask the model registry", and that is the right default.
    # 48 / 65 / 107 are three DIFFERENT vectors, not three lengths of one, and
    # each checkpoint consumes exactly one of them.  Pinning a width here would
    # let a spec change the model and silently keep feeding the old vector.
    want_dim: int | None = None
    gene: str | None = None

    def verify(self) -> dict[str, Any]:
        """Check the residues exist, and are what the caller said they are."""
        found: dict[int, str] = {}
        with open(self.pdb_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not line.startswith("ATOM  ") or len(line) < 27:
                    continue
                if line[21] != self.chain_id:
                    continue
                try:
                    num = int(line[22:26])
                except ValueError:
                    continue
                if num in self.residue_numbers and num not in found:
                    found[num] = _AA3.get(line[17:20].strip().upper(), "X")
        missing = [n for n in self.residue_numbers if n not in found]
        if missing:
            raise ValueError(
                f"pocket residues {missing} are absent from chain {self.chain_id} "
                f"of {self.pdb_path}. Declared frame: {self.frame}. This is a "
                f"NUMBERING FRAME ERROR — the residue numbers are being read in a "
                f"frame the structure does not use."
            )
        got = "".join(found[n] for n in self.residue_numbers)
        if self.expect_residues and got != self.expect_residues.upper():
            raise ValueError(
                f"pocket residues in {self.pdb_path} chain {self.chain_id} at "
                f"{list(self.residue_numbers)} are {got!r}, but the caller declared "
                f"{self.expect_residues.upper()!r}. Declared frame: {self.frame}. "
                f"The numbers resolve to the WRONG RESIDUES — generation would be "
                f"steered at a different face of the protein, and no downstream "
                f"metric can detect it."
            )
        return {"frame": self.frame, "chain_id": self.chain_id,
                "residue_numbers": list(self.residue_numbers),
                "residues_found": got, "pdb_path": self.pdb_path,
                "verified": True}


# ---------------------------------------------------------------------------
# Local LigandForge
# ---------------------------------------------------------------------------

_LF_ENGINE_CACHE: dict[tuple, Any] = {}


class LocalLigandForge:
    """LigandForge, loaded from a local checkpoint onto a local CUDA device."""

    def __init__(self, *, lf_root: str = DEFAULT_LF_ROOT,
                 delta_node: str = DEFAULT_DELTA_NODE,
                 model_id: str = DEFAULT_LF_MODEL_ID,
                 device: str = "cuda", verify: bool = False,
                 progress: Callable[..., None] = _noop) -> None:
        base = os.path.basename(str(model_id)).replace(".pt", "")
        if base in POCKET_BLIND_CHECKPOINTS:
            raise ValueError(
                f"refusing to generate on checkpoint {model_id!r}: it is "
                f"POCKET-BLIND (cross-attention entropy exactly 100.0000% of "
                f"ln(205); destroying the pocket moves the logits 0.097%). "
                f"Designs from it are not conditioned on the target at all, and "
                f"no gate downstream can detect that. Use {DEFAULT_LF_MODEL_ID}."
            )
        self.lf_root = lf_root
        self.delta_node = delta_node
        self.model_id = model_id
        self.device = device
        self.verify = verify
        self.progress = progress
        self._engine = None
        self.identity: Any = None
        self._spec_row: Any = None
        self._calls = 0
        self.cfg_weight: float | None = None

    # -- the registry is the single source of truth for a model's inputs ----
    def registry_row(self):
        """The delta registry's row for this model id.

        ``pocket_feature_dim`` and ``requires_cfg`` are properties OF THE
        CHECKPOINT.  Reading them from the registry rather than from a caller's
        argument is what stops a model change from silently keeping the previous
        model's feature vector.
        """
        if self._spec_row is not None:
            return self._spec_row
        self._install_paths()
        import lf_registry as R                                # noqa: PLC0415
        row = R.MODELS.get(self.model_id)
        if row is None:
            raise ValueError(
                f"model_id {self.model_id!r} is not in the local registry "
                f"({sorted(R.MODELS)}). Its checkpoint, feature width and CFG "
                f"requirement are unknown, so nothing here can be assumed.")
        path = row.path
        if not os.path.exists(path):
            raise FileNotFoundError(
                f"model {self.model_id!r} needs checkpoint {path}, which is not "
                f"on this host. It has NOT been substituted with another file — "
                f"a v6.9 checkpoint and a v6.5 checkpoint consume different "
                f"pocket vectors and are not interchangeable.")
        self._spec_row = row
        return row

    @property
    def pocket_feature_dim(self) -> int:
        return int(self.registry_row().pocket_feature_dim)

    @property
    def requires_cfg(self) -> bool:
        return bool(getattr(self.registry_row(), "requires_cfg", False))

    def _install_paths(self) -> None:
        os.environ.setdefault("LF_ROOT", self.lf_root)
        os.environ["LF_ROOT"] = self.lf_root
        os.environ["DELTA_LF_MODEL_ID"] = str(self.model_id)
        os.environ["DELTA_LF_DEVICE"] = self.device
        os.environ["DELTA_LF_VERIFY"] = "1" if self.verify else "0"
        for p in (self.delta_node, os.path.join(self.lf_root, "modules"),
                  os.path.join(self.lf_root, "runner")):
            if p not in sys.path:
                sys.path.insert(0, p)

    def engine(self):
        key = (self.lf_root, self.model_id, self.device)
        if key in _LF_ENGINE_CACHE:
            self._engine = _LF_ENGINE_CACHE[key]
            return self._engine
        self._install_paths()
        from engines import build_engine                       # noqa: PLC0415
        t0 = time.time()
        self.progress(f"LigandForge: building {self.model_id} on {self.device}")
        eng = build_engine("ligandforge",
                           {"model_id": self.model_id, "device": self.device,
                            "verify": self.verify})
        self.identity = eng.load()
        self.progress(f"LigandForge: loaded {getattr(self.identity, 'model_id', self.identity)} "
                      f"in {time.time() - t0:.1f}s")

        # ⛔ A v6.9 CHECKPOINT SAMPLED AT CFG WEIGHT 0 IS POCKET-BLIND AGAIN.
        #
        # This is the same failure as the deprecated v6.5_final checkpoint, but
        # arrived at from the other direction: the weights are fine, and the
        # SAMPLER throws the conditioning away. The registry's own note is
        # explicit — "REQUIRES CFG at the weight embedded in the checkpoint;
        # sampling at w=0 silently reproduces receptor-blind-looking output."
        # Silently is the whole problem: the run completes, the designs look
        # like peptides, and nothing downstream can tell they were not
        # conditioned on the target. Check it once, at load, and refuse.
        if self.requires_cfg:
            w = self._guidance_weight(eng)
            if w is None:
                self.progress(
                    f"  ⛔ WARNING: {self.model_id} requires CFG and the loaded "
                    f"model exposes no guidance_weight to verify. Proceeding, "
                    f"but the conditioning is UNVERIFIED for this run.")
            elif float(w) == 0.0:
                raise RuntimeError(
                    f"refusing to generate: {self.model_id} requires CFG and the "
                    f"loaded model reports guidance_weight=0.0. At w=0 the "
                    f"sampler discards the pocket conditioning entirely and "
                    f"silently produces receptor-blind output — the same defect "
                    f"as the deprecated pocket-blind checkpoint, reached through "
                    f"the sampler instead of the weights.")
            else:
                self.cfg_weight = float(w)
                self.progress(f"  CFG active: guidance_weight={self.cfg_weight}")
        _LF_ENGINE_CACHE[key] = eng
        self._engine = eng
        return eng

    @staticmethod
    def _guidance_weight(eng: Any) -> float | None:
        """The CFG weight the resident model will actually sample at."""
        res = getattr(eng, "_res", None)
        meta = getattr(res, "meta", None)
        if isinstance(meta, Mapping):
            for k in ("guidance_weight", "cfg_weight", "guidance_scale"):
                if meta.get(k) is not None:
                    return float(meta[k])
        return None

    def pocket_features(self, pocket: PocketSpec,
                        fastapi_backend: str = DEFAULT_FASTAPI_BACKEND
                        ) -> tuple[list[list[float]], dict[str, Any]]:
        """Per-residue pocket features over EXACTLY the residues asked for.

        ``resolve_pocket_features`` auto-detects the largest geometric pocket when
        it is given no residues.  That is the wrong pocket for a campaign that
        names its epitope: the auto-detected pocket on this target is a 14-residue
        cluster at 372-430, not the six-residue acidic face the spec declares.
        Passing ``residue_numbers`` is what makes the steering real, so this
        method requires them and verifies the count that comes back.
        """
        verification = pocket.verify()
        if fastapi_backend not in sys.path:
            sys.path.insert(0, fastapi_backend)
        from core.pocket_featurizer import resolve_pocket_features   # noqa: PLC0415
        pdb_content = open(pocket.pdb_path, "r", encoding="utf-8",
                           errors="replace").read()
        want_dim = pocket.want_dim or self.pocket_feature_dim
        if pocket.want_dim and pocket.want_dim != self.pocket_feature_dim:
            raise ValueError(
                f"pocket.want_dim={pocket.want_dim} but model {self.model_id} "
                f"consumes {self.pocket_feature_dim}-dim features. These are "
                f"DIFFERENT vectors, not different lengths of one — 65 is not a "
                f"prefix of 107 and neither contains 48.")
        feats, prov = resolve_pocket_features(
            want_dim=want_dim, pdb_content=pdb_content,
            chain_id=pocket.chain_id,
            residue_numbers=list(pocket.residue_numbers), gene=pocket.gene)
        n_want = len(pocket.residue_numbers)
        if len(feats) != n_want:
            raise RuntimeError(
                f"asked the featurizer for {n_want} pocket residues "
                f"{list(pocket.residue_numbers)} and it returned {len(feats)} rows. "
                f"The pocket was NOT narrowed to the declared epitope; generation "
                f"would be steered at whatever it picked instead. Featurizer "
                f"provenance: {json.dumps(prov, default=str)[:400]}"
            )
        prov = dict(prov or {})
        prov["pocket_verification"] = verification
        self.progress(f"pocket: {len(feats)} residues x {len(feats[0])} dims "
                      f"({verification['residues_found']} at "
                      f"{list(pocket.residue_numbers)}, frame: {pocket.frame})")
        return [list(map(float, row)) for row in feats], prov

    def generate(self, features: Sequence[Sequence[float]], n: int,
                 length_range: tuple[int, int], *, temperature: float = 0.9,
                 top_p: float = 0.92, gene: str | None = None,
                 seed: int | None = None, n_bins: int = 4,
                 job_id: str | None = None,
                 reference_features: Sequence[Sequence[Sequence[float]]] | None = None,
                 ) -> tuple[list[str], dict[str, Any]]:
        """``n`` sequences spread across ``length_range``.

        ⛔ LENGTH IS SAMPLED PER BATCH.  One call over a wide range collapses to a
        narrow band — the hosted platform has produced 1,300 designs at a single
        length this way, and ``SDKPlatform`` raises on it.  Asking in bins is how
        a local driver gets the range the spec actually declares; the bins are
        recorded so the batch composition can be audited rather than assumed.
        """
        lo, hi = int(length_range[0]), int(length_range[1])
        if hi < lo:
            raise ValueError(f"length_range {length_range} is inverted")
        eng = self.engine()
        span = hi - lo + 1
        k = max(1, min(int(n_bins), span))
        edges = [lo + round(i * span / k) for i in range(k)] + [hi + 1]
        bins = [(edges[i], edges[i + 1] - 1) for i in range(k) if edges[i] <= edges[i + 1] - 1]
        per = max(1, math.ceil(n / max(1, len(bins))))

        out: list[str] = []
        seen: set[str] = set()
        bin_report: list[dict[str, Any]] = []
        for bi, (blo, bhi) in enumerate(bins):
            req: dict[str, Any] = {
                "pocket_features": [list(r) for r in features],
                "num_peptides": per,
                "length_range": [blo, bhi],
                "temperature": temperature,
                "top_p": top_p,
            }
            # ⛔ THE CFG NEGATIVE MUST BE A REAL PARALOG POCKET, NOT ZEROS.
            #
            # The v6.9 line samples with classifier-free guidance, so it needs an
            # UNCONDITIONAL reference to push away from. The checkpoint's
            # historical reference was an all-zero receptor, and the runtime now
            # refuses it (H21) for the reason this whole module keeps running
            # into: guidance against a zero receptor is arithmetically identical
            # to no receptor conditioning at all, and the output is
            # receptor-blind while looking completely normal.
            #
            # Supplying the campaign's DECLARED off-target as the negative is
            # not a workaround for that refusal — it is the thing the spec is
            # asking for. Steering away from the paralog's version of the same
            # epitope is exactly the `min_margin` the stop metrics demand.
            if self.requires_cfg:
                if not reference_features:
                    raise ValueError(
                        f"{self.model_id} samples with CFG and requires at least "
                        f"one REAL off-target/paralog pocket as the negative "
                        f"reference. None was supplied. Declare an off_target "
                        f"construct with a structure and pocket residues; an "
                        f"all-zero reference is prohibited because it silently "
                        f"reproduces receptor-blind output.")
                req["reference_pocket_features"] = [
                    [list(r) for r in ref] for ref in reference_features]
            elif reference_features:
                # v6.5 REJECTS references rather than ignoring them; say so here
                # rather than letting the runtime raise from three frames down.
                raise ValueError(
                    f"{self.model_id} does not consume reference_pocket_features, "
                    f"but {len(reference_features)} were supplied. Refusing to "
                    f"silently drop receptor features.")
            if gene:
                req["gene"] = gene
            # ⛔ THE SEED MUST MOVE ON EVERY CALL, OR v6.9.x REPEATS ITSELF.
            #
            # The v6.9 line is DETERMINISTIC at a fixed seed — the same request
            # returns the identical sequences every time. The campaign engine
            # comes round the loop and asks for another N designs of the same
            # class against the same pocket, which is byte-for-byte the same
            # request; without a moving seed every iteration after the first
            # regenerates the designs it already has, the dedup drops them, and
            # the campaign stalls on no_progress while paying for each round.
            # A caller-supplied seed is honoured as the BASE, never as the whole
            # seed.
            self._calls += 1
            req["seed"] = ((int(seed) if seed is not None else 0)
                           + self._calls * 1000 + bi)
            if job_id:
                req["job_id"] = f"{job_id}-{blo}-{bhi}"
            t0 = time.time()
            res = eng.generate(req, lambda: False)
            raw = res.get("peptides") or res.get("sequences") or []
            got = 0
            for p in raw:
                s = p.get("sequence") if isinstance(p, dict) else p
                if not s:
                    continue
                s = str(s).strip().upper()
                # Boltz folds canonical sequences only; a UAA-containing design
                # would drop out at fold time while still counting as a proposal.
                if not s or not set(s) <= CANONICAL_AA:
                    continue
                if not (lo <= len(s) <= hi):
                    continue
                if s in seen:
                    continue
                seen.add(s)
                out.append(s)
                got += 1
            bin_report.append({"bin": [blo, bhi], "asked": per, "kept": got,
                               "seconds": round(time.time() - t0, 1)})
            self.progress(f"  gen bin {blo}-{bhi}: {got} kept "
                          f"({time.time() - t0:.1f}s)")
        prov = {"model_id": self.model_id, "device": self.device,
                "pocket_feature_dim": self.pocket_feature_dim,
                "requires_cfg": self.requires_cfg,
                "cfg_weight": self.cfg_weight,
                "n_cfg_reference_pockets": len(reference_features or ()),
                "length_bins": bin_report, "temperature": temperature,
                "top_p": top_p, "requested": n, "returned": len(out),
                "engine_identity": str(getattr(self.identity, "model_id", self.identity))}
        return out[:n], prov


# ---------------------------------------------------------------------------
# Local Boltz-2
# ---------------------------------------------------------------------------

@dataclass
class FoldOutputs:
    tag: str
    pdb_path: str
    confidence: dict[str, Any]
    pae_path: str | None
    n_samples: int


class LocalBoltz:
    """Boltz-2 from a local virtualenv, on a local GPU, with a local MSA."""

    def __init__(self, *, boltz_bin: str = DEFAULT_BOLTZ_BIN,
                 cache: str = DEFAULT_BOLTZ_CACHE,
                 site_packages: str = DEFAULT_BOLTZ_SITE_PACKAGES,
                 device_index: int = 0,
                 max_receptor_residues: int | None = None,
                 num_workers: int = 2, preprocessing_threads: int = 8,
                 timeout_per_design: float = 900.0,
                 progress: Callable[..., None] = _noop) -> None:
        if not os.path.exists(boltz_bin):
            raise FileNotFoundError(f"boltz not found at {boltz_bin}")
        self.boltz_bin = boltz_bin
        self.cache = cache
        self.site_packages = site_packages
        self.device_index = device_index
        self.max_receptor_residues = max_receptor_residues
        self.num_workers = num_workers
        self.preprocessing_threads = preprocessing_threads
        self.timeout_per_design = timeout_per_design
        self.progress = progress

    # -- environment ----------------------------------------------------
    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        # ⛔ Without these the CLI prints
        #   "Error while loading libcue_ops.so: libnvrtc.so.12: cannot open ..."
        # and silently falls back off its fused kernels.
        libs = [os.path.join(self.site_packages, p) for p in
                ("nvidia/cuda_nvrtc/lib", "nvidia/cublas/lib",
                 "nvidia/cuda_runtime/lib")]
        env["LD_LIBRARY_PATH"] = os.pathsep.join(
            [p for p in libs if os.path.isdir(p)] +
            ([env["LD_LIBRARY_PATH"]] if env.get("LD_LIBRARY_PATH") else []))
        env["CUDA_VISIBLE_DEVICES"] = str(self.device_index)
        env["BOLTZ_CACHE"] = self.cache
        return env

    def free_vram_mib(self) -> int | None:
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.total,memory.used",
                 "--format=csv,noheader,nounits", "-i", str(self.device_index)],
                capture_output=True, text=True, timeout=20, check=True).stdout
            total, used = (int(x.strip()) for x in out.strip().splitlines()[0].split(","))
            return total - used
        except Exception:                                      # noqa: BLE001
            return None

    def check_capacity(self, construct: Construct) -> None:
        """Refuse a fold this GPU cannot hold, by NAME and by NUMBER.

        Silently OOMing halfway through a batch looks, from the engine's side,
        exactly like "these designs did not score" — it re-queues and pays again.
        """
        n = len(construct.sequence)
        if self.max_receptor_residues is not None and n > self.max_receptor_residues:
            free = self.free_vram_mib()
            raise RuntimeError(
                f"construct {construct.name!r} is {n} aa, beyond the "
                f"{self.max_receptor_residues} aa this local GPU "
                f"(cuda:{self.device_index}, {free} MiB free) is configured to "
                f"fold. This arm needs a GPU with more VRAM and a Boltz install "
                f"on it. It has NOT been folded, and no number has been invented "
                f"for it — raise max_receptor_residues only if the VRAM is "
                f"actually there."
            )

    # -- yaml -----------------------------------------------------------
    @staticmethod
    def write_yaml(path: str, receptor: str, peptide: str, msa_path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("version: 1\nsequences:\n"
                     f"  - protein:\n      id: A\n      sequence: {receptor}\n"
                     f"      msa: {msa_path}\n"
                     f"  - protein:\n      id: B\n      sequence: {peptide}\n"
                     "      msa: empty\n")

    # -- run ------------------------------------------------------------
    def predict(self, yaml_dir: str, out_dir: str, *, diffusion_samples: int,
                sampling_steps: int, recycling_steps: int, use_potentials: bool,
                seed: int, n_designs: int) -> tuple[int, str]:
        os.makedirs(out_dir, exist_ok=True)
        cmd = [self.boltz_bin, "predict", yaml_dir,
               "--out_dir", out_dir,
               "--cache", self.cache,
               "--diffusion_samples", str(diffusion_samples),
               "--sampling_steps", str(sampling_steps),
               "--recycling_steps", str(recycling_steps),
               "--output_format", "pdb",
               "--seed", str(seed),
               "--num_workers", str(self.num_workers),
               "--preprocessing-threads", str(self.preprocessing_threads),
               "--override"]
        # ⛔ --use_potentials is a FLAG, not a boolean option.  Passing
        # "--use_potentials false" makes click read "false" as the DATA argument
        # and the real input path becomes an unexpected extra arg.  It is either
        # present (on) or absent (off) — never present with a value.
        if use_potentials:
            cmd.append("--use_potentials")
        log_path = os.path.join(out_dir, "boltz.log")
        timeout = max(600.0, self.timeout_per_design * max(1, n_designs))
        self.progress(f"  boltz: {n_designs} designs, {diffusion_samples} samples, "
                      f"seed {seed}, potentials={'on' if use_potentials else 'off'}, "
                      f"cuda:{self.device_index} ({self.free_vram_mib()} MiB free)")
        t0 = time.time()
        with open(log_path, "a", encoding="utf-8") as log:
            log.write(f"\n=== {time.strftime('%FT%T')} {' '.join(cmd)}\n")
            log.flush()
            rc = subprocess.run(cmd, env=self._env(), stdout=log,
                                stderr=subprocess.STDOUT, timeout=timeout).returncode
        self.progress(f"  boltz: rc={rc} in {time.time() - t0:.0f}s")
        return rc, log_path

    @staticmethod
    def collect(out_dir: str, yaml_dir: str) -> dict[str, FoldOutputs]:
        """Map design tag -> its top-ranked model and confidence."""
        name = os.path.basename(os.path.normpath(yaml_dir))
        pred = os.path.join(out_dir, f"boltz_results_{name}", "predictions")
        found: dict[str, FoldOutputs] = {}
        if not os.path.isdir(pred):
            return found
        for tag in sorted(os.listdir(pred)):
            d = os.path.join(pred, tag)
            pdb = os.path.join(d, f"{tag}_model_0.pdb")
            if not os.path.exists(pdb):
                continue
            conf_path = os.path.join(d, f"confidence_{tag}_model_0.json")
            conf: dict[str, Any] = {}
            if os.path.exists(conf_path):
                try:
                    conf = json.load(open(conf_path, encoding="utf-8"))
                except Exception:                              # noqa: BLE001
                    conf = {}
            pae = os.path.join(d, f"pae_{tag}_model_0.npz")
            n_samples = len([f for f in os.listdir(d)
                             if re.match(rf"{re.escape(tag)}_model_\d+\.pdb$", f)])
            found[tag] = FoldOutputs(tag=tag, pdb_path=pdb, confidence=conf,
                                     pae_path=pae if os.path.exists(pae) else None,
                                     n_samples=n_samples)
        return found


# ---------------------------------------------------------------------------
# Structure readers + the metrics
# ---------------------------------------------------------------------------

def parse_pdb_for_ipsae(pdb_path: str):
    """CB (CA for GLY) rows: chain, pLDDT, coordinate, residue name.

    Boltz writes per-residue pLDDT on the 0-100 scale into the B-factor column;
    the ipSAE implementation's pDockQ term expects that scale.
    """
    import numpy as np                                          # noqa: PLC0415
    ch: list[str] = []
    pl: list[float] = []
    cb: list[list[float]] = []
    rt: list[str] = []
    with open(pdb_path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.startswith("ATOM  "):
                continue
            atom = line[12:16].strip()
            res = line[17:20].strip()
            if atom == "CB" or (res == "GLY" and atom == "CA"):
                ch.append(line[21])
                rt.append(res)
                pl.append(float(line[60:66]))
                cb.append([float(line[30:38]), float(line[38:46]), float(line[46:54])])
    return (np.array(ch), np.array(pl), np.array(cb), np.array(rt))


def pdb_chain_ids(pdb_path: str) -> set[str]:
    chains: set[str] = set()
    with open(pdb_path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.startswith(("ATOM  ", "HETATM")) and len(line) > 21:
                c = line[21].strip()
                if c:
                    chains.add(c)
    return chains


def interface_plddt(pdb_path: str, peptide_chain: str, receptor_chain: str,
                    cutoff: float = 4.5) -> tuple[float | None, int]:
    """Mean pLDDT over PEPTIDE residues in contact with the receptor.

    Returns ``(None, 0)`` when nothing is in contact.  That is ABSENT, not zero:
    a pose with no interface has no interface pLDDT, and recording 0.0 would put
    it in the same bucket as a real, very bad interface.
    """
    import numpy as np                                          # noqa: PLC0415
    pep: list[tuple[int, float, float, float, float]] = []
    rec: list[tuple[float, float, float]] = []
    with open(pdb_path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.startswith("ATOM  ") or len(line) < 67:
                continue
            if line[76:78].strip() == "H":
                continue
            c = line[21]
            xyz = (float(line[30:38]), float(line[38:46]), float(line[46:54]))
            if c == peptide_chain:
                pep.append((int(line[22:26]), float(line[60:66]), *xyz))
            elif c == receptor_chain:
                rec.append(xyz)
    if not pep or not rec:
        return None, 0
    R = np.asarray(rec)
    per_res: dict[int, float] = {}
    hits: set[int] = set()
    for num, plddt, x, y, z in pep:
        per_res[num] = plddt
        if num in hits:
            continue
        d2 = ((R[:, 0] - x) ** 2 + (R[:, 1] - y) ** 2 + (R[:, 2] - z) ** 2).min()
        if d2 <= cutoff * cutoff:
            hits.add(num)
    if not hits:
        return None, 0
    return float(sum(per_res[n] for n in hits) / len(hits)), len(hits)


class IPSAEScorer:
    """peptide ipSAE — the binder metric, from the pinned implementation.

    ⛔ THE PEPTIDE METRIC IS THE PAIR VALUE, NOT ``overall_ipsae``.  For a
    two-chain complex the two coincide, so an implementation that reads the
    overall value looks correct right up until a receptor arrives with more than
    one chain — at which point the best of ALL pairs, including receptor-internal
    ones, silently becomes the "binder" score.  This reads
    ``chain_pairs[<receptor>-<peptide>]["ipsae_d0res"]`` and nothing else.
    """

    def __init__(self, ipsae_dir: str = DEFAULT_IPSAE_DIR,
                 diagnostic_path: str | None = DEFAULT_IPSAE_DIAGNOSTIC) -> None:
        if ipsae_dir not in sys.path:
            sys.path.insert(0, ipsae_dir)
        import ipsae_scoring                                    # noqa: PLC0415
        self.mod = ipsae_scoring
        self.ipsae_dir = ipsae_dir
        # ⛔ THE GATE VALUE STILL COMES FROM THE PINNED COPY. The diagnostic copy
        # is loaded ONLY to compute terms the pinned one predates, and every call
        # cross-checks that the two still agree on ipsae_d0res itself. Measured
        # 2026-09-29: bit-identical (max |diff| = 0.0) on 8C2E and on 12 campaign
        # poses. If either copy is ever edited and they drift, that is a silent
        # re-scaling of every gate calibrated on this metric, so it raises.
        #
        # Both files are named ipsae_scoring.py, so the diagnostic one is loaded
        # by PATH under its own module name -- a plain import returns whichever
        # is already in sys.modules, which is the bug that hid this comparison
        # the first time it was attempted.
        self.diag = None
        self.diagnostic_path = diagnostic_path
        if diagnostic_path and os.path.exists(diagnostic_path):
            import importlib.util                               # noqa: PLC0415
            spec = importlib.util.spec_from_file_location(
                "_ipsae_diagnostic", diagnostic_path)
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                try:
                    spec.loader.exec_module(mod)
                    self.diag = mod
                except Exception:                               # noqa: BLE001
                    self.diag = None

    def score(self, pdb_path: str, pae_path: str, *, peptide_chain: str = "B",
              receptor_chain: str = "A") -> dict[str, Any]:
        import numpy as np                                      # noqa: PLC0415
        pae = np.load(pae_path)["pae"]
        ch, pl, cb, rt = parse_pdb_for_ipsae(pdb_path)
        n = min(len(ch), pae.shape[0])
        if n == 0:
            return {"error": "no CB rows parsed"}
        if len(ch) != pae.shape[0]:
            # Truncating a PAE matrix against a different residue count silently
            # mis-pairs residues.  Say it rather than scoring it.
            return {"error": f"residue/PAE mismatch: {len(ch)} CB rows vs "
                             f"{pae.shape[0]}x{pae.shape[1]} PAE"}
        res = self.mod.calculate_ipsae_scores(pae[:n, :n], ch[:n], pl[:n], cb[:n], rt[:n])
        pairs = res.get("chain_pairs") or {}
        key = f"{receptor_chain}-{peptide_chain}"
        pair = pairs.get(key) or pairs.get(f"{peptide_chain}-{receptor_chain}")
        if not isinstance(pair, dict):
            return {"error": f"no {key} chain pair in ipSAE result "
                             f"(pairs: {sorted(pairs)})"}
        out = {"peptide_ipsae": pair.get("ipsae_d0res"),
               "ipsae_d0ref": pair.get("ipsae_d0ref"),
               "d0res_clamped_frac": pair.get("d0res_clamped_frac"),
               "ipae": pair.get("ipae"), "pdockq": pair.get("pdockq"),
               "pdockq2": pair.get("pdockq2"), "lis": pair.get("lis"),
               "n_contacts": pair.get("n_contacts"),
               "overall_ipsae": res.get("overall_ipsae"), "pair_key": key}
        if out["d0res_clamped_frac"] is None and self.diag is not None:
            out.update(self._diagnostics(pdb_path, pae, peptide_chain, out))
        return out

    def _diagnostics(self, pdb_path: str, pae: Any,
                     peptide_chain: str, pinned: Mapping[str, Any]) -> dict[str, Any]:
        """``ipsae_d0ref`` and ``d0res_clamped_frac``, which the pinned copy predates.

        ⛔ WHY THIS IS WORTH COMPUTING. The scorer's own note: ``ipsae_d0ref`` is
        the term to use "for any absolute threshold or any cross-length ranking",
        and a ``d0res_clamped_frac`` near 1.0 makes an absolute gate on
        ``ipsae_d0res`` MEANINGLESS for that complex.

        A campaign gate like ``peptide_ipsae_median >= 0.66`` is exactly an
        absolute threshold, and a spec with several peptide-length classes ranks
        across lengths. Measured on this campaign's first 12 poses: every peptide
        of 23 aa or shorter came back FULLY clamped (frac 1.000) while the 28-29
        aa ones were barely clamped (0.034-0.213) -- so the clamp, and therefore
        the comparability of the gate, tracks peptide length.

        Recording it does not change any gate. It makes the confound VISIBLE in
        the campaign record instead of invisible, which is the only thing that
        lets someone decide about it on evidence.
        """
        try:
            r = self.diag.score_boltz2_prediction(
                pdb_content=open(pdb_path, encoding="utf-8").read(),
                cif_content=None, pae_matrix=pae, plddt=None,
                peptide_chain_ids=[peptide_chain])
            pep = r.get("peptide") or {}
        except Exception as exc:                               # noqa: BLE001
            return {"diagnostic_error": f"{type(exc).__name__}: {exc}"}
        # ⛔ The two copies must still agree on the GATE metric itself.
        got, want = pep.get("ipsae"), pinned.get("peptide_ipsae")
        if got is not None and want is not None and abs(float(got) - float(want)) > 1e-9:
            raise RuntimeError(
                f"ipSAE scorers DISAGREE on {pdb_path}: pinned "
                f"{self.ipsae_dir} gives ipsae_d0res={want!r}, diagnostic "
                f"{self.diagnostic_path} gives {got!r}. Every gate calibrated on "
                f"this metric is silently re-scaled by that difference. Refusing "
                f"to record either value until the two copies are reconciled.")
        return {"ipsae_d0ref": pep.get("ipsae_d0ref"),
                "d0res_clamped_frac": pep.get("d0res_clamped_frac"),
                "ipae": pep.get("ipae"),
                "diagnostic_scorer": self.diagnostic_path}

    def selftest(self) -> dict[str, Any]:
        """Re-derive a validated value on a REAL complex before trusting a gate."""
        ref = IPSAE_REFERENCE
        if not (os.path.exists(ref["pdb"]) and os.path.exists(ref["pae"])):
            return {"ran": False, "reason": "reference structure not on this host"}
        got = self.score(ref["pdb"], ref["pae"],
                         receptor_chain=ref["pair"][0], peptide_chain=ref["pair"][-1])
        val = got.get("peptide_ipsae")
        ok = val is not None and abs(float(val) - ref["expected"]) <= ref["tolerance"]
        if not ok:
            raise RuntimeError(
                f"ipSAE self-test FAILED: 8C2E {ref['pair']} scored {val}, expected "
                f"{ref['expected']} +- {ref['tolerance']} from {self.ipsae_dir}. The "
                f"campaign gate (peptide ipSAE >= 0.66) is calibrated on that scale; "
                f"a shifted scorer silently re-scales every gate in the spec."
            )
        return {"ran": True, "reference": "8C2E", "expected": ref["expected"],
                "observed": round(float(val), 4), "scorer_dir": self.ipsae_dir}


# ---------------------------------------------------------------------------
# The Platform
# ---------------------------------------------------------------------------

class LocalPlatform:
    """A :class:`Platform` backed entirely by local compute."""

    name = "local"

    def __init__(self, *, work_dir: str, pocket: PocketSpec,
                 msa: MSARegistry,
                 reference_pockets: Sequence[PocketSpec] = (),
                 generator: LocalLigandForge | None = None,
                 folder: LocalBoltz | None = None,
                 ipsae: IPSAEScorer | None = None,
                 fastapi_backend: str = DEFAULT_FASTAPI_BACKEND,
                 progress: Callable[..., None] = _noop,
                 keep_structures: bool = True) -> None:
        self.work_dir = work_dir
        os.makedirs(work_dir, exist_ok=True)
        self.pocket = pocket
        # ⛔ CFG NEGATIVES. Each is a REAL off-target/paralog pocket, verified
        # against its own structure in its own numbering frame, exactly like the
        # target pocket. They are what the v6.9 line guides AWAY from.
        self.reference_pockets = list(reference_pockets)
        self.msa = msa
        self.progress = progress
        self.fastapi_backend = fastapi_backend
        self.keep_structures = keep_structures
        self.generator = generator or LocalLigandForge(progress=progress)
        self.folder = folder or LocalBoltz(progress=progress)
        self.ipsae = ipsae or IPSAEScorer()
        self._features: list[list[float]] | None = None
        self._feature_prov: dict[str, Any] = {}
        self._ref_features: list[list[list[float]]] | None = None
        self._ref_prov: list[dict[str, Any]] = []
        self._declared: dict[str, str] | None = None
        self._fold_calls = 0
        # Recorded so the run's own artefacts say what ran, without re-deriving.
        self.run_manifest: dict[str, Any] = {
            "platform": self.name,
            "generator": {"model_id": self.generator.model_id,
                          "lf_root": self.generator.lf_root,
                          "device": self.generator.device},
            "folder": {"boltz_bin": self.folder.boltz_bin,
                       "cache": self.folder.cache,
                       "cuda_device": self.folder.device_index,
                       "max_receptor_residues": self.folder.max_receptor_residues},
            "ipsae": self.ipsae.selftest(),
            "msa": self.msa.summary(),
            "unsupported_heads": [],
        }

    # -- helpers --------------------------------------------------------
    def _declared_receptors(self, spec: CampaignSpec) -> dict[str, str]:
        """Every construct the SPEC declares — the only receptors G7 will trust.

        ⛔ This is `spec.declared_receptors()`, NOT a second walk of the spec
        tree. G7 resolves which chain is the receptor by matching against the
        DECLARED constructs; if this set and the engine's set could drift, a
        construct the engine knows about would be unidentifiable here and its
        poses would silently fall back to "longest chain is the receptor".
        """
        if self._declared is None:
            self._declared = dict(spec.declared_receptors())
        return self._declared

    def _features_for(self, spec: CampaignSpec) -> list[list[float]]:
        if self._features is None:
            self._features, self._feature_prov = self.generator.pocket_features(
                self.pocket, fastapi_backend=self.fastapi_backend)
            self.run_manifest["pocket"] = self._feature_prov.get("pocket_verification")
        return self._features

    def _reference_features_for(self, spec: CampaignSpec) -> list[list[list[float]]]:
        """The CFG negatives, featurized once and verified like the target."""
        if self._ref_features is None:
            feats: list[list[list[float]]] = []
            prov: list[dict[str, Any]] = []
            for rp in self.reference_pockets:
                f, p = self.generator.pocket_features(
                    rp, fastapi_backend=self.fastapi_backend)
                feats.append(f)
                prov.append(p.get("pocket_verification") or {})
            self._ref_features = feats
            self._ref_prov = prov
            self.run_manifest["cfg_reference_pockets"] = prov
        return self._ref_features

    # -- generate -------------------------------------------------------
    def generate(self, spec: CampaignSpec, pc: PeptideClass, n: int) -> list[GeneratedDesign]:
        feats = self._features_for(spec)
        g = spec.generation

        # ⛔ A DECLARED HEAD THAT CANNOT RUN MUST SAY SO, LOUDLY AND ON EVERY ROW.
        #
        # spec.py validates `generation.heads`, which makes an author reasonably
        # believe the modules are applied.  The local generator's request schema
        # (pocket_features / num_peptides / length_range / temperature / top_p /
        # seed / gene / batch_size / run ids) has no head parameter at all, so
        # there is nothing to map them onto.  Dropping them silently is the exact
        # defect bd-dre-c80f4.5 records on the hosted path: a campaign whose own
        # record claims modules that never ran.  They are recorded as REQUESTED
        # AND NOT APPLIED, on every design's provenance and on the run manifest.
        heads = {k: v for k, v in dict(g.heads or {}).items() if v}
        if heads and not self.run_manifest["unsupported_heads"]:
            self.run_manifest["unsupported_heads"] = sorted(heads)
            self.progress(
                f"  ⛔ generation.heads {sorted(heads)} are declared in the spec but "
                f"the LOCAL generator has no head parameters — they are NOT applied. "
                f"Recorded on every design as heads_requested_not_applied.")

        self.progress(f"generate {n} x {pc.name} ({pc.length_range[0]}-"
                      f"{pc.length_range[1]} aa) vs {spec.target.gene} on "
                      f"{self.generator.model_id}")
        refs = self._reference_features_for(spec)
        seqs, prov = self.generator.generate(
            feats, n, tuple(pc.length_range), reference_features=refs,
            temperature=float(g.extra.get("temperature", 0.9)),
            top_p=float(g.extra.get("top_p", 0.92)),
            gene=spec.target.gene, seed=g.extra.get("seed"),
            job_id=f"{spec.name}-{pc.name}")

        # ⛔ The collapsed length-range defect, checked here exactly as
        # SDKPlatform checks it: a batch at a single length is not the range the
        # spec asked for, and folding it spends the whole budget on one length.
        lens = {len(s) for s in seqs}
        if seqs and len(lens) < 2:
            raise RuntimeError(
                f"generation returned {len(seqs)} designs at a SINGLE length {lens}. "
                f"The requested range was {tuple(pc.length_range)}. This is the "
                f"collapsed length-range defect; do not fold this batch.")
        if not seqs:
            raise RuntimeError(
                f"generation for class {pc.name!r} returned ZERO usable sequences "
                f"from {prov['length_bins']}. That is a failed generation, not an "
                f"empty result.")

        out: list[GeneratedDesign] = []
        for s in seqs:
            out.append(GeneratedDesign(
                design_id=sha256_seq(s)[:12], sequence=s,
                peptide_class=pc.name, topology=pc.topology,
                molecule_class=pc.molecule_class or pc.topology,
                provenance={"source": "local", **prov,
                            "heads_requested_not_applied": sorted(heads),
                            "pocket": self._feature_prov.get("pocket_verification")}))
        self.progress(f"  generated {len(out)} unique designs, "
                      f"{len(lens)} distinct lengths ({min(lens)}-{max(lens)} aa)")
        return out

    # -- fold -----------------------------------------------------------
    def fold(self, spec: CampaignSpec, sequences: Sequence[str], *,
             construct: Construct, cofactors: Sequence[str],
             n_seed_units: int, label: str) -> list[PoseResult]:
        f = spec.fold
        if cofactors:
            # Same refusal as SDKPlatform, for the same reason: a
            # cofactor-dependent interface folded cofactor-free is not the same
            # measurement, and Boltz has not been given the cofactor here either.
            raise RuntimeError(
                f"construct {construct.name!r} declares cofactors "
                f"{list(cofactors)} but this local fold path does not place "
                f"them. A cofactor-dependent interface folded cofactor-free is "
                f"not the same measurement. Remove the cofactor declaration to "
                f"run an explicitly apo campaign.")

        self.folder.check_capacity(construct)

        # ⛔ MSA-FREE RECEPTOR FOLDS PRODUCE GARBAGE ipSAE.  The spec declares
        # msa.receptor; honour it or refuse, never quietly fold without.
        msa_entry = self.msa.get(construct.sequence)
        want_msa = bool(f.msa_receptor)
        if want_msa and msa_entry is None:
            raise RuntimeError(
                f"construct {construct.name!r} ({len(construct.sequence)} aa, "
                f"sha256 {sha256_seq(construct.sequence)[:12]}) has NO registered "
                f"receptor MSA, and the spec sets fold.msa.receptor = true. "
                f"Folding a receptor without an MSA produces garbage ipSAE that "
                f"is indistinguishable from a real bad score. This arm has NOT "
                f"been folded. Register an a3m whose query row equals this "
                f"construct, or set msa.receptor false and accept the "
                f"consequence explicitly.")
        msa_path = msa_entry.path if msa_entry else "empty"

        uniq: list[str] = []
        seen: set[str] = set()
        for s in sequences:
            s = str(s).strip().upper()
            if s and s not in seen:
                seen.add(s)
                uniq.append(s)
        if not uniq:
            return []

        declared = self._declared_receptors(spec)
        results: list[PoseResult] = []
        self._fold_calls += 1
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", label)

        for unit in range(max(1, n_seed_units)):
            self.progress(f"fold[{label}] unit {unit + 1}/{max(1, n_seed_units)}: "
                          f"{len(uniq)} designs vs {construct.name} "
                          f"cofactors={list(cofactors) or 'none'}")
            base = os.path.join(self.work_dir, "folds",
                                f"{self._fold_calls:03d}_{safe}", f"u{unit}")
            yaml_dir = os.path.join(base, "in")
            out_dir = os.path.join(base, "out")
            os.makedirs(yaml_dir, exist_ok=True)
            tag_of: dict[str, str] = {}
            for s in uniq:
                tag = sha256_seq(s)[:12]
                tag_of[tag] = s
                self.folder.write_yaml(os.path.join(yaml_dir, f"{tag}.yaml"),
                                       construct.sequence, s, msa_path)

            # ⛔ EVERY SEED UNIT IS A DELIBERATE REPEAT.  Distinct --seed AND a
            # distinct output tree, so a unit can never be served from a previous
            # unit's cache.  The Wilson certification counts INDEPENDENT SEED
            # UNITS; a deduplicated replicate makes certification uncomputable
            # while looking perfectly normal.
            rc, log_path = self.folder.predict(
                yaml_dir, out_dir,
                diffusion_samples=f.diffusion_samples,
                sampling_steps=f.sampling_steps,
                recycling_steps=f.recycling_steps,
                use_potentials=f.use_potentials,
                seed=unit + 1, n_designs=len(uniq))

            found = self.folder.collect(out_dir, yaml_dir)

            # ⛔ G8 — ZERO ROWS FOR A NON-EMPTY REQUEST IS A FAILURE.
            if not found:
                raise RuntimeError(
                    f"fold[{label}] unit {unit + 1}: asked to fold {len(uniq)} "
                    f"sequence(s) against {construct.name!r} and Boltz produced "
                    f"ZERO structures (rc={rc}). That is a failed fold, not an "
                    f"empty result — the GPU time was spent. Do not re-queue on "
                    f"it. Log: {log_path}")

            dropped: dict[str, int] = {}
            unit_poses = 0
            for tag, fo in sorted(found.items()):
                seq = tag_of.get(tag)
                if seq is None:
                    dropped["unknown_tag"] = dropped.get("unknown_tag", 0) + 1
                    continue

                # ⛔ A COMPLEX FOLD THAT CAME BACK AS ONE CHAIN IS NOT A COMPLEX
                # FOLD.  Checked by PARSING coordinates, never a status field.
                chains = pdb_chain_ids(fo.pdb_path)
                if len(chains) < 2:
                    raise RuntimeError(
                        f"fold[{label}] asked for {seq} against construct "
                        f"{construct.name!r} ({len(construct.sequence)} aa) but "
                        f"{fo.pdb_path} has ONE chain {sorted(chains)} — it is a "
                        f"single-chain prediction of the peptide alone, not a "
                        f"complex. No interface metric can exist on it.")

                # ⛔ G7 — identity from the STRUCTURE, against DECLARED constructs.
                pid = identify_pose(fo.pdb_path, declared_receptors=declared)
                if pid.is_resolved and pid.receptor_sha256 != sha256_seq(construct.sequence):
                    raise RuntimeError(
                        f"fold[{label}]: {fo.pdb_path} was written for construct "
                        f"{construct.name!r} but its RECEPTOR CHAIN hashes to "
                        f"{pid.receptor_sha256[:12]}, not "
                        f"{sha256_seq(construct.sequence)[:12]}. The structure "
                        f"does not contain the receptor this arm claims.")

                pep_chain = pid.peptide_chain_id or "B"
                rec_chain = pid.receptor_chain_id or "A"

                if fo.pae_path is None:
                    ips: dict[str, Any] = {"error": "no PAE matrix written"}
                else:
                    ips = self.ipsae.score(fo.pdb_path, fo.pae_path,
                                           peptide_chain=pep_chain,
                                           receptor_chain=rec_chain)

                if ips.get("error"):
                    peptide_ipsae = Measured.failed(
                        f"peptide ipSAE could not be computed: {ips['error']}")
                else:
                    # ⛔ The clamp fraction rides along on the NOTE so it reaches
                    # the campaign record. A d0res_clamped_frac near 1.0 means an
                    # ABSOLUTE gate on this pose's ipsae_d0res is not comparable
                    # with a pose whose frac is near 0 -- and on this campaign the
                    # frac tracks peptide length, while the spec ranks across
                    # three length classes. Recording it changes no gate; it stops
                    # the confound from being invisible.
                    _cl = ips.get("d0res_clamped_frac")
                    _note = f"n_contacts={ips.get('n_contacts')}"
                    if _cl is not None:
                        _note += (f"; d0res_clamped_frac={float(_cl):.3f}"
                                  f"; ipsae_d0ref={ips.get('ipsae_d0ref')}")
                    peptide_ipsae = Measured(
                        float(ips["peptide_ipsae"]), Status.MEASURED, n=1,
                        method=f"ipsae_d0res[{ips['pair_key']}]", note=_note)

                conf = fo.confidence
                iptm = _conf_metric(conf, "iptm")
                complex_plddt = _conf_plddt(conf, "complex_plddt")

                pep_vals = _chain_plddt(fo.pdb_path, pep_chain)
                peptide_plddt = (Measured(pep_vals, Status.MEASURED, n=1,
                                          units="0-100", method="boltz2")
                                 if pep_vals is not None else
                                 Measured.absent("no peptide chain residues parsed"))
                iface, n_iface = interface_plddt(fo.pdb_path, pep_chain, rec_chain)
                peptide_interface_plddt = (
                    Measured(iface, Status.MEASURED, n=n_iface, units="0-100",
                             method="boltz2 CB<=4.5A")
                    if iface is not None else
                    Measured.absent("no peptide residue within 4.5 A of the receptor"))

                results.append(PoseResult(
                    design_id=tag, sequence=seq,
                    # ⛔ The seed-unit label is what ReliabilityBlock counts.  It
                    # must be unique per independent draw and identical in form to
                    # SDKPlatform's, or certification silently changes meaning.
                    seed_unit=f"{label}:u{unit}",
                    identity=pid,
                    peptide_ipsae=peptide_ipsae, iptm=iptm,
                    complex_plddt=complex_plddt, peptide_plddt=peptide_plddt,
                    peptide_interface_plddt=peptide_interface_plddt,
                    receptor_construct=construct.name,
                    cofactors_present=tuple(c.upper() for c in cofactors),
                    pose_path=fo.pdb_path, source="local"))
                unit_poses += 1

            missing = len(uniq) - unit_poses
            if missing > 0:
                dropped["no_structure_written"] = missing
            if dropped and unit_poses == 0:
                raise RuntimeError(
                    f"fold[{label}] unit {unit + 1}: every one of {len(uniq)} "
                    f"designs was dropped ({dropped}). The batch ran and nothing "
                    f"was recorded. Do not treat this as an empty result. "
                    f"Log: {log_path}")
            if dropped:
                self.progress(
                    f"  WARNING fold[{label}] unit {unit + 1}: {sum(dropped.values())} "
                    f"of {len(uniq)} designs produced no scored pose — {dropped}. "
                    f"A design that failed to fold is reliability data; excluding "
                    f"it biases the failure rate optimistic. Log: {log_path}")
            self.progress(f"  unit {unit + 1}: {unit_poses} poses recorded")
        return results

    # -- developability -------------------------------------------------
    def score_developability(self, spec: CampaignSpec,
                             sequences: Sequence[str]) -> dict[str, dict[str, Any]]:
        if self.fastapi_backend not in sys.path:
            sys.path.insert(0, self.fastapi_backend)
        try:
            from services.peptide_scoring import (                # noqa: PLC0415
                score_peptide_stability, score_peptide_immunogenicity)
        except Exception as exc:                                  # noqa: BLE001
            self.progress(f"  developability unavailable locally: {exc}")
            return {}
        out: dict[str, dict[str, Any]] = {}
        for seq in sequences:
            rec: dict[str, Any] = {}
            try:
                rec["stability"] = score_peptide_stability(seq)
            except Exception as exc:                              # noqa: BLE001
                rec["stability_error"] = f"{type(exc).__name__}: {exc}"
            try:
                rec["immunogenicity"] = score_peptide_immunogenicity(seq)
            except Exception as exc:                              # noqa: BLE001
                rec["immunogenicity_error"] = f"{type(exc).__name__}: {exc}"
            out[seq] = rec
        self.progress(f"  developability scored {len(out)} sequences locally")
        return out

    # -- affinity -------------------------------------------------------
    def score_affinity(self, spec: CampaignSpec,
                       poses: Sequence[PoseResult]) -> dict[str, dict[str, Any]]:
        """DeltaForge v10.2.

        ⛔ ``score_deltaforge_v102``, never ``compute_deltaforge_energy`` — the
        latter is a different, uncalibrated scorer.
        ⛔ The residue name must stay HIS.  ``deltaforge.rs`` dispatches on the
        residue NAME and has no HIP/HID/HIE branch, so a protonation-renamed
        histidine falls through to APOLAR and silently stops being the switch
        this campaign is built on.  Nothing here renames a residue.
        ⛔ ``kd_nm`` is a PREDICTION. It is recorded under that name and never
        presented as a measured affinity.
        """
        if self.fastapi_backend not in sys.path:
            sys.path.insert(0, self.fastapi_backend)
        try:
            from services.deltaforge_v10_client import (          # noqa: PLC0415
                score_deltaforge_v102)
        except Exception as exc:                                  # noqa: BLE001
            self.progress(f"  DeltaForge unavailable locally: {exc}")
            return {}
        out: dict[str, dict[str, Any]] = {}
        for p in poses:
            if not p.pose_path or not os.path.exists(p.pose_path):
                continue
            pep = (p.identity.peptide_chain_id if p.identity else None) or "B"
            rec = (p.identity.receptor_chain_id if p.identity else None) or "A"
            try:
                content = open(p.pose_path, encoding="utf-8").read()
                r = score_deltaforge_v102(content, receptor_chains=[rec],
                                          peptide_chain=pep)
                if isinstance(r, dict):
                    # Record what actually scored it, so a number can be audited
                    # without re-deriving which head ran.
                    r = dict(r)
                    r["_pose_path"] = p.pose_path
                    r["_value_is_prediction"] = True
                out[p.design_id] = r
            except Exception as exc:                              # noqa: BLE001
                out[p.design_id] = {"__error__": f"{type(exc).__name__}: {exc}"}
        heads = {str((v or {}).get("calibration_head")) for v in out.values()
                 if isinstance(v, dict)}
        self.progress(f"  DeltaForge scored {len(out)} poses; calibration heads {heads}")
        return out


# ---------------------------------------------------------------------------

def _conf_metric(conf: Mapping[str, Any], key: str) -> Measured:
    v = conf.get(key)
    if v is None:
        return Measured.absent(f"{key} not present in the Boltz confidence record")
    try:
        f = float(v)
    except (TypeError, ValueError):
        return Measured.failed(f"{key} was {v!r}")
    if f > 1.0:
        return Measured.failed(
            f"{key}={f} exceeds 1.0 — not a measured Boltz-2 metric")
    return Measured(f, Status.MEASURED, n=1, method="boltz2")


def _conf_plddt(conf: Mapping[str, Any], key: str) -> Measured:
    """Boltz writes pLDDT on 0-1 in the confidence JSON; normalise to 0-100."""
    v = conf.get(key)
    if v is None:
        return Measured.absent(f"{key} not present in the Boltz confidence record")
    try:
        f = float(v)
    except (TypeError, ValueError):
        return Measured.failed(f"{key} was {v!r}")
    if f <= 1.0:
        f *= 100.0
    return Measured(f, Status.MEASURED, n=1, units="0-100", method="boltz2")


def _chain_plddt(pdb_path: str, chain: str) -> float | None:
    per_res: dict[int, float] = {}
    with open(pdb_path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.startswith("ATOM  ") or len(line) < 67:
                continue
            if line[21] != chain:
                continue
            if line[12:16].strip() != "CA":
                continue
            try:
                per_res[int(line[22:26])] = float(line[60:66])
            except ValueError:
                continue
    if not per_res:
        return None
    return float(sum(per_res.values()) / len(per_res))
