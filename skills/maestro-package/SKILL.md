---
name: maestro-package
description: Make an attachment (a skill and tools given to the agent under test) for a Maestro service and improve it with experiments, using the maestro_sdk Python client. Use when asked to turn existing material into an attachment, publish an attachment from a git repository, test whether a change to an attachment helps, or pick up such work after an interruption. Read the maestro-sdk skill first for submitting runs and reporting results.
---

# Making and improving attachments

An attachment is a set of files copied into every trial's container before the agent under test starts: usually a skill and the tools it uses. Maestro keeps the facts others rely on: each published snapshot (content-addressed, immutable, with a parent), each run, each comparison you freeze. How you work towards a better attachment is up to you. Nothing below is enforced; it is what tends to work.

## Workspace

```python
import maestro_sdk.workspace
maestro_sdk.workspace.init("my-workspace")   # AGENTS.md, notes/, attachments/, skills; never overwrites
```

Fill in `AGENTS.md` with the person you work for: service, token file, what you may spend, naming conventions, the current goal. Keep notes in `notes/` in any form. Each attachment is its own git repository under `attachments/<name>/`.

## Making an attachment

1. **Read the material** (a local directory, a path in an online repository, or an existing snapshot: `client.attachments.fetch(id, dir)`). Note where it came from, the commit and the license in your notes.
2. **Choose what goes in.** Only the skill and the tools it needs. No datasets, answers or solutions to tasks, credentials, caches or large files. The repository root is the attachment root, and every tracked file is published.
3. **Write `manifest.json`** at the root: `{"schema_version": 1, "name": "<name>", "description": "...", "effects": ["agent_tools"], "compatible_harnesses": ["*"]}`. `effects` may also hold `model_call` and `task_environment`; `provides` (`{"tools": [...], "env": [...]}`) declares names that must not clash with other attachments. There is no `version` field: a snapshot is identified by its id.
4. **Write `bootstrap.sh` if something must be installed.** The platform runs `bash bootstrap.sh` in the attachment's directory inside the container (also in `$MAESTRO_ATTACHMENT_DIR`), as the task's agent user, under the install time limit, with the deployment's install proxy if any. What it must do depends on the harness: find out where the harness under test reads skills or tools (its documentation, or a smoke run that lists candidate directories) and install there. A failing or slow script makes the trial `attachment_failed`, never a model zero.
5. **Commit and publish:**

   ```python
   first = client.attachments.publish_commit("attachments/<name>")
   ```

   It publishes what the commit tracks, read from git (never untracked or ignored files), with executable bits from git's file modes, and checks the snapshot's digest. It warns about uncommitted changes, which it leaves out. `client.attachments.publish(dir)` publishes a directory as it is, for quick trials.
6. **Smoke run.** Oracle runs cannot carry attachments, so use one task with the real model and harness (it costs money; see `AGENTS.md`). In the artifacts, confirm the install log shows `bootstrap.sh` succeeded and the agent actually read the skill or used the tools. If it did not, the attachment is not done.

## Improving it

**Set up the comparison once.** Run the current best snapshot on the chosen tasks and repetitions, then build an attachment A/B collection from that run, so every later run differs only in the attachment:

```python
c = client.collections.create("<name> on <dataset>", "attachments", base_run_id=base_run_id)
```

Fix the task list with `task_selection` (`{"mode": "explicit", "task_ids": [...]}`) and use the same list every time: the collection does not check the task range. Keep a held-out set as a second collection if you are tuning on the first.

**Each attempt:**

1. Start from a snapshot (the current best or an earlier attempt). Read its results and trajectories, and your notes.
2. Write the hypothesis and the evidence for it: run, trial, artifact path, and an excerpt of the event. Artifacts expire after the retention period, so excerpts keep your reasons readable later.
3. Change the files on a branch, commit, and publish with `parent=` the starting snapshot. No label is needed: snapshots are identified by id, and the same content published again returns the existing snapshot.
4. Submit into the collection with a `client_request_id` you can derive again, such as `<name>-<first 8 characters of the starting snapshot id>-<n>`:

   ```python
   client.runs.submit({"attachments": [snapshot.id], "task_selection": tasks}, name="...",
                      collection_id=c.id, client_request_id="...")
   ```

5. When the runs end, compare against the starting point task by task, and read the new trajectories, not only the means.
6. Write down what you learned, including what did not work.

**Judging "better".** Compare with every failure kept and check the evidence is complete before reading the numbers:

```python
cmp = client.collections.compare(c.id, exclude=[], baseline_run_id=start_run_id)
complete = cmp.dropped_tasks == 0 and cmp.common_tasks == cmp.all_tasks and all(
    r.scored_trials == r.planned_trials for r in cmp.runs
)
```

- Not complete (unscored trials, execution failures, missing tasks): rerun or report "not enough evidence". Do not count them as zero and do not narrow to the common tasks.
- Complete: `r.mean` and `r.difference` are the macro means over the same tasks and repetitions. A valid 0 is a real score.
- Small differences over few tasks are often noise. Prefer more repetitions or tasks to a confident claim.

**The best attachment.** When a snapshot is judged better than the current best, record that where the team can see it: freeze the comparison and say in its note which snapshot is now the best and why, so anyone can find it without your notes:

```python
s = client.snapshots.create("<name> best", collection_id=c.id)
client.snapshots.add_note(s.id, "Best: snapshot <id> (commit ...), better than <id> because ...")
```

## Picking up after an interruption

Do not rely on your notes having any particular form. The platform has what you need:

- `client.attachments.list(name="<name>")`: every snapshot, with its parent.
- `client.snapshots.list(collection_id=c.id)`: the latest one names the current best snapshot.
- `client.collections.get(c.id)` and `client.collections.compare(c.id)`: the runs and how they stand.
- `client.runs.find("<client_request_id>")`: a run you submitted, even if you lost its id. Submitting the same request again under its idempotency key never starts a second run.
- `git log` in the attachment repository: what each commit changed.
