# Workspace for Maestro

<!-- Created by maestro_sdk.workspace.init. Fill in the parts in angle brackets with the
person you work for, then keep this file current. Never write a token here. -->

You work with a Maestro service through the `maestro_sdk` Python client. The skills in
`.agents/skills/` (also `.claude/skills/`) explain how:

- `maestro-sdk`: run, watch, compare and rerun experiments, and report results honestly.
- `maestro-package`: make an attachment (skill and tools for the agent under test) and
  improve it with experiments.

Reinstall them after upgrading the SDK: `python -c "import maestro_sdk.skills as s; s.install()"`.

## Service

- URL: <https://maestro.example>, in `MAESTRO_URL`.
- SDK: `maestro-sdk==<the service's version>` from the index <URL>, for example
  `https://<owner>.github.io/<repository>/simple` or
  `https://<gitea>/api/packages/<owner>/pypi/simple`.
- Token: a personal access token in the file <path>, given as `MAESTRO_TOKEN_FILE`. Never
  print it, log it, commit it or pass it on a command line.

## What you may spend

- Model runs (`execution_mode: "agent"`) cost money. Allowed without asking: <for example
  "up to 50 trials per day on dataset X with model Y">. Anything beyond: ask first.
- Oracle runs are free test runs, but they cannot carry attachments.

## Conventions

- `client_request_id`: <for example `<attachment>-<starting snapshot id prefix>-<n>`>, so a lost run
  can be found again with `client.runs.find(...)`.
- The best attachment: an analysis snapshot of the comparison whose note says which
  snapshot is now the best and why. Snapshots need no labels.

## Layout

- `attachments/<name>/`: one git repository per attachment; the repository root is the
  attachment root. Publish with `client.attachments.publish_commit(...)`.
- `notes/`: your notes, in any form. Write down what you would need to continue after
  losing this conversation: hypotheses, evidence excerpts (artifacts expire), what did not
  work, and the ids of snapshots, collections and runs.

## Current goal

<What the person wants now, the datasets and models to use, and when to stop and report.>
