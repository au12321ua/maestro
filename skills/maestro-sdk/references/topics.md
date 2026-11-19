# Resume work from an experiment topic

Use the topic link provided by the user. If they give a name, `client.topics.list(q=name)` returns candidates with IDs and purposes. Ask which candidate they mean when several match. A topic keeps the same URL after a rename.

```python
client.require("topics")
topic = client.topics.open(topic_link)
overview = topic.overview()
print(overview.topic.purpose)
if overview.latest_handoff:
    note = overview.latest_handoff
    print(note.author_username, note.created_at, note.text, note.references)
for run in overview.active_runs:
    print(run.id, run.state, run.scoring.scored_trials, run.scoring.planned_trials)
for snapshot in overview.used_attachment_snapshots:
    print(snapshot.id, snapshot.label, snapshot.source_run_ids)
```

Use `topic.runs.list(active=True, limit=25)` for the rest of the active runs. Use `topic.references("collection")` to find existing comparisons, `topic.materials("attachment_snapshot")` to confirm the exact installed snapshots and `topic.handoffs()` for older handoffs. Open individual trials and artifacts when you need their evidence.

If a follow-up is needed and authorized, resolve it and submit through this handle. Use the same handle, request and key when retrying an uncertain submission. A second topic handle can be used concurrently. `topic.batches.submit(...)` sends the whole batch to the bound topic, and `topic.runs.rerun(...)` applies the same destination to a rerun.

```python
resolution = client.configs.resolve(follow_up_config).raise_for_blockers()
submitted = topic.runs.submit(
    name="Verifier follow-up",
    resolution_id=resolution.resolution_id,
    client_request_id="verifier-follow-up",
)
# Record the decision and point the next person to the evidence.
client.records.create(
    {"type": "topic", "id": topic.id},
    "handoff",
    "Verify whether the timeout recurs",
    text="The verifier timeout appeared in the baseline. The follow-up isolates that task.",
    references=[{"type": "run", "id": submitted.run_id}, {"type": "trial", "id": baseline_trial_id}],
)
print(topic.url)
```

Adding an existing run, collection, dataset or attachment snapshot uses `client.topics.change_references(..., add=[{"kind": ..., "id": ...}])`. Share the same link and leave scores, state and evidence in the underlying objects.
