"""Attachment experiments: local briefs, preparation, evidence and factual reports.

Only public client resources are used. Returned dictionaries are JSON serializable;
no helper submits runs, chooses a candidate or writes platform workflow records.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from maestro_sdk import MaestroClient, models
from maestro_sdk.errors import ContentMismatch
from maestro_sdk.workflows._traces import index_trace, is_trace


def write_task_briefs(
    client: MaestroClient,
    dataset_version_id: str,
    directory: str | os.PathLike[str],
    *,
    tasks: Sequence[str],
) -> list[Path]:
    """Write instruction.md and metadata.json per task, selected by key or id.

    Directories use task ids so keys containing slashes cannot escape the destination.
    Only the catalog's public task and version data is copied.
    """
    version = client.catalog.version(dataset_version_id)
    selected = _select(client.catalog.tasks(dataset_version_id), tasks)
    paths = []
    for task in selected:
        folder = _local(Path(directory), task.id)
        _local(folder, "instruction.md").write_text(task.instruction, encoding="utf-8")
        metadata = task.model_dump(mode="json", exclude={"instruction"})
        metadata["dataset_version"] = version.model_dump(mode="json")
        _json(_local(folder, "metadata.json"), metadata)
        paths.append(folder)
    return paths


def prepare_candidate(
    client: MaestroClient,
    collection_id: str,
    parent_snapshot_id: str,
    candidate_snapshot_id: str,
    *,
    tasks: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Resolve a published candidate just as submission into the collection would.

    Preserve other attachments in load order, replacing the snapshot with the parent's
    name (or append if absent). With no task selection, use the collection's selection.
    The install hint reports bootstrap identity and recorded parent installs on the
    same complete harness configuration. Whether to reuse is the caller's decision;
    neither an unchanged bootstrap nor a successful install proves candidate validity.
    """
    collection = client.collections.get(collection_id)
    parent = client.attachments.get(parent_snapshot_id)
    candidate = client.attachments.get(candidate_snapshot_id)
    if parent.name != candidate.name:
        raise ValueError("Parent and candidate must have the same attachment name")
    template = collection.template
    snapshots = [client.attachments.get(s) for s in template.attachments or []]
    attachments = [candidate.id if s.name == parent.name else s.id for s in snapshots]
    if not any(s.name == parent.name for s in snapshots):
        attachments.append(candidate.id)
    request: dict[str, Any] = {"collection_id": collection_id, "attachments": attachments}
    catalog = client.catalog.tasks(template.dataset_version_id)
    if tasks is not None:
        request["task_selection"] = {
            "mode": "explicit",
            "task_ids": [t.id for t in _select(catalog, tasks)],
        }
    resolution = client.configs.resolve(request)
    effective = resolution.effective_config
    by_key = {t.task_key: t.id for t in catalog}
    trials = [
        {"task_key": t.task_key, "task_id": by_key[t.task_key], "repetition_index": rep}
        for t in (effective.tasks if effective else [])
        for rep in range(1, effective.repetitions + 1)
    ]
    before = next((f for f in parent.files or [] if f.path == "bootstrap.sh"), None)
    after = next((f for f in candidate.files or [] if f.path == "bootstrap.sh"), None)
    installs = []
    if effective and effective.harness:
        for member in collection.members or []:
            run = client.runs.get(member.run_id)
            config = run.snapshot.effective
            if config.harness != effective.harness or not any(
                a.version_id == parent.id for a in config.attachments
            ):
                continue
            artifacts = [
                a
                for a in client.runs.artifacts(run.id)
                if a.relative_path == "maestro/attachments.json" and a.kind == "trial"
            ]
            if not artifacts:
                installs.append(
                    {"run_id": run.id, "status": "unknown", "reason": "no install record"}
                )
            for artifact in artifacts:
                observation = {
                    "run_id": run.id,
                    "artifact_id": artifact.id,
                    "trial_id": artifact.trial_id,
                    "attempt_id": artifact.attempt_id,
                }
                if artifact.status != "available":
                    installs.append(
                        {
                            **observation,
                            "status": "unknown",
                            "reason": artifact.status_reason or artifact.status,
                        }
                    )
                    continue
                raw = client.artifacts.read(artifact.id)
                _verify(raw, artifact.digest)
                try:
                    records = json.loads(raw)
                    if not isinstance(records, list) or not all(
                        isinstance(r, dict) for r in records
                    ):
                        raise ValueError("Expected install record list")
                except (ValueError, UnicodeError):
                    installs.append(
                        {**observation, "status": "unknown", "reason": "invalid install record"}
                    )
                    continue
                matched = [
                    (i, r)
                    for i, r in enumerate(records)
                    if r.get("content_digest") == parent.content_digest
                ]
                if not matched:
                    installs.append(
                        {
                            **observation,
                            "status": "unknown",
                            "reason": "parent absent from install record",
                        }
                    )
                for index, record in matched:
                    installs.append(
                        {
                            **observation,
                            "record_offset": index,
                            "record": record,
                            "status": record.get("status", "unknown"),
                        }
                    )
    return {
        "collection_id": collection_id,
        "parent_snapshot_id": parent.id,
        "candidate_snapshot_id": candidate.id,
        "resolution": resolution.model_dump(mode="json"),
        "planned_trials": resolution.planned_trials,
        "trials": trials,
        "blockers": [b.model_dump(mode="json") for b in resolution.blockers],
        "diff": client.attachments.diff(candidate.id, parent.id).model_dump(mode="json"),
        "install_reuse_hint": {
            "bootstrap_unchanged": before == after,
            "parent_bootstrap": before.model_dump(mode="json") if before else None,
            "candidate_bootstrap": after.model_dump(mode="json") if after else None,
            "parent_install_succeeded": any(i["status"] == "ok" for i in installs),
            "observations": installs,
        },
    }


