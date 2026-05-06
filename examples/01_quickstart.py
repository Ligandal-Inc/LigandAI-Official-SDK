# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Quickstart — auth, tier check, simple search.

Run:
    LIGANDAI_API_KEY=lgai_basic_... python examples/01_quickstart.py
"""

from __future__ import annotations

from ligandai import LigandAI


def main() -> None:
    client = LigandAI()  # reads LIGANDAI_API_KEY env var

    # No network call — tier comes from key prefix.
    print(f"Tier: {client.tier}")
    print(f"Rate limit: {client.rate_limit_per_minute} req/min")
    print(f"Max peptides per generation: {client.max_peptides_per_generation}")
    print()

    # Network call: get user + credits
    user = client.user
    print(f"User: {user.email} ({user.first_name} {user.last_name})")
    print(f"Credits: {client.credits}")
    print()

    # Search ReceptorDB
    print("Searching for EGFR receptors...")
    hits = client.receptors.search("EGFR", limit=5)
    for h in hits:
        print(f"  {h.complex_name} — {h.oligomeric_state} ({h.organism})")


if __name__ == "__main__":
    main()
