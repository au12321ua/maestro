# Comparing runs

## One-off comparison

`client.comparisons.compare([run_a, run_b])` is computed on demand and never saved. It returns what differs between the runs' configurations, per-run statistics over the tasks they have in common, and a cell for each task.

- External failures (rate limits, environment build, platform stops) are left out by default; the remaining failures count as failed with a score of 0. Pass `exclude=[]` to keep every failure.
- `scope="all"` uses every task instead of the common ones, without differences between runs. With the default `scope="common"`, differences are given against `baseline_run_id` (default: the first run).

Next to the numbers, report:

- `dropped_tasks`: the tasks that were not compared.
- The excluded counts, by cause.
- Any `warnings`.

Never compare an oracle run with a model run.

## Collections

A collection is a run list the team keeps up. Its specification is fixed at creation: which dimensions may vary, and the values of the others. Only runs that match get in.

```python
c = client.collections.create("models round 1", "models", base_run_id=run_id, run_ids=[...])
client.runs.submit(cfg, name="...", collection_id=c.id)   # fixed fields are filled in; a mismatch is refused
client.collections.compare(c.id)
```

- Scenarios: `models`, `harnesses`, `model_harness`, `attachments`, `repeats`, and `custom` with `variable_dimensions`.
- The fixed values come from `base_run_id` or a full `base_request`.
- `client.collections.candidates(c.id)` lists recent runs that are not in the collection; those with an empty `mismatches` can join with `client.collections.add_runs(c.id, [run_id])`.
- `client.collections.copy(c.id, ...)` makes a new collection with another specification; `not_carried` lists the runs it left out.

## Recording a conclusion

Once the runs have ended, freeze the comparison and write the conclusion next to it:

```python
s = client.snapshots.create("round 1", collection_id=c.id)
client.snapshots.add_note(s.id, "text")
```

Your note is stored apart from the computed table, and only its author may change it (`client.snapshots.update_note`).