def collect_evidence(
    client: MaestroClient,
    run_ids: Sequence[str],
    directory: str | os.PathLike[str],
    *,
    tasks: Sequence[str] | None = None,
    trial_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Download evidence incrementally and write manifest.json and event-index.json.

    Task keys/ids and trial ids intersect when both are supplied. Every listed artifact
    remains in the manifest, including unavailable and out-of-scope files. Run-level
    artifacts are included even with filters. Matching local SHA-256 copies are kept;
    downloaded bytes must also match the catalog digest. Trace indexes only locate
    recorded commands and output; unsupported or malformed traces have explicit reasons.
    """
    root = Path(directory)
    artifacts, indexes = [], []
    for run_id in dict.fromkeys(run_ids):
        trials = {t.id: t for t in client.runs.trials(run_id)}
        for artifact in client.runs.artifacts(run_id):
            trial = trials.get(artifact.trial_id)
            scoped = artifact.trial_id is None or (
                (tasks is None or (trial and (trial.task_key in tasks or trial.task_id in tasks)))
                and (trial_ids is None or artifact.trial_id in trial_ids)
            )
            base = PurePosixPath("runs", run_id)
            if artifact.trial_id is None:
                base /= "run"
            else:
                base = base / "trials" / artifact.trial_id / (artifact.attempt_id or "trial")
            relative = (base / _relative(artifact.relative_path)).as_posix()
            target = _local(root, relative)
            entry = {
                **artifact.model_dump(mode="json"),
                "local_path": relative,
                "downloaded": False,
                "skipped_reason": None,
            }
            if not scoped:
                entry["skipped_reason"] = "outside selection"
            elif artifact.status != "available":
                entry["skipped_reason"] = artifact.status_reason or artifact.status
            elif artifact.digest and target.is_file() and _digest(target) == artifact.digest:
                entry["skipped_reason"] = "local digest matches"
            else:
                client.artifacts.download(artifact, target)
                if artifact.digest and _digest(target) != artifact.digest:
                    target.unlink()
                    raise ContentMismatch(
                        f"{artifact.id}: downloaded file differs from catalog digest"
                    )
                entry["downloaded"] = True
            artifacts.append(entry)
            if is_trace(artifact.relative_path):
                if entry["downloaded"] or entry["skipped_reason"] == "local digest matches":
                    index = index_trace(target, artifact.relative_path)
                    for event in index["events"]:
                        event["file"] = relative
                        output = event["output"]
                        for location in output if isinstance(output, list) else [output]:
                            if location is not None:
                                location["file"] = relative
                else:
                    index = {
                        "status": "not indexed",
                        "reason": entry["skipped_reason"],
                        "events": [],
                    }
                indexes.append({"artifact_id": artifact.id, "file": relative, **index})
    manifest = {"artifacts": artifacts}
    _json(_local(root, "manifest.json"), manifest)
    _json(_local(root, "event-index.json"), {"files": indexes})
    return {**manifest, "event_index": indexes}


def build_report(
    client: MaestroClient,
    collection_id: str,
    directory: str | os.PathLike[str],
    *,
    exclude: Sequence[str] = (),
    baseline_run_id: str | None = None,
) -> dict[str, Any]:
    """Write report.json and report.md from a fresh collection comparison and runs.

    Every failure is kept by default, overriding saved collection exclusions. Complete
    means no dropped tasks, common_tasks == all_tasks, and scored == planned for every
    compared run. This flag is an evidence count, never a judgement about a candidate.
    Usage totals are partial; absent fields and unreported costs are never filled with 0.
    """
    settings: dict[str, Any] = {"exclude": list(exclude)}
    if baseline_run_id is not None:
        settings["baseline_run_id"] = baseline_run_id
    comparison = client.collections.compare(collection_id, **settings)
    runs = []
    for row in comparison.runs:
        run = client.runs.get(row.run_id)
        summary = client.runs.summary(run.id)
        runs.append(
            {
                **row.model_dump(mode="json"),
                "attachments": [
                    a.model_dump(mode="json") for a in run.snapshot.effective.attachments
                ],
                "scoring_contract": summary.contract.model_dump(mode="json"),
                "summary": summary.model_dump(mode="json"),
                "usage": summary.usage.model_dump(mode="json"),
            }
        )
    complete = (
        comparison.dropped_tasks == 0
        and comparison.common_tasks == comparison.all_tasks
        and all(r.scored_trials == r.planned_trials for r in comparison.runs)
    )
    report = {
        "collection_id": collection_id,
        "complete": complete,
        "comparison": comparison.model_dump(mode="json"),
        "runs": runs,
    }
    root = Path(directory)
    _json(_local(root, "report.json"), report)
    _local(root, "report.md").write_text(_markdown(comparison, runs, complete), encoding="utf-8")
    return report


def _markdown(comparison: models.Comparison, runs: list[dict[str, Any]], complete: bool) -> str:
    """Tables a person can read; report.json keeps every field."""
    lines = [
        "# Attachment experiment report",
        "",
        f"- Complete: {'yes' if complete else 'no'}",
        (
            f"- Tasks: {comparison.common_tasks} common of {comparison.all_tasks}, "
            f"{comparison.dropped_tasks} dropped"
        ),
        f"- Excluded failure groups: {', '.join(comparison.settings.exclude) or 'none'}",
        "",
        "## Runs",
        "",
        (
            "| Run | Label | Attachments | Planned | Scored | Failed | Mean | Mean difference "
            "| Failures by group | Usage known / unknown trials |"
        ),
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for run in runs:
        attachments = ", ".join(
            f"{a.get('name')} {a.get('version_id')}" for a in run["attachments"]
        )
        groups = ", ".join(f"{k}: {v}" for k, v in sorted(run["failed_by_group"].items()))
        usage = run["usage"]
        lines.append(
            "| "
            + " | ".join(
                _cell(v)
                for v in (
                    run["run_id"],
                    run["label"],
                    attachments,
                    run["planned_trials"],
                    run["scored_trials"],
                    run["failed_trials"],
                    run["mean"],
                    (run["difference"] or {}).get("mean"),
                    groups,
                    f"{usage.get('known_trials')} / {usage.get('unknown_trials')}",
                )
            )
            + " |"
        )
    lines += [
        "",
        "## Tasks",
        "",
        "Mean per run; failures kept in the comparison count as 0 (or as not passed when lower is better).",
        "",
    ]
    ids = [r["run_id"] for r in runs]
    lines.append(
        "| Task | Status | " + " | ".join(_cell(r["label"] or r["run_id"]) for r in runs) + " |"
    )
    lines.append("| --- | --- |" + " --- |" * len(ids))
    for task in comparison.tasks:
        cells = []
        for run_id in ids:
            cell = task.cells.get(run_id)
            cells.append(
                "not included"
                if cell is None
                else f"{_cell(cell.mean)} ({cell.scored_trials} scored, "
                f"{cell.failed_trials} failed of {cell.planned_trials})"
            )
        lines.append(f"| {_cell(task.task_key)} | {task.status} | " + " | ".join(cells) + " |")
    lines += [
        "",
        (
            "Missing usage or cost is unknown, not zero. Scoring contracts and every other "
            "field are in report.json."
        ),
        "",
    ]
    return "\n".join(lines)


def _cell(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        value = f"{value:.4g}"
    return str(value).replace("|", "\\|").replace("\n", " ")


def _select(catalog: list[models.Task], tasks: Sequence[str]) -> list[models.Task]:
    selected = {}
    for key in tasks:
        matches = [t for t in catalog if key in (t.id, t.task_key)]
        if len(matches) != 1:
            raise ValueError(f"Unknown or ambiguous task: {key!r}")
        selected[matches[0].id] = matches[0]
    return list(selected.values())


def _relative(relative: str) -> PurePosixPath:
    path = PurePosixPath(relative)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError(f"Unsafe local path: {relative!r}")
    return path


def _local(root: Path, relative: str) -> Path:
    """The caller chooses ``root`` and may reach it through links; a link below it
    could redirect writes outside the destination, so none is followed."""
    parts = _relative(relative).parts
    path = root
    for part in parts:
        path = path / part
        if path.is_symlink():
            raise ValueError(f"Local destination cannot contain a link: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1 << 20):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _verify(raw: bytes, expected: str | None) -> None:
    actual = "sha256:" + hashlib.sha256(raw).hexdigest()
    if expected and actual != expected:
        raise ContentMismatch(f"Expected {expected}, received {actual}")
