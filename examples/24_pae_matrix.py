# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Example: download and inspect PAE for a folded peptide.

PAE (Predicted Aligned Error) is the per-residue-pair confidence matrix
produced by Boltz-2. Two endpoints are available:

- :meth:`client.folds.get_pae_summary` — open to all tiers; cheap stats.
- :meth:`client.folds.download_pae`    — academia+; full NxN matrix.

Both require ``schema_version >= 2`` folds (post 2026-05-09). Older folds
will return 404 / "PAE not yet computed".
"""

from __future__ import annotations

import os

from ligandai import LigandAI


def main() -> None:
    client = LigandAI(api_key=os.environ["LIGANDAI_API_KEY"])
    print(f"Tier: {client.tier}, credits: {client.credits}")

    # Replace with a real fold_id from client.peptides.list() / client.folds.*.
    fold_id = int(os.environ.get("LIGANDAI_FOLD_ID", "12345"))

    # Stage 1 — summary stats (free for every tier).
    summary = client.folds.get_pae_summary(fold_id)
    shape = summary.get("shape")
    print(
        f"PAE shape: {shape}, "
        f"mean: {summary.get('mean'):.2f} Å, "
        f"max: {summary.get('max'):.2f} Å"
    )
    print(f"Per-chain-pair max: {summary.get('per_chain_pair_max')}")

    # Stage 2 — full matrix (academia+ only). Free / basic raises
    # LigandAITierError client-side before the HTTP request goes out.
    try:
        pae = client.folds.download_pae(fold_id)
        print(f"Loaded PAE matrix: shape={pae.shape}, dtype={pae.dtype}")
        # Pair-wise interface confidence quick-look:
        print(f"  diag mean (intra-chain alignment): {pae.diagonal().mean():.2f} Å")
        print(f"  off-diag mean (interface):         {pae.mean():.2f} Å")
        # Plot if matplotlib is available.
        try:
            import matplotlib.pyplot as plt  # type: ignore

            fig, ax = plt.subplots(figsize=(6, 5))
            im = ax.imshow(pae, cmap="viridis_r", vmin=0, vmax=32)
            ax.set_xlabel("Residue index")
            ax.set_ylabel("Residue index")
            ax.set_title(f"PAE for fold {fold_id}")
            fig.colorbar(im, ax=ax, label="Predicted aligned error (Å)")
            out = f"pae_{fold_id}.png"
            fig.savefig(out, dpi=120, bbox_inches="tight")
            print(f"  saved plot → {out}")
        except ImportError:
            pass
    except Exception as e:  # noqa: BLE001 — example tolerates any failure mode
        print(f"PAE download failed (likely tier or schema_version): {e}")


if __name__ == "__main__":
    main()
