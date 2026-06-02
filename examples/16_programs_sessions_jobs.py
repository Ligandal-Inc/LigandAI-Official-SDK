# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""
16 — Programs, sessions, workstreams, and jobs CRUD.

Covers:
  client.programs.list / create / get / update / archive
  client.programs.list_sessions / get_session / find_session_by_gene
  client.programs.create_workstream / workstreams
  client.jobs.list / get / cancel / stop_all / stream

Programs are the top-level container; sessions live inside a program (one
per gene typically); workstreams group related sessions for parallel
multi-target campaigns. Jobs are the GPU-side units that actually run on the compute backend.

Run with:
    LIGANDAI_API_KEY=lgai_pro_... python 16_programs_sessions_jobs.py
"""

from __future__ import annotations

import os
import sys

from ligandai import LigandAI
from ligandai.errors import LigandAIError


def main() -> int:
    key = os.environ.get("LIGANDAI_API_KEY")
    if not key:
        print("LIGANDAI_API_KEY env var is required", file=sys.stderr)
        return 1

    client = LigandAI(api_key=key)

    try:
        # 1) List programs
        print("== programs.list() ==")
        progs = client.programs.list()
        for p in progs[:5]:
            print(f"  - id={p.id:<6}  name={p.name}  status={p.status}  "
                  f"sessions={getattr(p, 'session_count', '?')}")

        # 2) Create a fresh program (idempotent — caller can use as scratchpad)
        scratch_name = f"sdk_example_program_16"
        print(f"\n== programs.create(name='{scratch_name}') ==")
        try:
            scratch = client.programs.create(
                name=scratch_name,
                description="Created by examples/16_programs_sessions_jobs.py",
                color="#3b82f6",
            )
            print(f"  created id={scratch.id}")
        except LigandAIError as e:
            print(f"  (program may already exist: {e})")
            scratch = next((p for p in progs if p.name == scratch_name), None)

        # 3) Sessions in a program
        if progs:
            pid = progs[0].id
            print(f"\n== programs.list_sessions(...) for program {pid} ==")
            sessions = client.programs.list_sessions(limit=3)
            for s in sessions:
                print(f"  - {s.session_id}  gene={s.lead_gene}  status={s.status}")

            # Specific session detail
            if sessions:
                sd = client.programs.get_session(sessions[0].session_id)
                print(f"\n  session detail: peptides={sd.peptide_count}  "
                      f"folded={sd.folded_count}  elites={sd.elite_count}")

        # 4) Find session by gene (handy for "switch to my IL6R session")
        print("\n== programs.find_session_by_gene('EGFR') ==")
        match = client.programs.find_session_by_gene("EGFR")
        print(f"  match: {match.session_id if match else 'no session for EGFR'}")

        # 5) Workstreams
        if progs:
            print(f"\n== programs.workstreams(program_id={progs[0].id}) ==")
            wsl = client.programs.workstreams(program_id=progs[0].id)
            for w in wsl[:3]:
                print(f"  - id={w.id}  name={w.name}  genes={w.genes}")

        # 6) Active jobs (informational; do NOT cancel without confirming)
        print("\n== jobs.list(type='all', limit=5) ==")
        jobs = client.jobs.list(type="all", limit=5)
        for j in jobs:
            print(f"  - {j.id}  type={j.type}  status={j.status}  "
                  f"created={j.created_at}")

        # 7) Job detail (if any pending)
        pending = [j for j in jobs if j.status in ("queued", "running")]
        if pending:
            jid = pending[0].id
            print(f"\n== jobs.get('{jid}') ==")
            detail = client.jobs.get(jid)
            print(f"  progress={detail.progress_percent}%  message={detail.message}")
            # Streaming events (commented — requires long-running connection):
            # for event in client.jobs.stream(jid):
            #     print("    event:", event.event_type, event.payload)

    except LigandAIError as e:
        print(f"API error: {type(e).__name__}: {e}", file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
