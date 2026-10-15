"""Experimental local OpenDDE contact scoring; no hosted API or coordinates.

Install OpenDDE and its assets separately. Heavy dependencies are imported only
when constructing the scorer. Contact counts include BOTH cross-chain blocks,
matching our historical evaluation; these are not calibrated binding probabilities.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import statistics
import sys
import tempfile
import time
from contextlib import nullcontext
from pathlib import Path
from typing import Any


def _sequence(value: str) -> str:
    if not value or re.fullmatch(r"[ACDEFGHIKLMNPQRSTVWY]+", value) is None:
        raise ValueError("Use a nonempty, uppercase canonical protein sequence")
    return value


def _msa_query(text: str) -> str:
    rows: list[str] = []
    started = False
    for line in text.splitlines():
        if line.startswith(">"):
            if started:
                break
            started = True
        elif started and line.strip() and not line.startswith("#"):
            rows.append(line.strip())
    return "".join(rows)


def _disable_coordinates(model: Any) -> None:
    required = (
        "run_sample_diffusion_stage",
        "run_confidence_head_stage",
        "run_post_confidence_outputs_stage",
        "prepare_diffusion_cache_for_sampling",
        "sample_diffusion",
        "run_confidence_head",
        "get_pairformer_output",
        "_trunk_with_cache",
    )
    if any(not callable(getattr(model, name, None)) for name in required):
        raise RuntimeError("Unsupported OpenDDE stage contract; refusing full-fold fallback")

    def no_diffusion(*args: Any, pred_dict: dict[str, Any], **kwargs: Any) -> None:
        pred_dict["coordinate"] = None

    def no_confidence(*args: Any, **kwargs: Any) -> None:
        return None

    def empty(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {}

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("Coordinate/confidence primitive reached in trunk-only mode")

    model.run_sample_diffusion_stage = no_diffusion
    model.run_confidence_head_stage = no_confidence
    model.run_post_confidence_outputs_stage = empty
    model.prepare_diffusion_cache_for_sampling = empty
    model.sample_diffusion = forbidden
    model.run_confidence_head = forbidden
    # Repeated scores must execute the trunk. The upstream disk cache is for
    # alternate guidance on IDENTICAL inputs, not screening new candidates.
    model._trunk_with_cache = model.get_pairformer_output


def _precision_context(torch: Any, device: Any, dtype: str) -> Any:
    if dtype == "fp32":
        return nullcontext()
    if dtype != "bf16" or device.type != "cuda":
        raise ValueError("bf16 requires an explicitly selected CUDA device")
    return torch.autocast(device_type="cuda", dtype=torch.bfloat16)


def _block_stats(cp: Any, asym: Any, peptide_length: int) -> dict[str, Any]:
    import numpy as np

    cp, asym = np.asarray(cp), np.asarray(asym).reshape(-1)
    if cp.shape != (len(asym), len(asym)) or not np.isfinite(cp).all():
        raise RuntimeError("Nonfinite contacts or token-space mismatch")
    if (cp < 0).any() or (cp > 1).any():
        raise RuntimeError("Contact probabilities outside [0,1]")
    values, counts = np.unique(asym, return_counts=True)
    # The public contract requires the candidate to be strictly shorter than
    # EVERY receptor chain; ties and unexpected tokenization fail closed.
    if len(values) < 2 or sum(counts == peptide_length) != 1 or min(counts) != peptide_length:
        raise RuntimeError("Cannot identify the candidate unambiguously in token space")
    pm = asym == values[int(np.argmin(counts))]
    rm = ~pm
    block = np.concatenate([cp[np.ix_(pm, rm)].ravel(), cp[np.ix_(rm, pm)].ravel()])
    return {
        "contact_prob_max": float(block.max()),
        "contact_prob_mean": float(block.mean()),
        "contact_prob_sum": float(block.sum()),
        "contact_prob_per_res": float(block.sum() / peptide_length),
        "n_contacts_p50": int((block >= 0.5).sum()),
        "n_contacts_p80": int((block >= 0.8).sum()),
        "n_pep_tokens": peptide_length,
        "n_tokens": len(asym),
        "count_convention": "both_cross_chain_blocks",
    }


class LocalOpenDDETrunk:
    """Reuse one loaded model for sequential, local, structure-free scoring.

    ``receptor`` contains (chain ID, sequence, existing A3M path) tuples.
    Candidates must be strictly shorter than every receptor chain. No MSA
    search is requested. CPU is the default; CUDA requires an explicit device.
    Ten recycles is the documented default. Other settings do not inherit the
    historical quality figures. This object is not thread safe.
    """

    def __init__(
        self,
        receptor: list[tuple[str, str, str]],
        *,
        opendde_source: str,
        runtime_root: str,
        checkpoint: str,
        n_cycle: int = 10,
        device: str = "cpu",
        dtype: str = "fp32",
        chunk_size: int = 4,
        trimul_kernel: str | None = None,
        triatt_kernel: str | None = None,
        workdir: str | None = None,
    ) -> None:
        if device not in {"cpu", "cuda"}:
            raise ValueError("Choose cpu or cuda; select the GPU with CUDA_VISIBLE_DEVICES")
        if dtype not in {"fp32", "bf16"}:
            raise ValueError("Supported precision modes are fp32 and CUDA bf16")
        if dtype == "bf16" and device == "cpu":
            raise ValueError("bf16 requires an explicitly selected CUDA device")
        for kernel in (trimul_kernel, triatt_kernel):
            if kernel not in {None, "auto", "torch", "cuequivariance"}:
                raise ValueError("Kernel must be auto, torch, or cuequivariance")
            if device == "cpu" and kernel not in {None, "torch"}:
                raise ValueError("CPU scoring requires torch triangle kernels")
        if not receptor or n_cycle < 1 or chunk_size < 1:
            raise ValueError("Receptor, positive n_cycle and chunk_size are required")
        ids = [r[0] for r in receptor]
        if len(set(ids)) != len(ids) or any(re.fullmatch(r"[A-Z]", c) is None for c in ids):
            raise ValueError("Receptor chain IDs must be distinct single uppercase letters")
        free = [c for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if c not in ids]
        if not free:
            raise ValueError("No chain ID available for candidate")
        self.receptor = [
            (cid, _sequence(seq), str(Path(msa).resolve())) for cid, seq, msa in receptor
        ]
        for cid, seq, msa in self.receptor:
            if _msa_query(Path(msa).read_text()) != seq:
                raise ValueError(f"MSA query does not match receptor chain {cid}")
        source, root, ckpt = (Path(p).resolve() for p in (opendde_source, runtime_root, checkpoint))
        if (
            not (source / "runner/batch_inference.py").is_file()
            or not root.is_dir()
            or not ckpt.is_file()
        ):
            raise ValueError("Existing OpenDDE source, runtime assets and checkpoint are required")
        existing_root = os.environ.get("OPENDDE_ROOT_DIR")
        if existing_root and Path(existing_root).resolve() != root:
            raise ValueError("OPENDDE_ROOT_DIR conflicts with runtime_root; use a fresh process")
        os.environ["OPENDDE_ROOT_DIR"] = str(root)
        if device == "cpu":
            os.environ["LAYERNORM_TYPE"] = "torch"
        sys.path.insert(0, str(source))
        self.workdir = Path(workdir or tempfile.mkdtemp(prefix="ligandai_opendde_"))
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.peptide_chain = free[0]
        self.n_cycle = n_cycle
        self.dtype = dtype
        self.chunk_size = chunk_size
        self.timings: list[dict[str, float]] = []
        self._counter = 0
        start = time.perf_counter()
        try:
            import torch
            from runner.batch_inference import get_default_runner
        except ImportError as exc:
            raise ImportError(
                "Install the OpenDDE runtime dependencies in this Python environment"
            ) from exc
        self._torch = torch
        if dtype == "bf16":
            requested_device = torch.device(device)
            with torch.cuda.device(requested_device):
                if not torch.cuda.is_bf16_supported():
                    raise ValueError("Selected CUDA device does not support bf16")
        with ckpt.open("rb") as stream:
            digest = hashlib.sha256()
            for part in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                digest.update(part)
        self.checkpoint_sha256 = digest.hexdigest()
        options = {"trimul_kernel": "torch", "triatt_kernel": "torch"} if device == "cpu" else {}
        for key, value in (("trimul_kernel", trimul_kernel), ("triatt_kernel", triatt_kernel)):
            if value is not None:
                options[key] = value
        self.runner = get_default_runner(
            seeds=[101],
            dump_dir=str(self.workdir / "dump"),
            n_cycle=n_cycle,
            n_step=1,
            n_sample=1,
            dtype=dtype,
            device=device,
            load_checkpoint_path=str(ckpt),
            use_msa=True,
            use_template=False,
            use_rna_msa=False,
            need_atom_confidence=False,
            **options,
        )
        self.runner.configs.num_workers = 0
        setting = self.runner.configs.infer_setting
        setting.chunk_size = chunk_size
        if hasattr(setting, "chunk_size_thresholds"):
            setting.chunk_size_thresholds = dict.fromkeys(setting.chunk_size_thresholds, chunk_size)
        self.model = self.runner.model.eval()
        _disable_coordinates(self.model)
        self.load_s = time.perf_counter() - start

    def _job(self, peptide: str, name: str) -> dict[str, Any]:
        chains = []
        for cid, seq, msa in self.receptor:
            paired = self.workdir / f"receptor_{cid}_paired.a3m"
            paired.write_text(f">query\n{seq}\n")
            chains.append(
                {
                    "proteinChain": {
                        "sequence": seq,
                        "count": 1,
                        "id": [cid],
                        "unpairedMsaPath": msa,
                        "pairedMsaPath": str(paired),
                    }
                }
            )
        pep_msa = self.workdir / f"{name}.a3m"
        pep_msa.write_text(f">query\n{peptide}\n")
        chains.append(
            {
                "proteinChain": {
                    "sequence": peptide,
                    "count": 1,
                    "id": [self.peptide_chain],
                    "unpairedMsaPath": str(pep_msa),
                    "pairedMsaPath": str(pep_msa),
                }
            }
        )
        return {"name": name, "modelSeeds": [101], "sequences": chains}

    def score(self, sequence: str) -> dict[str, Any]:
        """Return contact readouts and measured times, never a fold confidence."""
        peptide = _sequence(sequence)
        if len(peptide) >= min(len(seq) for _, seq, _ in self.receptor):
            raise ValueError("Candidate must be strictly shorter than every receptor chain")
        from opendde.data.inference.infer_dataloader import get_inference_dataloader
        from opendde.utils.torch_utils import to_device

        started = time.perf_counter()
        import numpy as np

        random.seed(101)
        np.random.seed(101)
        self._torch.manual_seed(101)
        self._counter += 1
        name = f"candidate_{self._counter:06d}"
        input_path = self.workdir / f"{name}.json"
        input_path.write_text(json.dumps([self._job(peptide, name)]))
        cfg = self.runner.configs
        previous = cfg.input_json_path
        cfg.input_json_path = str(input_path)
        try:
            batches = list(get_inference_dataloader(configs=cfg))
        finally:
            cfg.input_json_path = previous
        if len(batches) != 1:
            raise RuntimeError("Expected exactly one featurized input")
        data, _, error = batches[0][0]
        if error:
            raise RuntimeError(f"OpenDDE featurization failed: {error}")
        features = to_device(data["input_feature_dict"], self.runner.device)
        self._sync()
        featurize_s = time.perf_counter() - started
        forward_started = time.perf_counter()
        with self._torch.no_grad(), _precision_context(self._torch, self.runner.device, self.dtype):
            pred, _, _ = self.model(features, mode="inference")
        self._sync()
        trunk_s = time.perf_counter() - forward_started
        if pred.get("coordinate") is not None:
            raise RuntimeError("Unexpected coordinates from trunk-only inference")
        if pred.get("contact_probs") is None:
            raise RuntimeError("OpenDDE did not return distogram contact probabilities")
        result = _block_stats(
            pred["contact_probs"].float().cpu().numpy(),
            features["asym_id"].cpu().numpy(),
            len(peptide),
        )
        timing = {
            "featurize_s": featurize_s,
            "trunk_s": trunk_s,
            "total_s": time.perf_counter() - started,
        }
        self.timings.append(timing)
        result.update(timing)
        result.update(
            seed=101,
            n_cycle=self.n_cycle,
            checkpoint_sha256=self.checkpoint_sha256,
            structure_free=True,
            score_kind="distogram_contact_triage",
            device=str(self.runner.device),
            dtype=self.dtype,
            chunk_size=self.chunk_size,
            trunk_output_cache=False,
        )
        return result

    def _sync(self) -> None:
        if self.runner.device.type == "cuda":
            self._torch.cuda.synchronize(self.runner.device)

    def cost_summary(self) -> dict[str, Any]:
        warm = [t["total_s"] for t in self.timings[1:]]
        return {
            "n": len(self.timings),
            "model_load_s": self.load_s,
            "first_candidate_s": self.timings[0]["total_s"] if self.timings else None,
            "warm_median_total_s": statistics.median(warm) if warm else None,
            "warm_samples": len(warm),
        }
