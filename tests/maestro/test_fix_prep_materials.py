"""Review fixes for the task material cache: digest framing, concurrent installs,
start-up sweeps and the author's build time limit."""

import asyncio
import errno
import os
import threading
from pathlib import Path
from unittest import mock

import pytest
from sqlalchemy import select
from support import running, setup_admin
from test_datasets import import_dataset, tasks_by_key

from maestro.boundaries.dataset_source import UNSUPPORTED, SourceError
from maestro.db import schema as t
from maestro.execution import materials as m
from maestro.execution.materials import (
    MAX_BUILD_TIMEOUT,
    TaskMaterials,
    build_timeout,
    inspect_task,
    tree_digest,
)
from maestro.testing.fakes import FakeDatasetSource

DIGEST = "sha256:" + "ab" * 32


def make_task(root: Path, dockerfile="FROM scratch\n") -> Path:
    (root / "environment").mkdir(parents=True)
    (root / "task.toml").write_text("[agent]\ntimeout_sec = 1\n")
    (root / "environment" / "Dockerfile").write_text(dockerfile)
    return root


# --- tree digest ---------------------------------------------------------------------


def test_tree_digest_frames_names_and_contents(tmp_path):
    """One file whose content imitates the next entry must not equal two files."""
    two, one = tmp_path / "two", tmp_path / "one"
    two.mkdir()
    one.mkdir()
    (two / "x").write_bytes(b"X")
    (two / "y").write_bytes(b"Y")
    (one / "x").write_bytes(b"X\0y\x000\0Y")
    assert tree_digest(two) != tree_digest(one)


def test_tree_digest_tells_every_difference_apart(tmp_path):
    def tree(name, files=None, dirs=(), mode=0o644):
        root = tmp_path / name
        root.mkdir()
        for relative, content in (files or {}).items():
            (root / relative).write_bytes(content)
            (root / relative).chmod(mode)
        for relative in dirs:
            (root / relative).mkdir()
        return tree_digest(root)

    base = tree("base", {"a": b"1"})
    assert tree("same", {"a": b"1"}) == base
    assert tree("renamed", {"b": b"1"}) != base
    assert tree("content", {"a": b"2"}) != base
    assert tree("executable", {"a": b"1"}, mode=0o755) != base
    assert tree("extra-dir", {"a": b"1"}, dirs=["d"]) != base
    assert tree("dir-not-file", dirs=["a"]) != tree("empty-file", {"a": b""})


def test_tree_digest_reads_files_in_chunks(tmp_path):
    root = tmp_path / "big"
    root.mkdir()
    big = root / "blob"
    big.write_bytes(os.urandom(3 * (1 << 20) + 17))
    before = tree_digest(root)
    with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("read whole file")):
        assert tree_digest(root) == before
    with big.open("r+b") as handle:  # a change past the first chunk still shows
        handle.seek(-1, os.SEEK_END)
        handle.write(b"\xff" if big.read_bytes()[-1:] != b"\xff" else b"\x00")
    assert tree_digest(root) != before


# --- concurrent installs -------------------------------------------------------------


def test_a_lost_install_race_is_success_when_the_winner_left_the_same_content(tmp_path):
    first, second = make_task(tmp_path / "a"), make_task(tmp_path / "b")
    store = TaskMaterials(tmp_path / "cache")
    real_rename, raced = os.rename, []

    def rename(src, dst, *args, **kwargs):
        if str(dst).endswith("ab" * 32) and not raced:
            raced.append(1)
            store.install(second, DIGEST)  # another install finishes first
        return real_rename(src, dst, *args, **kwargs)

    with mock.patch.object(m.os, "rename", rename):
        material = store.install(first, DIGEST)
    assert material == tree_digest(first)
    assert store.verified(DIGEST, material) is not None
    assert not [p for p in store.root.iterdir() if p.name.startswith(".")]  # no staging left


def test_two_threads_installing_the_same_digest_both_succeed(tmp_path):
    sources = [make_task(tmp_path / "a"), make_task(tmp_path / "b")]
    store = TaskMaterials(tmp_path / "cache")
    barrier = threading.Barrier(2, timeout=5)
    real_rename, waited = os.rename, []

    def rename(src, dst, *args, **kwargs):
        if str(dst).endswith("ab" * 32) and len(waited) < 2:
            waited.append(1)
            barrier.wait()  # both have decided that nothing is in place yet
        return real_rename(src, dst, *args, **kwargs)

    results, errors = [], []

    def install(source):
        try:
            results.append(store.install(source, DIGEST))
        except Exception as error:  # noqa: BLE001 - the test reports whatever came out
            errors.append(error)

    with mock.patch.object(m.os, "rename", rename):
        threads = [threading.Thread(target=install, args=(s,)) for s in sources]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    assert not errors, errors
    assert results == [tree_digest(sources[0])] * 2


def test_a_failed_rename_that_left_nothing_usable_still_raises(tmp_path):
    store = TaskMaterials(tmp_path / "cache")
    source = make_task(tmp_path / "a")

    def rename(*args, **kwargs):
        raise OSError(errno.ENOSPC, "no space left on device")

    with mock.patch.object(m.os, "rename", rename), pytest.raises(OSError):
        store.install(source, DIGEST)
    assert store.path_for(DIGEST) is None


