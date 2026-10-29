"""Review fixes for the Docker preparation environment, against a fake docker client:
failure classification, the author's build limit, the start-up sweeps and the local image
registry (N-01). Nothing here starts Docker or a registry; real-Docker coverage stays in
test_harbor_docker.py (opt-in)."""

import asyncio
import json
import stat
import sys
import textwrap

import pytest

from maestro.boundaries.environment import (
    AUTH,
    FAILED,
    MISMATCH,
    TRANSIENT,
    UNAVAILABLE,
    PreparationError,
    PreparationSpec,
    PreparedImage,
    PreparedOutput,
)
from maestro.execution.materials import MAX_BUILD_TIMEOUT, image_key
from maestro.execution.preparation import (
    DEFAULT_BUILD_TIMEOUT,
    DockerEnvironment,
    classify,
)
from maestro.services.preparation import PreparationFailure, failure

SPEC_DIGEST = "sha256:" + "5c" * 32
DIGEST = "sha256:" + "17" * 32
OTHER = "sha256:" + "18" * 32
REGISTRY = "127.0.0.1:1"


def log(*lines: str) -> str:
    """One captured stderr, a line per argument."""
    return "\n".join(lines)


REGISTRY_REFUSALS = [
    # docker pull of a private or missing repository
    log(
        "Error response from daemon: pull access denied for acme/private, repository does not "
        "exist or may require 'docker login': denied: requested access to the resource is denied"
    ),
    log(
        'Error response from daemon: Head "https://registry-1.docker.io/v2/acme/private/'
        'manifests/latest": unauthorized: authentication required'
    ),
    log('Error response from daemon: Get "https://ghcr.io/v2/": no basic auth credentials'),
    # buildx resolving the base image
    log(
        "ERROR: failed to solve: acme/base:1.0: failed to resolve source metadata for "
        "docker.io/acme/base:1.0: failed to authorize: failed to fetch anonymous token: "
        "unexpected status from GET request to https://auth.docker.io/token?scope=repository"
        "%3Aacme%2Fbase%3Apull&service=registry.docker.io: 401 Unauthorized"
    ),
]
FAILS_EVERY_TIME = [
    log("ERROR: failed to solve: dockerfile parse error on line 3: unknown instruction: RUNN"),
    log(
        "ERROR: failed to solve: failed to compute cache key: failed to calculate checksum of "
        'ref x: "/requirements.txt": not found'
    ),
    log('#8 ERROR: process "/bin/sh -c make -j4" did not complete successfully: exit code: 2'),
    # a RUN step that talks to a package index which refuses it: the task's own problem
    log(
        "#7 3.2 ERROR: HTTP error 401 while getting https://pypi.example/simple/x/",
        "#7 3.2 requests.exceptions.HTTPError: 401 Client Error: Unauthorized for url",
        '#7 ERROR: process "/bin/sh -c pip install -r requirements.txt" did not complete '
        "successfully: exit code: 1",
    ),
    log(
        "#6 1.1 E: Failed to fetch http://deb.debian.org/debian/pool/x.deb  403  Forbidden "
        "[IP: 151.101.2.132 80]",
        '#6 ERROR: process "/bin/sh -c apt-get install -y x" did not complete successfully: '
        "exit code: 100",
    ),
    log(
        "#5 0.9 curl: (22) The requested URL returned error: 403",
        "#5 0.9 AccessDenied: denied: token expired",
        '#5 ERROR: process "/bin/sh -c curl -fsSL https://x/y" did not complete successfully: '
        "exit code: 22",
    ),
    # the failure summary block repeats the step output without the "#N" prefix
    log(
        'ERROR: failed to solve: process "/bin/sh -c pip install x" did not complete '
        "successfully: exit code: 1",
        "------",
        " > [2/3] RUN pip install x:",
        "0.456 ERROR: HTTP error 401 while getting https://x",
        "0.456 401 Client Error: Unauthorized for url",
        "------",
    ),
    # "EOF" as a heredoc delimiter is not an end-of-file error
    log(
        "#5 [2/3] RUN <<EOF (set -e; make)",
        '#5 ERROR: process "/bin/sh -c set -e; make" did not complete successfully: exit code: 2',
    ),
    log(
        "Error response from daemon: no matching manifest for linux/arm64/v8 in the manifest "
        "list entries"
    ),
]
KNOWN_TRANSIENT = [
    log(
        "ERROR: failed to solve: ubuntu:22.04: failed to resolve source metadata for "
        'docker.io/library/ubuntu:22.04: failed to do request: Head "https://registry-1.'
        'docker.io/v2/library/ubuntu/manifests/22.04": net/http: TLS handshake timeout'
    ),
    # the token endpoint cannot be reached: "failed to authorize" but no refusal
    log(
        "ERROR: failed to solve: x: failed to authorize: failed to fetch anonymous token: Get "
        '"https://auth.docker.io/token": dial tcp: lookup auth.docker.io on 127.0.0.53:53: '
        "read udp 127.0.0.1:41234->127.0.0.53:53: i/o timeout"
    ),
    log(
        'Error response from daemon: Get "https://registry-1.docker.io/v2/": net/http: request '
        "canceled while waiting for connection (Client.Timeout exceeded while awaiting headers)"
    ),
    log(
        "error pulling image configuration: download failed after attempts=6: read tcp "
        "10.0.0.2:51234->104.18.123.25:443: read: connection reset by peer"
    ),
    log("Error response from daemon: received unexpected HTTP status: 503 Service Unavailable"),
    log('Error response from daemon: Get "https://registry-1.docker.io/v2/": EOF'),
    log("Error response from daemon: toomanyrequests: You have reached your pull rate limit."),
    log(
        "#7 5.2 W: Failed to fetch http://deb.debian.org/debian/dists/bookworm/InRelease  "
        "Temporary failure resolving 'deb.debian.org'",
        '#7 ERROR: process "/bin/sh -c apt-get update" did not complete successfully: '
        "exit code: 100",
    ),
    log(
        "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. "
        "Is the docker daemon running?"
    ),
]


