"""Review fix: Docker cleanup is only ever scoped by the instance label.

Without the guard an empty label set would list, and force-remove, every container,
network and image on the host. A fake ``docker`` script records every call.
"""

import asyncio
import stat

import pytest

from maestro.execution.docker import LABEL_INSTANCE, LABEL_RUN, DockerResources

SCRIPT = """#!/bin/sh
echo "$@" >> '{log}'
case "$1" in
  ps) [ -f '{state}/container' ] || echo c1 ;;
  images) [ -f '{state}/image' ] || echo i1 ;;
  network)
    if [ "$2" = ls ]; then [ -f '{state}/network' ] || echo n1; else touch '{state}/network'; fi ;;
  volume)
    if [ "$2" = ls ]; then [ -f '{state}/volume' ] || echo v1; else touch '{state}/volume'; fi ;;
  rm) touch '{state}/container' ;;
  image) touch '{state}/image' ;;
  *) echo "unexpected docker call: $@" >&2; exit 64 ;;
esac
"""


@pytest.fixture
def fake_docker(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    log = tmp_path / "calls.log"
    script = tmp_path / "docker"
    script.write_text(SCRIPT.format(log=log, state=state))
    script.chmod(script.stat().st_mode | stat.S_IXUSR)

    def calls() -> list[list[str]]:
        return [line.split() for line in log.read_text().splitlines()] if log.exists() else []

    return DockerResources(docker=str(script), timeout=20), calls


@pytest.mark.parametrize(
    "labels",
    [{}, {LABEL_RUN: "run_1"}, {"io.maestro.other": "x"}, {LABEL_INSTANCE: ""}],
)
def test_an_unscoped_cleanup_raises_and_never_runs_docker(fake_docker, labels):
    docker, calls = fake_docker
    with pytest.raises(ValueError):
        asyncio.run(docker.cleanup(labels))
    assert calls() == []


def test_a_scoped_sweep_filters_every_list_call_and_removes_only_what_it_listed(fake_docker):
    docker, calls = fake_docker
    labels = {LABEL_INSTANCE: "ins_1", LABEL_RUN: "run_1"}
    report = asyncio.run(docker.cleanup(labels))
    assert report.released and report.remaining == []
    filters = [
        "--filter",
        "label=io.maestro.instance_id=ins_1",
        "--filter",
        "label=io.maestro.run_id=run_1",
    ]
    listed = [
        c
        for c in calls()
        if c[0] in {"ps", "images"} or c[:2] in (["network", "ls"], ["volume", "ls"])
    ]
    assert len(listed) == 8  # each kind once to find, once to confirm
    for call in listed:
        assert call[-4:] == filters
        assert filters[1] in call and filters[3] in call
    removed = [c for c in calls() if c not in listed]
    assert removed == [
        ["rm", "-f", "-v", "c1"],
        ["network", "rm", "n1"],
        ["volume", "rm", "v1"],
        ["image", "rm", "i1"],
    ]


def test_a_docker_failure_is_reported_not_raised_and_not_released(tmp_path):
    script = tmp_path / "docker"
    script.write_text("#!/bin/sh\necho daemon down >&2\nexit 1\n")
    script.chmod(0o755)
    report = asyncio.run(
        DockerResources(docker=str(script), timeout=20).cleanup({LABEL_INSTANCE: "ins_1"})
    )
    assert not report.released and report.remaining == ["error:DockerError"]
