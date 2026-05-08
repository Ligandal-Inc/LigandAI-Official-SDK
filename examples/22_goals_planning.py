# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""
22 — Persistent AutoResearch goal runs via ``client.goals``.

Covers every public method on :class:`Goals`:
  client.goals.start(goal, automatic_mode=True, budget_cap_credits=, ...)
  client.goals.list(program_id=, project_id=, ...)
  client.goals.get(run_id)
  client.goals.graph(run_id)              -> derived checklist/dependency graph
  client.goals.stream(run_id)             -> live SSE iterator
  client.goals.pause(run_id)
  client.goals.resume(run_id)
  client.goals.stop(run_id)

⚠️ Persistent runs CONTINUE consuming credits after the Python process exits.
   ``automatic_mode=True`` is required to acknowledge that. Always pass
   ``budget_cap_credits=`` so a runaway goal can't drain your account.

Run with:

    LIGANDAI_API_KEY=lgai_pro_... python 22_goals_planning.py
    # to actually launch a run, set:
    # LIGANDAI_GOALS_LAUNCH=1 python 22_goals_planning.py
"""

from __future__ import annotations

import os
import sys
import time

from ligandai import LigandAI
from ligandai.errors import LigandAIError


def main() -> int:
    key = os.environ.get("LIGANDAI_API_KEY")
    if not key:
        print("LIGANDAI_API_KEY env var is required", file=sys.stderr)
        return 1

    client = LigandAI(api_key=key)

    # 1) List existing goal runs (read-only, safe on every tier that has access)
    print("== goals.list() ==")
    try:
        runs = client.goals.list()
        print(f"  {len(runs)} existing run(s)")
        for r in runs[:3]:
            print(f"    - {r.id}  status={getattr(r, 'status', '—')}  "
                  f"goal={(getattr(r, 'goal', '') or '')[:60]}")
    except LigandAIError as e:
        print(f"  (goals.list skipped: {type(e).__name__}: {e})")
        runs = []

    # 2) Inspect graph / state of an existing run (if any)
    if runs:
        rid = runs[0].id
        print(f"\n== goals.get('{rid}') ==")
        try:
            run = client.goals.get(rid)
            print(f"  status={getattr(run, 'status', '—')}  "
                  f"iterations={getattr(run, 'iterations', '—')}")
        except LigandAIError as e:
            print(f"  (goals.get skipped: {type(e).__name__}: {e})")

        print(f"\n== goals.graph('{rid}') ==")
        try:
            graph = client.goals.graph(rid)
            checklist = getattr(graph, "checklist", []) or []
            evidence = getattr(graph, "evidence", []) or []
            print(f"  checklist items: {len(checklist)}  evidence items: {len(evidence)}")
        except LigandAIError as e:
            print(f"  (goals.graph skipped: {type(e).__name__}: {e})")

    # 3) Optionally launch a real run (gated behind LIGANDAI_GOALS_LAUNCH=1)
    if os.environ.get("LIGANDAI_GOALS_LAUNCH") == "1":
        goal_text = os.environ.get(
            "LIGANDAI_GOAL_TEXT",
            "Identify three EGFR binder candidates with iPSAE>0.7 and report Kd.",
        )
        print("\n== goals.start(...) ==")
        try:
            started = client.goals.start(
                goal=goal_text,
                automatic_mode=True,           # required acknowledgement
                budget_cap_credits=200,        # hard cap
                max_iterations=5,
                program_id=os.environ.get("LIGANDAI_PROGRAM_ID") or None,
                project_id=os.environ.get("LIGANDAI_PROJECT_ID") or None,
            )
            print(f"  started run_id={started.id}")

            # Pause/resume/stop demonstration (idempotent best-effort)
            time.sleep(2)
            client.goals.pause(started.id)
            print("  paused")
            client.goals.resume(started.id)
            print("  resumed")

            # Stream a few events then stop the run cleanly
            print("\n== goals.stream(...) ==")
            count = 0
            try:
                for ev in client.goals.stream(started.id, timeout=15):
                    print(f"  event#{count}: stage={getattr(ev, 'stage', '—')}  "
                          f"message={getattr(ev, 'message', '—')[:80]}")
                    count += 1
                    if count >= 5:
                        break
            except LigandAIError as e:
                print(f"  (stream interrupted: {type(e).__name__}: {e})")

            print("\n== goals.stop(...) ==")
            client.goals.stop(started.id)
            print("  stopped")
        except LigandAIError as e:
            print(f"  (goals.start skipped: {type(e).__name__}: {e})")
    else:
        print("\n  (set LIGANDAI_GOALS_LAUNCH=1 to actually start a goal run)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