@pytest.mark.parametrize("stderr", REGISTRY_REFUSALS)
def test_registry_refusals_are_auth(stderr):
    assert classify(stderr) == AUTH


@pytest.mark.parametrize("stderr", FAILS_EVERY_TIME)
def test_deterministic_failures_are_neither_auth_nor_retried(stderr):
    assert classify(stderr) == FAILED


@pytest.mark.parametrize("stderr", KNOWN_TRANSIENT)
def test_known_passing_trouble_is_transient(stderr):
    assert classify(stderr) == TRANSIENT


def test_only_transient_causes_are_retried():
    """The worker retries what ``retryable`` allows: a build that fails every time
    must not be run three times."""
    assert PreparationFailure(failure(TRANSIENT, "x")).retryable
    for kind in (AUTH, FAILED):
        assert not PreparationFailure(failure(kind, "x")).retryable
    assert failure(FAILED, "x")["code"] == "PREPARATION_RESTORE_FAILED"


# --- a fake docker client ---------------------------------------------------------------


class FakeDocker:
    """A docker executable that records its arguments and answers from rules: the first
    rule whose text occurs in the joined arguments decides exit code, stdout, stderr."""

    def __init__(self, directory):
        self.directory = directory
        self.rules: list[dict] = []
        self.path = directory / "docker"
        self.path.write_text(
            textwrap.dedent(
                f"""\
                #!{sys.executable}
                import json, sys
                from pathlib import Path
                state = Path({str(directory)!r})
                line = " ".join(sys.argv[1:])
                with (state / "calls.log").open("a") as log:
                    log.write(line + "\\n")
                for rule in json.loads((state / "rules.json").read_text()):
                    if rule["match"] in line:
                        sys.stdout.write(rule["out"])
                        sys.stderr.write(rule["err"])
                        sys.exit(rule["code"])
                """
            )
        )
        self.path.chmod(self.path.stat().st_mode | stat.S_IXUSR)
        self._save()  # unmatched calls succeed quietly

    def respond(self, match: str, code=0, out="", err="") -> None:
        self.rules.append({"match": match, "code": code, "out": out, "err": err})
        self._save()

    def _save(self) -> None:
        (self.directory / "rules.json").write_text(json.dumps(self.rules))

    def calls(self) -> list[str]:
        log = self.directory / "calls.log"
        return log.read_text().splitlines() if log.exists() else []


@pytest.fixture
def docker(tmp_path):
    directory = tmp_path / "fake-docker"
    directory.mkdir()
    return FakeDocker(directory)


@pytest.fixture
def environment(tmp_path, docker):
    env = DockerEnvironment(REGISTRY, docker=str(docker.path), legacy_dir=tmp_path / "preparations")
    env.bind("inst-1")
    return env


