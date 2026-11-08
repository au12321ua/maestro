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

**Read the tasks first.** Before the first attempt, read the instruction of every task you will tune on (the development tasks, never the held-out ones), and any checks you are allowed to read. Opening a task's instruction or a trial's files is recorded as a read of that task:

```python
for task in client.catalog.tasks(version_id):          # keys and ids only, not a read
    if task.id in development_ids:
        print(task.task_key, client.catalog.task(task.id).instruction)
```

Write down, per task or for the set: the inputs, what the agent must deliver and where, what the score measures, and what you do not know. Most wrong hypotheses come from guessing these.

**Helpers for the mechanical work.** `maestro_sdk.workflows.attachments` does the repetitive parts through the public API and only states facts; the judgements stay yours:

```python
from maestro_sdk.workflows import attachments as aw

aw.write_task_briefs(client, version_id, "briefs", tasks=task_keys)   # instructions to local files
plan = aw.prepare_candidate(client, c.id, parent.id, snapshot.id)      # resolve, never submit
aw.collect_evidence(client, [run_id], "evidence")   # incremental downloads + trajectory event index
aw.build_report(client, c.id, "report")             # report.json / report.md, every failure kept
```

`prepare_candidate` lists the planned trials, the file diff with the parent and whether `bootstrap.sh` changed and the parent installed on this harness; whether that is enough to skip an install check is your call.

**Set up the comparison once.** Run the current best snapshot on the chosen tasks and repetitions, then build an attachment A/B collection from that run, so every later run differs only in the attachment:

```python
c = client.collections.create("<name> on <dataset>", "attachments", base_run_id=base_run_id)
```

Fix the task list with `task_selection` (`{"mode": "explicit", "task_ids": [...]}`) and use the same list every time: the collection does not check the task range.

**Keep held-out tasks.** If you will tune on results, mark some tasks of the dataset as held out when you create the collection; the marks are fixed once it exists:

```python
c = client.collections.create("<name> on <dataset>", "attachments", base_run_id=base_run_id,
                              held_out_task_ids=held_out_ids)
```

Tune on the development tasks only. Do not open the held-out tasks' instructions, trajectories, logs or archives; their scores are fine to see. Submit the held-out tasks at the end, for the snapshot you want to confirm. The platform does not block reads, it records them: before reporting, look at `client.collections.held_out(c.id)` (who read each held-out task and when, `read_since_creation`, `accepted_runs`) and include it, so whoever writes the conclusion can judge whether the held-out tasks were still unseen.

**Each attempt:**

1. Start from a snapshot (the current best or an earlier attempt). Read its results and trajectories, and your notes.
2. Write the hypothesis and the evidence for it: run, trial, artifact path, and an excerpt of the event. Artifacts expire after the retention period, so excerpts keep your reasons readable later.
3. Say what kind of change it is, because each kind is judged by its own measure and one does not prove another:
   - **installation**: the attachment installs and the agent can find it (install log, the agent reading the skill);
   - **execution reliability**: fewer crashes, timeouts or tool errors (failure groups, error events);
   - **delivery**: more trials hand in a valid deliverable (deliverable present and accepted by the checks);
   - **quality**: better scores on what was delivered (scores over the same tasks and repetitions).

   A reliability or delivery gain is not a quality gain, even if the mean rises with it; say which one you saw.
4. Before submitting, write down what evidence would support which decision: that the mechanism happened (the agent did the new thing), that the snapshot is worth keeping for now, or that it is better overall. Decide this before the numbers come in. If the starting snapshot has unscored trials, report only the mechanism evidence; do not add trials just to fill the gap unless the person you work for asked for it.
5. Change the files on a branch, commit, and publish with `parent=` the starting snapshot. No label is needed: snapshots are identified by id, and the same content published again returns the existing snapshot. Record it as a candidate of the collection, so the team sees the hypothesis next to the runs (saving the same snapshot again returns the existing record):

   ```python
   record = aw.save_candidate(client, c.id, snapshot.id, parent_snapshot_id=parent.id,
                              hypothesis="...", change_kind="delivery", invalid_if="...",
                              references=[{"type": "run", "id": run_id}],
                              excerpts=[{"text": "<the lines that show it>"}])
   ```

   The fields above go in the record body, which `candidates` and `decide_candidate` read. If people need more than they say, pass `text="..."` (Markdown) as a note for them; do not repeat the body in it.
6. Submit into the collection with a `client_request_id` you can derive again, such as `<name>-<first 8 characters of the starting snapshot id>-<n>`:

   ```python
   client.runs.submit({"attachments": [snapshot.id], "task_selection": tasks}, name="...",
                      collection_id=c.id, client_request_id="...")
   ```

7. When the runs end, compare against the starting point task by task, and read the new trajectories, not only the means.
8. Record the decision on the candidate (`keep`, `discard`, `insufficient_evidence` or `undecided`) with the reason and how strong the evidence is; earlier decisions stay:

   ```python
   aw.decide_candidate(client, record.id, "discard", reason="...", evidence_level="mechanism observed")
   ```

9. Write down what you learned, including what did not work.

**Notes hold what the platform does not.** Snapshots, runs, comparisons, frozen snapshots and candidate records are already kept by the platform; refer to them by id instead of copying their contents into your notes. An attachment A/B collection already guarantees that everything except the attachment is the same, so there is no need to compare configurations yourself.

**Counting events in trajectories.** When a claim rests on how often something happened (errors of a kind, uses of a tool), find each occurrence (file and line; the event index from `collect_evidence` lists recorded commands with their position and exit code), read it to confirm it is what you think, then count. Do not count by pattern alone: one command can span several lines, and similar text can mean different things. Write down how you counted, so someone else can repeat it.

**Judging "better".** Compare with every failure kept and check the evidence is complete before reading the numbers:

```python
cmp = client.collections.compare(c.id, exclude=[], baseline_run_id=start_run_id)
complete = cmp.dropped_tasks == 0 and cmp.common_tasks == cmp.all_tasks and all(
    r.scored_trials == r.planned_trials for r in cmp.runs
)
```

- With held-out marks, `cmp` is the development tasks and `cmp.held_out` the held-out ones, each with the same fields; judge them separately and never average them together.
- Not complete (unscored trials, execution failures, missing tasks): rerun or report "not enough evidence". Do not count them as zero and do not narrow to the common tasks.
- Complete: `r.mean` and `r.difference` are the macro means over the same tasks and repetitions. A valid 0 is a real score.
- Small differences over few tasks are often noise. Prefer more repetitions or tasks to a confident claim.

**The best attachment.** When a snapshot is judged better than the current best, point the collection at it, with the reason; the change log keeps who changed it, when and why, so anyone can find it without your notes:

```python
client.collections.set_current_best(c.id, snapshot.id, reason="better than <id> on ... because ...")
```

Freeze the comparison as well (`client.snapshots.create`) when you want the numbers behind the decision kept as they are.

## Picking up after an interruption

Do not rely on your notes having any particular form. The platform has what you need:

- `client.attachments.list(name="<name>")`: every snapshot, with its parent.
- `client.collections.get(c.id).current_best`: the current best snapshot and why it was chosen.
- `aw.candidates(client, c.id)`: every candidate with its hypothesis, latest decision and runs.
- `client.collections.get(c.id)` and `client.collections.compare(c.id)`: the runs and how they stand.
- `client.runs.find("<client_request_id>")`: a run you submitted, even if you lost its id. Submitting the same request again under its idempotency key never starts a second run.
- `git log` in the attachment repository: what each commit changed.
