# Attachment improvement workflow

Use `maestro_sdk.workflows.attachments` with your `MaestroClient`. These helpers only
compose the public HTTP API and write local files; they do not submit runs or decide
which snapshot to keep. Follow the installed `maestro-package` skill for decisions.

1. `write_task_briefs(client, version_id, directory, tasks=[key_or_id, ...])` writes
   `instruction.md` and `metadata.json` below each task id. Pass development tasks only:
   each brief is recorded as a read of that task, and held-out tasks should stay unread.
   Read the selected tasks and record your hypothesis and what evidence would support it.
2. Publish your candidate, then call
   `prepare_candidate(client, collection_id, parent_id, candidate_id, tasks=[...])`.
   Inspect the resolution, planned trials, blockers, file diff and recorded parent
   installs. The install hint reports facts; whether to reuse evidence is your choice.
   Submit separately with `client.runs.submit` only within the agreed scope and budget.
3. `collect_evidence(client, run_ids, directory, tasks=[...], trial_ids=[...])` saves
   artifacts incrementally, `manifest.json`, and `event-index.json`. Filters intersect;
   run-level files remain included. Indexes locate completed commands in Codex,
   Claude Code and both OpenCode event streams, with output pointers. ATIF indexes
   retain recorded terminal keystrokes and identify shared batch output; they never
   infer shell execution from terminal text. Unknown formats and unstructured ATIF
   raw content are listed as not indexed with a reason.
   Check the raw records before counting or classifying events. Artifacts can expire.
4. `build_report(client, collection_id, directory)` writes `report.json` and `report.md`
   with every failure kept, overriding saved exclusions. Completeness requires no
   dropped tasks, all tasks common, and every compared run's scored trials equal its
   planned trials. Unknown usage/cost stays unknown. This does not decide what is better.

5. `save_candidate(client, collection_id, snapshot_id, hypothesis=..., ...)` records a
   published snapshot as a candidate of the collection (hypothesis, change kind,
   evidence references and excerpts); `decide_candidate(client, record_id, decision,
   reason=...)` appends a decision; `candidates(client, collection_id)` lists them
   with their latest decision and runs. Point the collection at the best one with
   `client.collections.set_current_best(collection_id, snapshot_id, reason=...)`.

Your reasoning, dead ends and working notes stay in `notes/`, in any form.
