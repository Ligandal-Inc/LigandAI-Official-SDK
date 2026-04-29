# Long-running jobs

Generation, folding, and scoring submit GPU work and return a `Job`:

```python
job = client.peptides.generate(gene="EGFR", num_peptides=300)

# Properties
job.id
job.status     # "queued" | "running" | "complete" | "failed"
job.progress   # 0-100 or None
job.estimated_credits

# Block until done
result = job.wait(timeout=1800, poll_interval=2.0)

# Stream live progress (SSE)
for event in job.stream():
    print(f"{event.stage}: {event.message} ({event.progress})")

# Cancel
job.cancel()
```

## Async

```python
import asyncio
from ligandai import AsyncLigandAI

async def main():
    async with AsyncLigandAI() as client:
        job = await client.peptides.generate(gene="EGFR", num_peptides=300)
        result = await job.wait()
        # Or async stream
        async for event in job.stream():
            print(event)

asyncio.run(main())
```

## Parallel jobs

```python
import asyncio

async def main():
    async with AsyncLigandAI() as client:
        jobs = await asyncio.gather(*[
            client.peptides.generate(gene=g, num_peptides=300)
            for g in ["EGFR", "HER2", "KIT"]
        ])
        results = await asyncio.gather(*[j.wait() for j in jobs])
```
