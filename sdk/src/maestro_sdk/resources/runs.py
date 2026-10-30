"""Submitting, rerunning, listing and reading runs and their artifacts."""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from maestro_sdk import models
from maestro_sdk._transport import _body, _key, _query, _Resource, _save
from maestro_sdk.errors import MaestroError
from maestro_sdk.results import Download, Submission


class Runs(_Resource):
    def submit(
        self,
        config: models.ConfigRequestInput | Mapping[str, Any] | None = None,
        *,
        name: str,
        resolution_id: str | None = None,
        client_request_id: str | None = None,
        source_run_id: str | None = None,
        collection_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> Submission:
        """Submit a resolved configuration (``resolution_id``) or one given inline.
        With ``collection_id`` the collection fills in its fixed fields and the run joins
        it; a run that does not match is refused.

        One logical submission is one ``idempotency_key`` (generated when omitted, and kept
        on the result and on TransportError): repeating it returns the same run. Transient
        failures are retried here under that key; the run is never submitted twice.
        """
        body: dict[str, Any] = dict(_body(config) or {})
        body.update(
            _query(
                {
                    "name": name,
                    "resolution_id": resolution_id,
                    "client_request_id": client_request_id,
                    "source_run_id": source_run_id,
                    "collection_id": collection_id,
                }
            )
        )
        key = _key(idempotency_key)
        response = self._c._call("POST", "runs", json=body, idempotency_key=key)
        return Submission(
            accepted=self._c._parse(models.RunAccepted, response.json()["data"]),
            idempotency_key=key,
            replayed=response.headers.get("Idempotent-Replayed") == "true",
        )

    def rerun(
        self,
        run_id: str,
        *,
        mode: str = "all",
        task_ids: list[str] | None = None,
        name: str | None = None,
        client_request_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> Submission:
        """A new run from the source's snapshot: ``all``, ``unfinished`` or ``explicit``."""
        body = {
            "task_selection": _query({"mode": mode, "task_ids": task_ids}),
            **_query({"name": name, "client_request_id": client_request_id}),
        }
        key = _key(idempotency_key)
        response = self._c._call("POST", f"runs/{run_id}/rerun", json=body, idempotency_key=key)
        return Submission(
            accepted=self._c._parse(models.RunAccepted, response.json()["data"]),
            idempotency_key=key,
            replayed=response.headers.get("Idempotent-Replayed") == "true",
        )

    def request(self, run_id: str) -> models.ConfigRequestOutput:
        """The request that reproduces a run; edit it and submit with ``source_run_id``."""
        return self._c._get(f"runs/{run_id}/request", models.ConfigRequestOutput)

    def get(self, run_id: str) -> models.RunDetail:
        return self._c._get(f"runs/{run_id}", models.RunDetail)

    def list(self, *, limit: int | None = None, **filters: Any) -> Iterator[models.Run]:
        """Newest first, all pages. Filters: owner_id, state, dataset_version_id,
        model_config_id, harness_version_id, attachment_version_id, plan_id,
        client_request_id, accepted_after, accepted_before (RFC 3339 UTC)."""
        return self._c._iterate("runs", models.Run, limit=limit, **filters)

    def find(self, client_request_id: str) -> list[models.Run]:
        """Runs carrying this ``client_request_id``: how to recover after a lost key."""
        return list(self.list(client_request_id=client_request_id))

    def trials(self, run_id: str) -> list[models.Trial]:
        return self._c._get(f"runs/{run_id}/trials", list[models.Trial])

    def summary(self, run_id: str) -> models.RunSummary:
        return self._c._get(f"runs/{run_id}/summary", models.RunSummary)

    def cancel(self, run_id: str) -> models.Run:
        return self._c._send("POST", f"runs/{run_id}/cancel", models.Run)

    def delete(self, run_id: str) -> models.RecycleEntry:
        """Into the recycle bin; queued or running runs must be cancelled first."""
        return self._c._send("DELETE", f"runs/{run_id}", models.RecycleEntry)

    def artifacts(self, run_id: str, *, trial_id: str | None = None) -> list[models.Artifact]:
        return self._c._get(f"runs/{run_id}/artifacts", list[models.Artifact], trial_id=trial_id)

    def download_archive(
        self,
        run_id: str,
        destination: str | os.PathLike[str],
        *,
        task_key: str | None = None,
        trial_id: str | None = None,
        attempt_id: str | None = None,
    ) -> Path:
        """Save the run's artifacts as one zip (``destination`` is the file path), or only
        one task's, trial's or attempt's. Ended runs only; its manifest.json lists the
        files left out and why."""
        params = _query({"task_key": task_key, "trial_id": trial_id, "attempt_id": attempt_id})
        return _save(self._c, f"runs/{run_id}/artifacts/archive", Path(destination), params)

    def download_artifacts(
        self, run_id: str, destination: str | os.PathLike[str], *, trial_id: str | None = None
    ) -> list[Download]:
        """Save every available artifact under ``destination``; report the others.

        Run files go to ``run/<path>``, trial files to ``trials/<trial>/<attempt>/<path>``.
        Expired, failed or deleted files are listed with their status and reason.
        """
        root = Path(destination)
        results = []
        for artifact in self.artifacts(run_id, trial_id=trial_id):
            if artifact.status != "available":
                reason = artifact.status_reason or artifact.status
                results.append(Download(artifact=artifact, path=None, skipped_reason=reason))
                continue
            if artifact.trial_id is None:
                target = root / "run"
            else:
                target = root / "trials" / artifact.trial_id / (artifact.attempt_id or "trial")
            target = _inside(target, artifact.relative_path)
            _save(self._c, f"artifacts/{artifact.id}/download", target)
            results.append(Download(artifact=artifact, path=str(target)))
        return results


def _inside(base: Path, relative: str) -> Path:
    parts = PurePosixPath(relative).parts
    if not parts or PurePosixPath(relative).is_absolute() or ".." in parts:
        raise MaestroError(f"Refusing artifact path {relative!r}")
    return base.joinpath(*parts)
