"""Shared configuration helpers for tests."""


def resolve(api, version_id, expect=201, **body):
    payload = {"dataset_version_id": version_id, "execution_mode": "oracle", **body}
    return api.post("/api/v1/config-resolutions", json=payload, expect=expect).json()["data"]


def agent_body(model_id, harness_id, **extra):
    return {
        "execution_mode": "agent",
        "model_config_id": model_id,
        "harness_version_id": harness_id,
        **extra,
    }


def codes(resolution) -> list[str]:
    return [b["code"] for b in resolution["blockers"]]


def error(response) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


def prepare_model(admin) -> tuple[str, str]:
    """The administrator's part: a credential, a model bound to it, a harness version."""
    credential = admin.post(
        "/api/v1/admin/credentials",
        json={
            "name": "demo-key",
            "kind": "model_api_key",
            "provider": "demo",
            "value": "sk-demo-not-a-real-key-0000",
        },
        expect=201,
    ).json()["data"]
    model = admin.post(
        "/api/v1/admin/model-configs",
        json={
            "name": "demo",
            "provider": "demo",
            "protocol": "openai-compatible",
            "endpoint": "https://models.example.test/v1",
            "model_id": "demo-model",
            "params": {"temperature": 0},
            "credential_id": credential["id"],
        },
        expect=201,
    ).json()["data"]
    harness = admin.post(
        "/api/v1/admin/harness-versions",
        json={
            "name": "fake-harness",
            "version": "1.0.0",
            "agent": "fake-agent",
            "protocols": ["openai-compatible"],
        },
        expect=201,
    ).json()["data"]
    return model["id"], harness["id"]


def credential_of(admin, model_id: str) -> str:
    return admin.get(f"/api/v1/model-configs/{model_id}", expect=200).json()["data"][
        "credential_id"
    ]
