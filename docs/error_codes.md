# Error Codes

Every error the SDK raises subclasses `LigandAIError`. Server-side errors
include a code (e.g. `E001`), HTTP status, request id, and the raw
response payload. The SDK maps HTTP status codes and the server's
`error` field to specific exception subclasses.

## HTTP Status → Exception

| HTTP | Exception | Meaning |
|------|-----------|---------|
| 400 / 422 | `LigandAIValidationError` | Bad request body or query params. Check `e.errors` for per-field validation. |
| 401 | `LigandAIAuthError` | API key missing, malformed, expired, or revoked. |
| 402 (`error: "upgrade_required"`) | `LigandAIUpgradeRequired` | Caller's tier does not include the requested surface. Inherits from `LigandAIPaidTierRequired` for back-compat. |
| 402 (other) | `LigandAICreditError` | Insufficient credits for the operation. `e.required` and `e.available` populated. |
| 403 (tier-flavored) | `LigandAITierError` | Tier escalation needed (server emits `requiredTier`/`tier_required`). |
| 403 (other) | `LigandAIForbidden` | Pilot allowlist, ownership check, EULA not accepted, etc. `e.reason` carries the server's `error_code`. |
| 404 | `LigandAINotFoundError` | Resource not found — e.g. unknown gene, peptide id, fold id, or job id. |
| 429 | `LigandAIRateLimitError` | Rate limit exceeded. `e.retry_after` (seconds) populated when the server provides `Retry-After`. |
| 5xx | `LigandAIServerError` | Internal server error. The SDK retries up to `max_retries` automatically with exponential backoff before raising. |

Network errors (DNS, connection reset, timeout) propagate as
`LigandAIError` after the retry budget is exhausted.

## Server Error Codes

The server emits stable codes on the `code` field of error responses.

| Code | HTTP | Meaning |
|------|------|---------|
| `E001` | 401 | Authentication required / invalid API key. |
| `E002` | 401 | API key expired. |
| `E003` | 401 | API key revoked. |
| `E004` | 403 | Feature requires higher tier. |
| `E007` | 402 | Insufficient credits for operation. |
| `E014` | 403 | LigandAI Terms of Service or EULA not accepted. Visit `action_url`. |
| `E429` | 429 | Rate limit exceeded. Honor `Retry-After`. |

## Common Error Field Shapes

### 402 Upgrade Required
```json
{
  "error": "upgrade_required",
  "code": null,
  "message": "This API endpoint requires a paid subscription...",
  "tier_required": "pro",
  "current_tier": "free",
  "upgrade_url": "https://ligandai.com/pricing"
}
```
SDK raises `LigandAIUpgradeRequired` with `current_tier`, `required_tier`,
and `upgrade_url` populated.

### 402 Insufficient Credits
```json
{
  "error": "insufficient_credits",
  "message": "Folding requires 50 credits; available 12.",
  "required": 50,
  "available": 12
}
```
SDK raises `LigandAICreditError` with `required` and `available`.

### 403 Tier Required
```json
{
  "error": "tier_required",
  "code": "PRO_TIER_REQUIRED",
  "message": "This feature requires Pro tier.",
  "requiredTier": "pro",
  "currentTier": "academia"
}
```
SDK raises `LigandAITierError`.

### 403 EULA / TOS Not Accepted
```json
{
  "error": "terms_not_accepted",
  "code": "E014",
  "message": "You must accept the LigandAI Terms of Service before using the API.",
  "action_url": "https://ligandai.com/terms"
}
```
SDK raises `LigandAIForbidden` with `reason="terms_not_accepted"`. The
`action_url` is exposed on the raw response.

### 429 Rate Limit
```json
{
  "error": "rate_limit_exceeded",
  "code": "E429",
  "message": "Rate limit exceeded for free tier (10 req/min). Retry after 23s.",
  "retry_after_seconds": 23,
  "tier": "free",
  "limit_per_minute": 10
}
```
SDK raises `LigandAIRateLimitError` with `retry_after=23.0`.

## Catching Errors

```python
from ligandai import (
    LigandAI,
    LigandAIError,
    LigandAIUpgradeRequired,
    LigandAICreditError,
    LigandAIRateLimitError,
    LigandAIValidationError,
)

client = LigandAI(api_key="lgai_basic_...")

try:
    detail = client.peptides.get(12345, include=["pdb"])
except LigandAIUpgradeRequired as e:
    print(f"Upgrade needed: {e.current_tier} → {e.required_tier}")
    print(f"  Visit {e.upgrade_url}")
except LigandAICreditError as e:
    print(f"Need {e.required} credits, have {e.available}")
except LigandAIRateLimitError as e:
    print(f"Slow down — retry after {e.retry_after}s")
except LigandAIValidationError as e:
    print(f"Bad request: {e.errors}")
except LigandAIError as e:
    print(f"Unexpected error: {e.code} {e.message}")
```

## Tier Redaction Is Not An Error

When a free-tier caller hits a tier-flexible endpoint
(`/v1/peptides/list`, `/v1/peptides/search`, `/v1/structures/list`,
`/v1/structures/:id/pdb`), the response succeeds with HTTP 200 but the
data is redacted. Detect this via the JSON body:

```json
{ "_tier": "free", "_tier_redacted": true, "_upgrade_url": "..." }
```

or per-row:
```json
{ "sequence": "ACDEFGHIKL********", "_masked": true, ... }
```

The SDK exposes these as `Peptide.masked` and the `_tier_redacted` flag
on the response — no exception is raised, since the call itself
succeeded. Use `LigandAIUpgradeRequired` only for hard 402 walls.
