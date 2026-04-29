# Errors

```python
from ligandai import (
    LigandAIError,           # base
    LigandAIAuthError,       # 401
    LigandAITierError,       # 403
    LigandAIRateLimitError,  # 429 (auto-retried)
    LigandAICreditError,     # 402
    LigandAINotFoundError,   # 404
    LigandAIServerError,     # 5xx (auto-retried)
    LigandAIValidationError, # 400/422
)
```

## Tier errors

```python
try:
    job = client.peptides.generate(gene="EGFR", num_peptides=10000)
except LigandAITierError as e:
    print(f"Need {e.required_tier}, have {e.current_tier}")
```

## Credit errors

```python
try:
    job = client.peptides.generate(gene="EGFR", num_peptides=5000)
except LigandAICreditError as e:
    print(f"Need {e.required} credits, have {e.available}")
```

## Retry behavior

The SDK auto-retries on `429`, `5xx`, and transient network errors with
exponential backoff. Configure via `LigandAI(max_retries=N)`.