def test_install_still_rejects_links_and_changed_content(tmp_path):
    store = TaskMaterials(tmp_path / "cache")
    linked = make_task(tmp_path / "linked")
    (linked / "link").symlink_to("task.toml")
    with pytest.raises(SourceError) as caught:
        store.install(linked, DIGEST)
    assert caught.value.kind == UNSUPPORTED
    plain = make_task(tmp_path / "plain")
    with pytest.raises(SourceError):
        store.install(plain, DIGEST, expected_material="sha256:" + "00" * 32)


# --- start-up sweep ------------------------------------------------------------------


def test_sweep_removes_leftovers_and_unreferenced_material_only(tmp_path):
    store = TaskMaterials(tmp_path / "cache")
    kept = "sha256:" + "11" * 32
    store.install(make_task(tmp_path / "kept"), kept)
    store.install(make_task(tmp_path / "orphan"), DIGEST)  # no task row refers to this one
    for leftover in (".fetch-abc", ".install-def", ".remove-ghi"):
        (store.root / leftover / "task").mkdir(parents=True)
        (store.root / leftover / "task" / "x").write_text("half")
    (store.root / "notes.txt").write_text("not ours")  # unknown names are left alone
    (store.root / "keep-me").mkdir()
    removed = store.sweep({kept})
    assert sorted(removed) == sorted([".fetch-abc", ".install-def", ".remove-ghi", "ab" * 32])
    assert sorted(p.name for p in store.root.iterdir()) == sorted(
        ["11" * 32, "keep-me", "notes.txt"]
    )
    assert store.verified(kept, tree_digest(tmp_path / "kept")) is not None
    assert store.sweep({kept}) == []  # nothing more to do


def test_sweep_without_a_cache_directory_is_a_no_op(tmp_path):
    assert TaskMaterials(tmp_path / "nothing-yet").sweep(set()) == []


def test_recover_sweeps_material_no_task_refers_to(service, version_id):
    """At start-up, before anything runs: a failed import's copy and half-fetched
    staging directories go, every material a task row refers to stays."""
    root = service.preparer.materials.root
    with service.db.read() as conn:
        rows = conn.execute(select(t.tasks.c.content_digest, t.tasks.c.material_digest)).all()
    materials = dict(rows)
    orphan = make_task(root.parent / "orphan-source")
    service.preparer.materials.install(orphan, DIGEST)
    (root / ".fetch-crashed" / "task").mkdir(parents=True)
    report = asyncio.run(service.preparer.recover())
    assert sorted(report["leftovers_removed"]) == sorted([".fetch-crashed", "ab" * 32])
    assert service.preparer.materials.path_for(DIGEST) is None
    for digest, material in materials.items():
        assert service.preparer.materials.verified(digest, material) is not None
    assert "leftovers_removed" not in asyncio.run(service.preparer.recover())


def test_recover_reports_every_sweep(service, version_id):
    """The environment's own sweeps (images, old originals) and the material sweep all run."""

    def sweep_legacy():
        return ["abc.tar"]

    service.preparer.environment.sweep_legacy = sweep_legacy
    (service.preparer.materials.root / ".install-crashed").mkdir()
    report = asyncio.run(service.preparer.recover())
    assert report == {
        "temporary_images_removed": [],
        "leftovers_removed": ["abc.tar", ".install-crashed"],
    }


# --- the author's build time limit -----------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (0, None),
        (-5, None),
        (float("inf"), None),
        (float("-inf"), None),
        (float("nan"), None),
        ("600", None),
        (True, None),
        (1, 1.0),
        (900, 900.0),
        (1234.5, 1234.5),
        (MAX_BUILD_TIMEOUT, MAX_BUILD_TIMEOUT),
        (1e12, MAX_BUILD_TIMEOUT),
    ],
)
def test_build_timeout_accepts_only_finite_positive_numbers_and_caps_them(value, expected):
    assert build_timeout(value) == expected


@pytest.mark.parametrize(
    ("toml", "expected"),
    [("inf", None), ("1e12", MAX_BUILD_TIMEOUT), ("-1", None), ("900", 900.0)],
)
def test_inspection_records_the_normalized_limit(tmp_path, toml, expected):
    task = make_task(tmp_path / "t")
    (task / "task.toml").write_text(f"[environment]\nbuild_timeout_sec = {toml}\n")
    definition = inspect_task(task, DIGEST, tree_digest(task))  # inf must not break the digest
    assert definition["preparation"]["spec"]["build_timeout_sec"] == expected
    assert definition["timeouts"]["build_sec"] == expected


def test_a_task_asking_for_an_endless_build_still_imports(service, source):
    files = FakeDatasetSource.task_files("demo", "slow", "slow")
    files["task.toml"] += "build_timeout_sec = inf\n"
    source.add("demo", "slow", "v1", ["slow"], files={"slow": files})
    with running(service) as client:
        admin = setup_admin(service, client)
        done = import_dataset(admin, name="slow")
        assert done["status"] == "succeeded", done
        (task,) = tasks_by_key(admin, done["result"]["dataset_version_id"]).values()
        assert task["timeouts"]["build_sec"] is None
