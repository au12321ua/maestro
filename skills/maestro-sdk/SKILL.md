---
name: maestro-sdk
description: Run, watch, compare and rerun team experiments on a Maestro service with the maestro_sdk Python client and a personal access token (PAT). Use when asked to browse Maestro datasets, submit or rerun runs, wait for results, read scores and logs, download artifacts or export runs. Not for administering the service (members, credentials, models, dataset import), which is web-only.
---

# Maestro SDK

Maestro runs Harbor experiments for a team. You reach it over HTTP with a PAT; you never need a shell on the server. Everything you do is shared with the team and attributed to the PAT's owner.

## Setup

```bash
pip install <maestro-repo>/sdk        # Python >= 3.10; depends only on httpx and pydantic
```

```python
from maestro_sdk import MaestroClient
client = MaestroClient.from_env()      # MAESTRO_URL plus MAESTRO_TOKEN or MAESTRO_TOKEN_FILE
```

- Never print, log, commit or pass the token on a command line. Read it from the environment or a file.
- `read` PATs can only read. `experiment` PATs can also submit, cancel, rerun, and publish attachments and plans.
- The client ignores proxy environment variables unless you pass `trust_env=True`.

## Workflow

1. **Check the service.** Run `client.require("runs", "execution")`; it raises `UnsupportedCapability` when a feature is off. Read `caps.limits` for team limits. A `ContractDriftWarning` means the SDK and service versions differ; tell the user.
2. **Find inputs.**
   - Datasets: `client.catalog.datasets()` returns an iterator; then `client.catalog.tasks(version_id)`.
   - Models and harnesses: `client.configs.models()` and `client.configs.harnesses()`.
   - Attachments: `client.attachments.list()`. Plans: `client.plans.list()`.
   - Choose by name and id. If the choice is ambiguous, ask.
3. **Resolve before submitting.** `client.configs.resolve(config)` returns the effective values, the source of each value (`field_sources`), excluded tasks, warnings and `config_digest`.
   - `.raise_for_blockers()` raises `BlockersPresent` listing code, path and message for each blocker.
   - Fix the fields the blockers point at. Do not work around team limits.
4. **Confirm cost.** `execution_mode: "agent"` calls a real model through a team credential and spends money. Say how many trials it plans (`resolution.planned_trials`) and get the user's go-ahead unless they already authorised this scale. `"oracle"` runs each task's reference solution; it is a test mode, free of model cost, and never a model result.
5. **Submit once.** Run `client.runs.submit(name=..., resolution_id=resolution.resolution_id, client_request_id="<something you will remember>")`, and keep `submitted.idempotency_key` and `submitted.run_id`.
6. **Wait.** Run `client.operations.wait(submitted.operation_id, timeout=...)`. A `WaitTimeout` only stops your waiting, and the run continues. To stop the run itself, call `client.runs.cancel(run_id)`, but only when asked.
7. **Read results.**
   - The run: `client.runs.get(run_id)`, `client.runs.summary(run_id)`, `client.runs.trials(run_id)`.
   - Evidence: `client.runs.download_artifacts(run_id, "dir")`. To page through a log, use `client.artifacts.read(id, offset=, length=)`.

Example config: `{"dataset_version_id": ..., "execution_mode": "agent", "model_config_id": ..., "harness_version_id": ..., "repetitions": 3}`. Unset fields come from the task authors, the team policy or a plan (`plan_id`). `task_selection` defaults to all eligible tasks; otherwise use `{"mode": "explicit", "task_ids": [...]}`.

## Reporting results honestly

- `result.score == 0.0` is a real zero.
- `score is None` together with `state == "execution_error"` and a `reason_code` means the trial was never scored. Do not count it as 0, and do not drop it silently.
- `summary.excluded_by_cause` counts unscored trials by cause; `scored_coverage` is the share of trials that were scored. Report both next to any mean.
- When usage or cost is `None`, report it as unknown, not as 0.
- `download_artifacts` returns skipped files (expired, failed or deleted) with a reason. Mention them.
- Oracle runs are test-mode runs. Never compare them with model runs.

## Lost answers and retries

- One logical submission uses one idempotency key. The SDK already retries connection errors and 502/503/504 under the same key, so the run is never created twice; `submitted.replayed` tells you the answer was replayed.
- If `TransportError` is raised, repeat the same call with `idempotency_key=error.idempotency_key`. If the key is lost too, use `client.runs.find("<client_request_id>")`.
- Only use a new key (omit it) when you really mean to run again.

## Follow-up runs

| Need | Call |
| --- | --- |
| Same configuration again (snapshot fixed) | `client.runs.rerun(run_id)`; `mode="unfinished"` reruns only tasks without scores; `mode="explicit", task_ids=[...]` reruns a chosen subset |
| Change something | `req = client.runs.request(run_id)`, edit the fields, then `client.runs.submit(req, name=..., source_run_id=run_id)`; the differences are recorded |
| Several configs, all or none | `client.batches.preview(name, [c1, c2])`, then `client.batches.submit(name, [c1, c2])` |
| Package for analysis | `e = client.exports.create([run_ids])`, `client.operations.wait(e.operation_id)`, `client.exports.download(e.id, "x.zip")` |
| Publish tools for the agent under test | `client.attachments.publish("dir", label=..., parent=...)` (needs `manifest.json`, no `version`; uploads only changed files, same content returns the same snapshot); then put the id in `attachments`. `fetch(id, "dir")` restores a snapshot to edit |
| Filter runs | `client.runs.list(dataset_version_id=..., model_config_id=..., state=..., accepted_after="2026-10-24T00:00:00Z")` |
| Compare runs | `client.comparisons.compare([run_a, run_b])`: what differs, per-run statistics over common tasks, per-task cells. External failures (rate limits, environment build, platform stops) are excluded by default and the rest count as failed (0); pass `exclude=[]` to keep them all. Report `dropped_tasks`, the excluded counts and any `warnings` with the numbers |
| Keep a collection | `c = client.collections.create(name, "models", base_run_id=run_id, run_ids=[...])`; submit into it with `client.runs.submit(cfg, name=..., collection_id=c.id)` (fixed fields are filled in; a mismatch is refused); `client.collections.compare(c.id)` |
| Record a conclusion | `s = client.snapshots.create(name, collection_id=c.id)` once the runs ended, then `client.snapshots.add_note(s.id, text)`; your note is stored apart from the computed table |

## Errors

`ApiError` carries these fields:

- `status` and `code`: the HTTP status and a stable error code.
- `message`
- `field_errors`: JSON paths such as `/repetitions`.
- `retryable`: whether the same request may succeed later. It never means "resubmit with a new key".
- `suggested_actions` and `request_id`.

Common codes:

| Code | Meaning |
| --- | --- |
| `PAT_SCOPE_INSUFFICIENT` | A read PAT tried to write. |
| `ADMIN_SESSION_REQUIRED` | Administration happens only in the web UI; ask an administrator. |
| `CONFIG_RESOLUTION_STALE` (409) | The team defaults or the model changed; resolve again. |
| `RERUN_CONFIG_CHANGED` | Use a modified run instead. |
| `EXECUTION_UNAVAILABLE` (503) | The service cannot run anything right now. |
| 429 | Rate limit or PAT quota. Do not retry in a loop. |

Full reference: `sdk/README.md` in the Maestro repository.