def build_entry(role="environment/main", context="environment") -> dict:
    return {
        "role": role,
        "source": "build",
        "context": context,
        "dockerfile": "Dockerfile",
        "args": {},
        "target": None,
        "definition_digest": "sha256:" + "de" * 32,
    }


def make_spec(tmp_path, kind="build", docker_image=None, known=None, **top) -> PreparationSpec:
    task = tmp_path / "task"
    (task / "environment").mkdir(parents=True, exist_ok=True)
    (task / "environment" / "Dockerfile").write_text("FROM scratch\n")
    if kind == "build":
        entry = build_entry()
    else:
        entry = {"role": "environment/main", "source": "pull", "reference": docker_image}
    spec = {"images": [entry], **top}
    return PreparationSpec(SPEC_DIGEST, "demo", task, spec, known=known or {})


def registry_answers(environment, digest=DIGEST):
    """The registry reports ``digest`` for any pushed tag and deletes what it is asked to."""
    deleted = []

    async def reported(key):
        return digest

    async def delete(value):
        deleted.append(value)

    environment._registry_digest = reported
    environment._delete_manifest = delete
    return deleted


async def no_progress(phase: str) -> None:
    return None


def prepare_error(environment, spec) -> PreparationError:
    with pytest.raises(PreparationError) as caught:
        asyncio.run(environment.prepare(spec, no_progress))
    return caught.value


# --- failures through the real code paths --------------------------------------------------


@pytest.mark.parametrize(
    ("stderr", "kind"),
    [
        (REGISTRY_REFUSALS[3], AUTH),
        (FAILS_EVERY_TIME[3], FAILED),
        (FAILS_EVERY_TIME[0], FAILED),
        (KNOWN_TRANSIENT[0], TRANSIENT),
    ],
)
def test_a_failed_build_is_classified_and_its_temporary_tag_removed(
    tmp_path, docker, environment, stderr, kind
):
    docker.respond("buildx build", code=1, err=stderr)
    error = prepare_error(environment, make_spec(tmp_path))
    assert error.kind == kind
    assert error.evidence["stderr_tail"].endswith(stderr[-200:])
    removed = [
        c for c in docker.calls() if c.startswith(f"image rm --no-prune {environment.repository}")
    ]
    assert len(removed) == 1 and "-tmp-" in removed[0]


@pytest.mark.parametrize(
    ("stderr", "reference", "kind"),
    [
        (REGISTRY_REFUSALS[0], "acme/private:1", AUTH),
        (REGISTRY_REFUSALS[1], "acme/private@sha256:" + "ab" * 32, AUTH),
        ("Error response from daemon: manifest unknown", "acme/x@sha256:" + "ab" * 32, UNAVAILABLE),
        (  # what the containerd image store says (Docker 29)
            'Error response from daemon: failed to resolve reference "acme/x@sha256:'
            + "ab" * 32
            + '": acme/x@sha256:'
            + "ab" * 32
            + ": not found",
            "acme/x@sha256:" + "ab" * 32,
            UNAVAILABLE,
        ),
        # a tag that is gone could also be a typo or a moved tag: not "exactly gone"
        ("Error response from daemon: manifest unknown", "acme/x:1", FAILED),
        (KNOWN_TRANSIENT[0], "acme/x:1", TRANSIENT),
        (FAILS_EVERY_TIME[7], "acme/x:1", FAILED),
    ],
)
def test_a_failed_pull_is_classified(tmp_path, docker, environment, stderr, reference, kind):
    docker.respond("pull --quiet", code=1, err=stderr)
    spec = make_spec(tmp_path, kind="image", docker_image=reference)
    assert prepare_error(environment, spec).kind == kind


# --- the author's build limit -----------------------------------------------------------


@pytest.mark.parametrize(
    ("requested", "used"),
    [
        (None, DEFAULT_BUILD_TIMEOUT),
        (float("inf"), DEFAULT_BUILD_TIMEOUT),
        (float("nan"), DEFAULT_BUILD_TIMEOUT),
        (-1, DEFAULT_BUILD_TIMEOUT),
        (0, DEFAULT_BUILD_TIMEOUT),
        (900, 900.0),
        (10**9, MAX_BUILD_TIMEOUT),
    ],
)
def test_the_build_runs_under_a_finite_capped_limit(tmp_path, environment, requested, used):
    timeouts = []

    async def run(*args, timeout):
        timeouts.append((args[0], timeout))
        return 0, "", ""

    environment._run = run
    spec = make_spec(tmp_path, build_timeout_sec=requested)
    entry = spec.spec["images"][0]
    asyncio.run(environment._build(spec, entry, "maestro-prep/inst-1:tmp", no_progress))
    assert timeouts == [("buildx", used)]


