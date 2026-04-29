# Copyright © 2025 Ligandal, Inc. All rights reserved.
"""Live SSE progress streaming for a generation job.

Run:
    LIGANDAI_API_KEY=lgai_pro_... python examples/06_streaming.py
"""

from __future__ import annotations

from ligandai import LigandAI


def main() -> None:
    client = LigandAI()
    print(f"Tier={client.tier}\n")

    # Submit a small generation job
    print("Submitting generation for EGFR (100 peptides, no auto-fold)...")
    job = client.peptides.generate(
        gene="EGFR",
        num_peptides=100,
        auto_fold=False,
    )
    print(f"Job: {job.id}\n")

    # Stream live progress events
    print("=== Live progress (Ctrl-C to abort) ===")
    try:
        for event in job.stream():
            stage = event.stage or "?"
            msg = event.message or ""
            progress = f" [{event.progress:.1f}%]" if event.progress is not None else ""
            print(f"  {stage}: {msg}{progress}")
            if event.event_type in ("complete", "completed", "failed"):
                break
    except KeyboardInterrupt:
        print("\nCancelling...")
        job.cancel()
        return

    # Final result
    print("\n=== Done ===")
    job.refresh()
    if job.succeeded:
        result = job.results
        print(f"Got {len(result.peptides)} peptides")
    else:
        print(f"Job ended with status: {job.status}")


if __name__ == "__main__":
    main()
