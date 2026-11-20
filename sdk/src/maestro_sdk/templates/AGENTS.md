# Workspace for Maestro

<!-- Created by maestro_sdk.workspace.init. Fill in the parts in angle brackets with the
person you work for, then keep this file current. Never write a token here. -->

You work with a Maestro service through the `maestro_sdk` Python client. The skills in
`.agents/skills/` (also `.claude/skills/`) explain how:

- `maestro-sdk`: run, watch, compare and rerun experiments, and report results honestly.
- `maestro-package`: make an attachment (skill and tools for the agent under test) and
  improve it with experiments.

## Service

The workspace owner fills in `MAESTRO_URL` and `MAESTRO_TOKEN_FILE` in `.env` and
loads it when starting the agent with `uv run --no-project --env-file .env -- <agent command>`.
Use `.venv/bin/python` and `MaestroClient.from_env()` for SDK calls. The PAT file
belongs outside the workspace. Keep its contents out of logs, commits and command lines.

For a released service, sync the SDK and skills before starting the agent.

```bash
uv run --no-project --env-file .env -- .venv/bin/python -I update-maestro.py
```

For manual SDK installation, use the service's version from the
[Maestro package index](https://au12321ua.github.io/maestro/simple).

## What you may spend

- Model runs (`execution_mode: "agent"`) cost money. Allowed without asking: <for example
  "up to 50 trials per day on dataset X with model Y">. Anything beyond: ask first.
- Oracle runs are free test runs, but they cannot carry attachments.

## Conventions

- `client_request_id`: <for example `<attachment>-<starting snapshot id prefix>-<n>`>, so a lost run
  can be found again with `client.runs.find(...)`.
- The best attachment: the collection's current best pointer
  (`client.collections.set_current_best`), with the reason. Snapshots need no labels.

## Layout

- `attachments/<name>/`: one git repository per attachment; the repository root is the
  attachment root. Publish with `client.attachments.publish_commit(...)`.
- `notes/`: your notes, in any form. Write down what you would need to continue after
  losing this conversation: hypotheses, evidence excerpts (artifacts expire), what did not
  work, and the ids of snapshots, collections and runs.

## Current goal

<What the person wants now, the datasets and models to use, and when to stop and report.>
