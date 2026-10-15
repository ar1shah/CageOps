"""Reading the registry from the serving side. Registering and activating stay in the trainer."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from sqlalchemy import Connection, select

from cageops_common.db.models import ModelVersion


@dataclass(frozen=True)
class ActiveVersion:
    version: str
    trained_at: datetime
    data_cutoff: date
    features_output_sha256: str
    features_load_run_id: int | None
    artifacts_sha256: dict[str, str] = field(default_factory=dict)


def read_active_version(conn: Connection) -> ActiveVersion | None:
    """The one row with is_active (Postgres allows at most one), or None."""
    row = conn.execute(
        select(
            ModelVersion.version,
            ModelVersion.trained_at,
            ModelVersion.data_cutoff,
            ModelVersion.features_output_sha256,
            ModelVersion.features_load_run_id,
            ModelVersion.config,
        ).where(ModelVersion.is_active)
    ).one_or_none()
    if row is None:
        return None
    config: dict[str, Any] = row.config or {}
    return ActiveVersion(
        version=row.version,
        trained_at=row.trained_at,
        data_cutoff=row.data_cutoff,
        features_output_sha256=row.features_output_sha256,
        features_load_run_id=row.features_load_run_id,
        artifacts_sha256=dict(config.get("artifacts_sha256") or {}),
    )
