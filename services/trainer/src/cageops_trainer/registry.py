"""The model registry: register a trained bundle, list versions, switch the active one (D-033).

`model_versions` is the table (migration 0009). Rows are registered INACTIVE; activation is a
separate, explicit act. "At most one active" is enforced by Postgres (a partial unique index),
so these functions cannot leave two even by mistake.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import Connection, insert, select, update

from cageops_common.db.models import ModelVersion


class UnknownVersion(Exception):
    pass


def version_name(trained_at: datetime) -> str:
    return trained_at.strftime("%Y%m%d-%H%M%S")


def register(
    conn: Connection,
    *,
    version: str,
    trained_at: datetime,
    data_cutoff: date,
    features_output_sha256: str,
    features_load_run_id: int,
    metrics: dict[str, Any],
    config: dict[str, Any],
    artifact_path: str,
) -> int:
    return conn.execute(
        insert(ModelVersion)
        .values(
            version=version,
            trained_at=trained_at,
            data_cutoff=data_cutoff,
            features_output_sha256=features_output_sha256,
            features_load_run_id=features_load_run_id,
            metrics=metrics,
            config=config,
            artifact_path=artifact_path,
            is_active=False,
        )
        .returning(ModelVersion.id)
    ).scalar_one()


def list_versions(conn: Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        select(
            ModelVersion.version,
            ModelVersion.trained_at,
            ModelVersion.data_cutoff,
            ModelVersion.features_output_sha256,
            ModelVersion.is_active,
            ModelVersion.artifact_path,
            ModelVersion.metrics,
        ).order_by(ModelVersion.trained_at.desc(), ModelVersion.id.desc())
    ).mappings()
    return [dict(r) for r in rows]


def activate(conn: Connection, version: str) -> dict[str, Any]:
    """Make `version` the only active one. Run inside one transaction (`engine.begin()`).

    The old active row is switched off first: a unique index is checked row by row, so turning
    the new one on first would be rejected mid-statement."""
    target = conn.execute(
        select(ModelVersion.id, ModelVersion.is_active).where(ModelVersion.version == version)
    ).one_or_none()
    if target is None:
        raise UnknownVersion(f"no model version {version!r}; list them with the `list` command")
    previous = list(
        conn.execute(
            select(ModelVersion.version).where(ModelVersion.is_active, ModelVersion.id != target.id)
        ).scalars()
    )
    conn.execute(
        update(ModelVersion)
        .where(ModelVersion.is_active, ModelVersion.id != target.id)
        .values(is_active=False)
    )
    conn.execute(update(ModelVersion).where(ModelVersion.id == target.id).values(is_active=True))
    return {"version": version, "was_active": bool(target.is_active), "deactivated": previous}
