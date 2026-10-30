# Errors

Every error the service returns is raised as `ApiError`, which carries these fields:

- `status` and `code`: the HTTP status and a stable error code.
- `message` and `stage`.
- `field_errors`: JSON paths such as `/repetitions`.
- `retryable`: whether the same request may succeed later. It never means "resubmit with a new key".
- `suggested_actions` and `request_id`.

Other SDK errors: `TransportError` (no answer; see "Lost answers and retries" in SKILL.md), `BlockersPresent` (from `raise_for_blockers()`), `UnsupportedCapability`, `WaitTimeout` (the work continues on the service) and `ContentMismatch` (a download did not match its digest; nothing was saved).

## Common codes

| Code | Meaning |
| --- | --- |
| `PAT_SCOPE_INSUFFICIENT` | A read PAT tried to write. |
| `ADMIN_SESSION_REQUIRED` | Administration happens only in the web UI; ask an administrator. |
| `CONFIG_RESOLUTION_STALE` (409) | The team defaults or the model changed; resolve again. |
| `RERUN_CONFIG_CHANGED` | Use a modified run instead. |
| `EXECUTION_UNAVAILABLE` (503) | The service cannot run anything right now. |
| 429 | Rate limit or PAT quota. Do not retry in a loop. |

When you report an error to the user, include `code`, `message` and `request_id`.
