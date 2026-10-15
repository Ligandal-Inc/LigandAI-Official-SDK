"""Measure one supplied protein pair; no generation, optimization or folding.

Run in an environment with the local SDK extension, OpenDDE and its assets.
See the accompanying setup notes. Repeat measurements use the same sequence.
"""

import argparse
import json
import os
import time
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--receptor-fasta", required=True)
    parser.add_argument("--receptor-msa", required=True)
    parser.add_argument("--candidate-fasta", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--cycles", type=int, default=1)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    parser.add_argument("--dtype", choices=("fp32", "bf16"), default="fp32")
    parser.add_argument("--chunk-size", type=int, default=32)
    parser.add_argument("--trimul-kernel", choices=("auto", "torch", "cuequivariance"))
    parser.add_argument("--triatt-kernel", choices=("auto", "torch", "cuequivariance"))
    args = parser.parse_args()
    if args.repeats < 1 or args.threads < 1 or args.chunk_size < 1:
        parser.error("repeats, threads and chunk size must be positive")
    if args.device == "cpu":
        os.environ["CUDA_VISIBLE_DEVICES"] = ""
    if args.dtype == "bf16" and args.device != "cuda":
        parser.error("bf16 requires --device cuda")
    for key in (
        "OMP_NUM_THREADS",
        "MKL_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
    ):
        os.environ[key] = str(args.threads)
    os.environ["LAYERNORM_TYPE"] = "torch"
    os.environ["LIGANDAI_SKIP_VERSION_CHECK"] = "1"
    started = time.perf_counter()
    import torch

    from ligandai.opendde import LocalOpenDDETrunk

    torch.set_num_threads(args.threads)
    torch.set_num_interop_threads(1)

    def fasta(path: str) -> str:
        lines = Path(path).read_text().splitlines()
        if sum(line.startswith(">") for line in lines) != 1:
            raise ValueError("Expected exactly one FASTA record")
        return "".join(line.strip() for line in lines if not line.startswith(">"))

    scorer = LocalOpenDDETrunk(
        receptor=[("A", fasta(args.receptor_fasta), args.receptor_msa)],
        opendde_source=args.source,
        runtime_root=args.runtime,
        checkpoint=args.checkpoint,
        n_cycle=args.cycles,
        device=args.device,
        dtype=args.dtype,
        chunk_size=args.chunk_size,
        trimul_kernel=args.trimul_kernel,
        triatt_kernel=args.triatt_kernel,
        workdir=args.workdir,
    )
    print(f"Model loaded in {scorer.load_s:.3f}s", flush=True)
    rows = []
    for index in range(args.repeats):
        rows.append(scorer.score(fasta(args.candidate_fasta)))
        receipt = {
            "rows": rows,
            "cost": scorer.cost_summary(),
            "wall_s": time.perf_counter() - started,
            "device": args.device,
            "dtype": args.dtype,
            "chunk_size": args.chunk_size,
            "threads": args.threads,
            "unchanged_pair_repeats": args.repeats,
        }
        Path(args.out).write_text(json.dumps(receipt, indent=2) + "\n")
        print(f"Repeat {index + 1}: {rows[-1]['total_s']:.3f}s", flush=True)


if __name__ == "__main__":
    main()
