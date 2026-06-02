# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""
20 — Explicit parallel-GPU control for folding (the platform / P2.D).

Demonstrates the new ``n_parallel_gpus=`` parameter on ``client.peptides.fold()``.
The server validates against tier GPU limits and returns HTTP 400 with the cap
value when the request exceeds the caller's tier:

    free        → 1 GPU
    basic       → 4 GPUs
    academia    → 16 GPUs
    pro         → 25 GPUs
    enterprise  → 50 GPUs

Combined with estimate_fold_time(), you can compute the wall-clock impact
BEFORE submitting — see the ETA banner at the bottom.

Run with:
    LIGANDAI_API_KEY=lgai_pro_... python 20_parallel_fold_control.py
"""

from __future__ import annotations

import os
import sys

from ligandai import LigandAI, estimate_fold_time, format_eta
from ligandai.errors import LigandAIError, LigandAIValidationError


def main() -> int:
    key = os.environ.get("LIGANDAI_API_KEY")
    if not key:
        print("LIGANDAI_API_KEY env var is required", file=sys.stderr)
        return 1

    client = LigandAI(api_key=key)
    me = client.account.me()
    print(f"Authenticated as {me.email} (tier={me.subscription_tier})")

    # Example sequences (replace with your own)
    sequences = [
        {"chainId": "A", "sequence": "MELAALCRWGLLLALLPPGAASTQVCTGTDMKLRLPASPETHLDMLRHLYQGCQVVQGNLELTYLPTNASLSFLQDIQEVQGYVLIAHNQVRQVPLQRLRIVRGTQLFEDNYALAVLDNGDPLNNTTPVTGASPGGLRELQLRSLTEILKGGVLIQRNPQLCYQDTILWKDIFHKNNQLALTLIDTNRSRACHPCSPMCKGSRCWGESSEDCQSLTRTVCAGGCARCKGPLPTDCCHEQCAAGCTGPKHSDCLACLHFNHSGICELHCPALVTYNTDTFESMPNPEGRYTFGASCVTACPYNYLSTDVGSCTLVCPLHNQEVTAEDGTQRCEKCSKPCARVCYGLGMEHLREVRAVTSANIQEFAGCKKIFGSLAFLPESFDGDPASNTAPLQPEQLQVFETLEEITGYLYISAWPDSLPDLSVFQNLQVIRGRILHNGAYSLTLQGLGISWLGLRSLRELGSGLALIHHNTHLCFVHTVPWDQLFRNPHQALLHTANRPEDECVGEGLACHQLCARGHCWGPGPTQCVNCSQFLRGQECVEECRVLQGLPREYVNARHCLPCHPECQPQNGSVTCFGPEADQCVACAHYKDPPFCVARCPSGVKPDLSYMPIWKFPDEEGACQPCPINCTHSCVDLDDKGCPAEQRASPLTSIISAVVGILLVVVLGVVFGILIKRRQQKIRKYTMRRLLQETELVEPLTPSGAMPNQAQMRILKETELRKVKVLGSGAFGTVYKGIWIPDGENVKIPVAIKVLRENTSPKANKEILDEAYVMAGVGSPYVSRLLGICLTSTVQLVTQLMPYGCLLDHVRENRGRLGSQDLLNWCMQIAKGMSYLEDVRLVHRDLAARNVLVKSPNHVKITDFGLARLLDIDETEYHADGGKVPIKWMALESILRRRFTHQSDVWSYGVTVWELMTFGAKPYDGIPAREIPDLLEKGERLPQPPICTIDVYMIMVKCWMIDADSRPKFRELIIEFSKMARDPQRYLVIQGDERMHLPSPTDSKFYRTLMDEELHPALVDQQYTVLGVDPEHGTAEPCPGRR"},
    ]

    # 1) Submit with explicit GPU count
    n_gpus = 4
    pre_eta = estimate_fold_time(
        protein_length=len(sequences[0]["sequence"]),
        num_trajectories=1,
        n_parallel_gpus=n_gpus,
    )
    print(f"Pre-submit ETA at n_parallel_gpus={n_gpus}: {format_eta(pre_eta)}")

    try:
        job = client.peptides.fold(
            sequences=sequences,
            target_gene="EGFR",
            n_parallel_gpus=n_gpus,
            num_trajectories=1,
        )
        print(f"Submitted job {job.id}; will poll with ETA-aware progress.")

        def progress(info):
            extras = getattr(info, "model_extra", None) or {}
            eta_str = extras.get("eta_human") or "—"
            print(f"  status={info.status}  eta={eta_str}", flush=True)

        result = job.wait(timeout=900, on_progress=progress)
        print(f"Done — pLDDT={result.plddt}, iPTM={result.iptm}")

    except LigandAIValidationError as e:
        # Server returns 400 with structured body when n_parallel_gpus > tier cap.
        print(f"VALIDATION FAIL: {e}")
        body = getattr(e, "response", None) or {}
        if isinstance(body, dict) and body.get("cap"):
            print(f"  Tier cap is {body['cap']}; resubmit with n_parallel_gpus≤{body['cap']}.")
        return 2
    except LigandAIError as e:
        print(f"API error: {type(e).__name__}: {e}", file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
