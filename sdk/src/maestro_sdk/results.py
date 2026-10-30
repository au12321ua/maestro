"""What the SDK hands back beyond the generated models."""

from __future__ import annotations

from pydantic import BaseModel

from maestro_sdk import models
from maestro_sdk.errors import BlockersPresent


class Resolution(models.ConfigResolution):
    """A configuration resolution with the check the design calls for (10 section 7)."""

    def raise_for_blockers(self) -> Resolution:
        if self.blockers:
            raise BlockersPresent(self)
        return self


class Submission(BaseModel):
    """What a submission returned, with the key that makes repeating it safe."""

    accepted: models.RunAccepted
    idempotency_key: str
    replayed: bool

    @property
    def run_id(self) -> str:
        return self.accepted.run_id

    @property
    def operation_id(self) -> str:
        return self.accepted.operation_id


class BatchSubmission(BaseModel):
    accepted: models.BatchAccepted
    idempotency_key: str
    replayed: bool

    @property
    def batch_id(self) -> str:
        return self.accepted.batch_id

    @property
    def operation_id(self) -> str:
        return self.accepted.operation_id


class Download(BaseModel):
    """One artifact of ``runs.download_artifacts``: ``path`` is set when it was saved."""

    artifact: models.Artifact
    path: str | None
    skipped_reason: str | None = None
