"""Harness versions pin the program their adapter installs (N-09): the upstream version is
part of the version's content, checked against the package registry when it answers, and
the publishing form gets the registry's release tags and recent releases through the
setup network. Adapters that pass on only some model parameters refuse the others."""

import json
from urllib.parse import unquote

import pytest
from support import create_pat, invite_member, running, setup_admin
from test_configuration import agent_body, codes, error, resolve

from maestro.services.upstream import UpstreamVersions
from maestro.testing.fakes import FakeExecutor

PACKAGE = "@acme/agent-cli"
TIMES = {
    "created": "2026-01-01T00:00:00.000Z",
    "modified": "2026-10-27T00:00:00.000Z",
    "1.0.0": "2026-09-01T00:00:00.000Z",
    "1.1.0": "2026-10-01T00:00:00.000Z",
    "1.2.0": "2026-10-20T00:00:00.000Z",
    "1.3.0-alpha.1": "2026-10-25T00:00:00.000Z",
    "1.2.0-linux-x64": "2026-10-20T00:00:01.000Z",
    "1.2.1": "2026-10-26T00:00:00.000Z",
}
DOCUMENT = {
    "name": PACKAGE,
    "dist-tags": {"latest": "1.2.1", "stable": "1.2.0", "alpha": "1.3.0-alpha.1"},
    "versions": {v: {} for v in TIMES if v[0].isdigit()},
    "time": TIMES,
}


class Registry:
    """Answers like an npm registry and records what was asked, through which proxy."""

    def __init__(self):
        self.asked: list[tuple[str, str | None]] = []
        self.down = False

    def __call__(self, url, proxy):
        from maestro.services.upstream import Unavailable

        self.asked.append((url, proxy))
        if self.down:
            raise Unavailable("ConnectError: no route")
        path = unquote(url.split("/", 3)[3])
        if path == PACKAGE:
            return 200, json.dumps(DOCUMENT).encode()
        if path.startswith(PACKAGE + "/"):
            return (200 if path.rsplit("/", 1)[1] in DOCUMENT["versions"] else 404), b"{}"
        return 404, b"{}"


@pytest.fixture
def executor():
    return FakeExecutor(
        harness_adapters={
            "fake-agent": ("anthropic", "openai", "openai-compatible"),
            "cli-agent": ("openai-compatible",),
        },
        harness_model_parameters={"cli-agent": frozenset({"reasoning_effort"})},
        harness_upstream={"cli-agent": {"package": PACKAGE, "tested": "1.2.0"}},
    )


@pytest.fixture
def registry(service):
    fake = Registry()
    service.models.versions = UpstreamVersions(service.setup_networks.current, get=fake)
    return fake


def publish(admin, expect=201, **body):
    payload = {
        "name": "cli",
        "version": "1",
        "agent": "cli-agent",
        "protocols": ["openai-compatible"],
        **body,
    }
    return admin.post("/api/v1/admin/harness-versions", json=payload, expect=expect)


def test_publishing_pins_an_exact_upstream_version(service, registry):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        adapters = {
            a["agent"]: a
            for a in admin.get("/api/v1/admin/harness-adapters", expect=200).json()["data"]
        }
        assert adapters["cli-agent"]["package"] == PACKAGE
        assert adapters["cli-agent"]["tested_version"] == "1.2.0"
        assert adapters["cli-agent"]["model_parameters"] == ["reasoning_effort"]
        assert adapters["fake-agent"]["package"] is None

        for bad in (None, "latest", "^1.2.0", "1.x", "v1.2.0"):
            refused = publish(admin, expect=None, upstream_version=bad)
            assert error(refused) == (422, "HARNESS_INVALID"), bad
            assert refused.json()["error"]["field_errors"][0]["path"] == "/upstream_version"
        missing = publish(admin, expect=None, upstream_version="9.9.9")
        assert error(missing) == (422, "HARNESS_INVALID")
        assert missing.json()["error"]["field_errors"][0]["code"] == "NOT_FOUND"
        installs_nothing = admin.post(
            "/api/v1/admin/harness-versions",
            json={
                "name": "plain",
                "version": "1",
                "agent": "fake-agent",
                "protocols": ["anthropic"],
                "upstream_version": "1.0.0",
            },
        )
        assert installs_nothing.json()["error"]["field_errors"][0]["code"] == "UNSUPPORTED"

        first = publish(admin, upstream_version="1.2.0").json()["data"]
        assert first["upstream_version"] == "1.2.0"
        second = publish(admin, version="2", upstream_version="1.2.1").json()["data"]
        # The same harness at another program version is other content.
        assert first["content_digest"] != second["content_digest"]

        # A registry that cannot be asked does not stop publishing a well-formed version.
        registry.down = True
        offline = publish(admin, version="3", upstream_version="1.4.0").json()["data"]
        assert offline["upstream_version"] == "1.4.0"