# --- start-up sweeps and eviction -------------------------------------------------------


def test_sweep_temporary_removes_only_unpublished_tags(docker, environment):
    repo = environment.repository
    docker.respond(
        "image ls",
        out=f"{repo}:aaaa\n{repo}:aaaa-tmp-1234abcd\n{repo}:bbbb-tmp-ffff0000\n<none>:<none>\n",
    )
    removed = asyncio.run(environment.sweep_temporary())
    assert removed == [f"{repo}:aaaa-tmp-1234abcd", f"{repo}:bbbb-tmp-ffff0000"]
    assert [c for c in docker.calls() if c.startswith("image rm")] == [
        f"image rm --no-prune {repo}:aaaa-tmp-1234abcd",
        f"image rm --no-prune {repo}:bbbb-tmp-ffff0000",
    ]


def test_sweep_temporary_does_nothing_unbound_or_when_docker_fails(tmp_path, docker):
    unbound = DockerEnvironment(REGISTRY, docker=str(docker.path))
    assert asyncio.run(unbound.sweep_temporary()) == []
    assert docker.calls() == []
    bound = DockerEnvironment(REGISTRY, docker=str(docker.path))
    bound.bind("inst-1")
    docker.respond("image ls", code=1, err="Cannot connect to the Docker daemon")
    assert asyncio.run(bound.sweep_temporary()) == []
    assert not [c for c in docker.calls() if c.startswith("image rm")]


def test_sweep_legacy_removes_the_old_originals(tmp_path, environment):
    """Originals from before the registry are not used again (U-54)."""
    assert environment.sweep_legacy() == []  # nothing there
    legacy = environment.legacy_dir
    legacy.mkdir()
    (legacy / ("5c" * 32 + ".tar")).write_bytes(b"original")
    assert environment.sweep_legacy() == ["5c" * 32 + ".tar"]
    assert not legacy.exists()


def image(role="environment/main", digest=DIGEST, entry=None) -> PreparedImage:
    key = image_key(entry or build_entry(role))
    return PreparedImage(role, digest, f"{REGISTRY}/maestro-prep/inst-1@{digest}", key)


def test_a_prepared_image_is_pushed_and_known_by_the_registry_digest(tmp_path, docker, environment):
    """Docker's own RepoDigests can name content the registry does not hold (an index of
    which only one platform was pushed): the digest comes from the registry."""
    registry_answers(environment)
    docker.respond("image inspect", out=OTHER + "\n")
    output = asyncio.run(environment.prepare(make_spec(tmp_path), no_progress))
    (prepared,) = output.images
    assert prepared.digest == DIGEST
    assert prepared.ref == f"{environment.repository}@{DIGEST}"
    assert prepared.key == image_key(build_entry())
    calls = docker.calls()
    assert any(c == f"push --quiet {environment.tag(prepared.key)}" for c in calls)


def test_an_identical_definition_is_reused_not_built(tmp_path, docker, environment):
    known = {image_key(build_entry()): image(digest=DIGEST)}
    docker.respond("image inspect", out=DIGEST + "\n")
    output = asyncio.run(environment.prepare(make_spec(tmp_path, known=known), no_progress))
    assert [i.digest for i in output.images] == [DIGEST]
    assert not [c for c in docker.calls() if c.startswith(("buildx", "push"))]


def test_restore_pulls_from_the_registry_by_digest(tmp_path, docker, environment):
    spec = make_spec(tmp_path)
    expected = PreparedOutput((image(),))

    async def present(ref):  # missing locally until it was pulled
        return f"pull --quiet {ref}" in docker.calls()

    environment._present = present
    restored = asyncio.run(environment.restore(spec, expected, no_progress))
    assert restored.content_id == expected.content_id
    assert f"pull --quiet {environment.ref(DIGEST)}" in docker.calls()
    assert not [c for c in docker.calls() if c.startswith("buildx")]


def test_an_image_the_registry_lost_is_rebuilt_and_must_match(tmp_path, docker, environment):
    spec = make_spec(tmp_path)
    expected = PreparedOutput((image(),))
    docker.respond(f"image inspect --format {{{{.Id}}}} {environment.ref(DIGEST)}", code=1)
    docker.respond(f"pull --quiet {environment.ref(DIGEST)}", code=1, err="manifest unknown")
    docker.respond("image inspect", out=OTHER + "\n")
    registry_answers(environment, OTHER)
    with pytest.raises(PreparationError) as caught:
        asyncio.run(environment.restore(spec, expected, no_progress))
    assert caught.value.kind == MISMATCH
    assert caught.value.evidence == {"expected_digest": DIGEST, "actual_digest": OTHER}


