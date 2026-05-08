# Programs / projects / sessions

A **program** is a top-level workstream (one disease, one therapeutic
modality). A **project** is a sub-workstream within a program. Each
generation/fold run is attached to a **session** which lives under a project.

## A. List + create

```python
from ligandai import LigandAI
client = LigandAI()

# List programs you own
for p in client.programs.list():
    print(p.id, p.name, p.disease, p.modality)

# Create a new one
prog = client.programs.create(
    name="ALS — peptide PROTAC pilot",
    disease="ALS",
    modality="peptide-conjugate",
)

# Add a workstream (project) under it
ws = client.programs.create_workstream(
    program_id=prog.id,
    name="C9orf72 RAN-protein binders",
)
```

## B. Sessions inside a program

```python
sessions = client.programs.list_sessions(gene="EGFR", limit=20)
session = client.programs.get_session(sessions[0].id)
print(session.peptide_count, session.fold_count)

# Find an existing session by gene
existing = client.programs.find_session_by_gene("EGFR")
```

## C. Attach a generation run to a program/project

```python
job = client.peptides.generate(
    gene="EGFR",
    num_peptides=50,
    program_id=prog.id,
    project_id=ws.id,
)
```

## D. Job control (cancel, list, stream)

```python
# All running jobs across all programs
for j in client.jobs.list(status="running"):
    print(j.id, j.kind, j.session_id)

# Get one
info = client.jobs.get("job_...")

# Cancel
client.jobs.cancel("job_...")

# Stream live SSE events
for ev in client.jobs.stream("job_..."):
    print(ev.stage, ev.message, ev.progress)
    if ev.stage == "complete":
        break

# Nuke everything (use with care)
client.jobs.stop_all()
```

## E. Persistent AutoResearch goals (pilot)

```python
run = client.goals.start(
    goal="Find three EGFR binders with iPSAE>0.7 and report Kd.",
    automatic_mode=True,            # required acknowledgement
    budget_cap_credits=200,
    max_iterations=5,
    program_id=prog.id,
)
client.goals.pause(run.id)
client.goals.resume(run.id)
client.goals.stop(run.id)
```
