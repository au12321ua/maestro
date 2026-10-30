---
name: maestro-sdk
description: Run, watch, compare and rerun team experiments on a Maestro service with the maestro_sdk Python client and a personal access token (PAT). Use when asked to browse Maestro datasets, submit or rerun runs, wait for results, read scores and logs, download artifacts or export runs. Not for administering the service (members, credentials, models, dataset import), which is web-only.
---

# Maestro SDK

Maestro runs Harbor experiments for a team. You reach it over HTTP with a PAT; you never need a shell on the server. Everything you do is shared with the team and attributed to the PAT's owner.

## Setup

```bash
# Python >= 3.10; depends only on httpx and pydantic. Use the service's version.
uv pip install "maestro-sdk==<version>" --index maestro=https://<gitea>/api/packages/<owner>/pypi/simple
python -c "import maestro_sdk.skills as s; s.install()"   # these skills, matching that SDK
```

- Take the index and version from the workspace's `AGENTS.md` or ask. A private index reads a `read:package` token from `~/.netrc`; never put it on the command line.
- `maestro_sdk.workspace.init(dir)` sets up a whole workspace (see the `maestro-package` skill).

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

## More

Read the file that matches the task before acting on it:

- [references/follow-up-runs.md](references/follow-up-runs.md): rerun a run, change a configuration and run it again, submit several configurations together, use plans, filter runs, export runs for analysis.
- [references/comparing.md](references/comparing.md): compare runs, keep a collection, record a conclusion in a snapshot, and how to report comparison numbers.
- [references/attachments.md](references/attachments.md): publish tools for the agent under test from a directory or a git commit, restore a snapshot to edit it, see what changed. To make or improve an attachment, use the `maestro-package` skill.
- [references/errors.md](references/errors.md): the fields of `ApiError` and what the common error codes ask you to do.

Every SDK method has a docstring; `help(client.runs.submit)` shows the arguments it takes.