def test_eviction_keeps_what_another_task_uses(docker, environment):
    """Only the platform's own names go, and not those of an image another cache holds."""
    shared, own = image("environment/db", OTHER), image()
    docker.respond("image inspect", out=DIGEST + "\n")
    asyncio.run(environment.evict(PreparedOutput((own, shared)), frozenset({OTHER})))
    removed = [c for c in docker.calls() if c.startswith("image rm")]
    assert removed == [f"image rm {environment.ref(DIGEST)} {environment.tag(own.key)}"]


def test_discard_deletes_registry_manifests_nobody_records(docker, environment):
    deleted = registry_answers(environment)
    shared, own = image("environment/db", OTHER), image()
    docker.respond("image inspect", code=1)
    asyncio.run(environment.discard(PreparedOutput((own, shared)), frozenset({OTHER})))
    assert deleted == [DIGEST]


# What BuildKit (Docker 29.9.1, buildx 0.38) really printed for a Dockerfile whose RUN step
# says "pull access denied" and then fails, captured by the real-Docker test. The command
# text comes back three times without a step prefix: in the error line, as the instruction
# header and in the Dockerfile excerpt. None of it is a registry speaking.
REAL_RUN_STEP_FAILURE = """\
#4 [1/2] FROM docker.io/library/ubuntu:22.04@sha256:2edbbc5dc405e9612ba3584ce95480277e3eb374407b5505fe26f17df77c7dbc
#4 CACHED

#5 [2/2] RUN echo "401 Unauthorized: pull access denied, requested access to the resource is denied" && exit 3
#5 0.099 401 Unauthorized: pull access denied, requested access to the resource is denied
#5 ERROR: process "/bin/sh -c echo \\"401 Unauthorized: pull access denied, requested access to the resource is denied\\" && exit 3" did not complete successfully: exit code: 3
------
 > [2/2] RUN echo "401 Unauthorized: pull access denied, requested access to the resource is denied" && exit 3:
0.099 401 Unauthorized: pull access denied, requested access to the resource is denied
------
Dockerfile:2
--------------------
   1 |     FROM ubuntu:22.04
   2 | >>> RUN echo "401 Unauthorized: pull access denied, requested access to the resource is denied" && exit 3
   3 |     
--------------------
ERROR: failed to build: failed to solve: process "/bin/sh -c echo \\"401 Unauthorized: pull access denied, requested access to the resource is denied\\" && exit 3" did not complete successfully: exit code: 3
"""


def test_a_failed_run_step_is_a_failed_build_even_when_its_command_names_a_refusal():
    assert classify(REAL_RUN_STEP_FAILURE) == FAILED


def test_the_command_text_of_a_failed_run_step_is_not_read_as_network_trouble_either():
    # The command only mentions the words (in a shell comment); the step printed nothing.
    command = "true && exit 3 # connection refused, service unavailable"
    echoed = (
        f"#5 [2/2] RUN {command}\n"
        f'#5 ERROR: process "/bin/sh -c {command}" did not complete successfully: exit code: 3\n'
        f"------\n > [2/2] RUN {command}:\n------\n"
        f"Dockerfile:2\n--------------------\n   2 | >>> RUN {command}\n"
        f"--------------------\n"
        f'ERROR: failed to build: failed to solve: process "/bin/sh -c {command}" did not '
        "complete successfully: exit code: 3\n"
    )
    assert classify(echoed) == FAILED


def test_network_trouble_in_the_output_of_a_failed_run_step_still_retries():
    flaky = REAL_RUN_STEP_FAILURE.replace(
        "0.099 401 Unauthorized: pull access denied, requested access to the resource is denied",
        "0.099 W: Failed to fetch http://deb.debian.org/ Temporary failure resolving 'deb.debian.org'",
    )
    assert classify(flaky) == TRANSIENT


def test_a_refused_base_image_pull_is_still_auth_when_no_step_ran():
    refused = (
        "ERROR: failed to build: failed to solve: private/base:1: failed to resolve source "
        "metadata for docker.io/private/base:1: pull access denied, repository does not "
        "exist or may require authorization: server message: insufficient_scope: authorization failed"
    )
    assert classify(refused) == AUTH