def test_the_form_lists_releases_through_the_setup_network(service, registry):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        member = invite_member(service, admin, "member")
        url = "/api/v1/admin/harness-adapters/cli-agent/upstream-versions"
        assert member.get(url).status_code == 403
        assert create_pat(admin).get(url).status_code == 403
        assert (
            admin.get("/api/v1/admin/harness-adapters/fake-agent/upstream-versions").status_code
            == 404
        )

        found = admin.get(url, expect=200).json()["data"]
        assert found["registry"] == "https://registry.npmjs.org" and found["error"] is None
        assert found["tested_version"] == "1.2.0"
        assert [(t["tag"], t["version"]) for t in found["tags"]] == [
            ("stable", "1.2.0"),
            ("latest", "1.2.1"),
        ]
        # Releases only, newest first: no alpha, no platform build.
        assert [r["version"] for r in found["recent"]] == ["1.2.1", "1.2.0", "1.1.0", "1.0.0"]
        assert found["recent"][0]["published_at"] == TIMES["1.2.1"]
        assert registry.asked == [("https://registry.npmjs.org/@acme%2Fagent-cli", None)]

        admin.get(url, expect=200)
        assert len(registry.asked) == 1  # cached
        admin.get(url + "?refresh=true", expect=200)
        assert len(registry.asked) == 2

        admin.put(
            "/api/v1/admin/setup-network",
            json={
                "proxy": "http://10.0.0.1:3128",
                "no_proxy": "",
                "env": {"npm_config_registry": "https://registry.npmmirror.com/"},
                "harness_setup_timeout_sec": None,
                "note": "mirror",
                "expected_revision": 0,
            },
            expect=200,
        )
        mirrored = admin.get(url, expect=200).json()["data"]
        assert mirrored["registry"] == "https://registry.npmmirror.com"
        assert registry.asked[-1] == (
            "https://registry.npmmirror.com/@acme%2Fagent-cli",
            "http://10.0.0.1:3128",
        )

        registry.down = True
        failed = admin.get(url + "?refresh=true", expect=200).json()["data"]
        assert failed["error"].startswith("ConnectError") and failed["tags"] == []


def test_runs_carry_the_upstream_version_and_refuse_unknown_model_parameters(
    service, registry, version_id
):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        harness = publish(admin, upstream_version="1.2.0").json()["data"]

        def model(name, params):
            return admin.post(
                "/api/v1/admin/model-configs",
                json={
                    "name": name,
                    "provider": "demo",
                    "protocol": "openai-compatible",
                    "endpoint": "https://models.example.test/v1",
                    "model_id": "demo-model",
                    "params": params,
                },
                expect=201,
            ).json()["data"]["id"]

        fine = resolve(
            admin, version_id, **agent_body(model("ok", {"reasoning_effort": "low"}), harness["id"])
        )
        assert codes(fine) == []
        assert fine["effective_config"]["harness"]["upstream_version"] == "1.2.0"
        refused = resolve(
            admin, version_id, **agent_body(model("hot", {"temperature": 0}), harness["id"])
        )
        assert codes(refused) == ["HARNESS_MODEL_PARAMS_UNSUPPORTED"]
        assert "temperature" in refused["blockers"][0]["message"]
