# Follow-up runs

## Same configuration again

`client.runs.rerun(run_id)` starts a new run from the source run's fixed snapshot, so team defaults that changed since then do not apply.

- `mode="all"` (default) reruns every task.
- `mode="unfinished"` reruns only the tasks without a score.
- `mode="explicit", task_ids=[...]` reruns a chosen subset.

The rerun belongs to you, not to the source run's submitter. It takes `idempotency_key` and `client_request_id` like `submit` does. If the service answers `RERUN_CONFIG_CHANGED`, use a modified run instead.

## Change something

```python
req = client.runs.request(run_id)          # the request that reproduces the run
# edit the fields that should change
client.runs.submit(req, name="...", source_run_id=run_id)
```

`source_run_id` makes the service record what differs from the source. Resolve the edited request first when you are not sure it is valid.

## Several configurations, all or none

`client.batches.preview(name, [c1, c2])` checks every configuration without accepting anything. `client.batches.submit(name, [c1, c2])` then accepts all of them or none; each run costs one unit of PAT quota. Wait on `submitted.operation_id`, read the batch with `client.batches.get(batch_id)` and stop it with `client.batches.cancel(batch_id)` only when asked.

## Plans

A plan is a saved partial configuration: `client.plans.list()`, `client.plans.get(plan_id)`, `client.plans.create(name, template)`. Put `plan_id` in a configuration to use one; the run fixes the plan revision it used. Only the creator may change a plan, with `client.plans.update(plan_id, expected_revision, ...)`.

## Filter runs

`client.runs.list(...)` returns every page, newest first. Filters: `owner_id`, `state`, `dataset_version_id`, `model_config_id`, `harness_version_id`, `attachment_version_id`, `plan_id`, `client_request_id`, `accepted_after` and `accepted_before` (RFC 3339 UTC, such as `"2026-10-24T00:00:00Z"`).

## Export for analysis

```python
e = client.exports.create([run_a, run_b])                  # include_artifacts=False for records only
client.operations.wait(e.operation_id)
client.exports.download(e.id, "runs.zip")                  # the digest is checked
```

To keep only the files of one run, `client.runs.download_archive(run_id, "run.zip")` saves them as one zip (ended runs only); its `manifest.json` lists the files left out and why.
