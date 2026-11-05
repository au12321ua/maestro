"""Shared runs helpers for tests."""

import uuid


def rerun(api, run_id, expect=None, key=None, **body):
    response = api.post(
        f"/api/v1/runs/{run_id}/rerun",
        json=body,
        headers={"Idempotency-Key": key or uuid.uuid4().hex},
    )
    if expect is not None:
        assert response.status_code == expect, response.text
    return response


def accept(service, owner, version_id, **body):
    import uuid

    _, data, _ = service.runs.submit(
        owner,
        uuid.uuid4().hex,
        {"name": "r", "dataset_version_id": version_id, "execution_mode": "oracle", **body},
    )
    return data


def trials_by_key(service, run_id):
    out = {}
    for trial in service.runs.trials(run_id):
        out.setdefault(trial["task_key"], []).append(trial)
    return out


async def _drain(service):
    service.worker.start_epoch()
    await service.worker.drain()


def batch_body(version_id, *configs, name="sweep") -> dict:
    return {
        "name": name,
        "runs": [
            {"name": f"member {i}", "dataset_version_id": version_id, **config}
            for i, config in enumerate(configs)
        ],
    }


def post_batch(api, body, key=None):
    return api.post(
        "/api/v1/run-batches", json=body, headers={"Idempotency-Key": key or uuid.uuid4().hex}
    )
