"""Shared fixtures for the API tests. Nothing here imports the trainer."""

from __future__ import annotations

import io
import json
import logging
from datetime import date
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pytest
from sqlalchemy import Engine, text

from cageops_common.inference import features as f
from cageops_common.inference.bundle import Manifest, write_bundle
from cageops_common.inference.model import MethodModel, WinLossModel
from cageops_common.logs import JsonFormatter


@pytest.fixture
def access_lines():
    """The JSON access-log lines emitted while a test runs, as dicts."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("cageops_api.access")
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    class Lines:
        def all(self) -> list[dict]:
            return [json.loads(line) for line in stream.getvalue().splitlines() if line]

        def raw(self) -> str:
            return stream.getvalue()

    yield Lines()
    logger.removeHandler(handler)
    logger.setLevel(previous)


# -- tiny model bundles and registry rows -------------------------------------------------------

N_TREES = 10


def _booster(names, objective, seed, num_class=None) -> lgb.Booster:
    rng = np.random.default_rng(seed)
    x = rng.normal(size=(300, len(names)))
    y = (
        (x[:, 0] + rng.normal(0, 0.5, 300) > 0).astype(int)
        if objective == "binary"
        else (rng.integers(0, 3, 300))
    )
    params = {"objective": objective, "verbosity": -1, "num_leaves": 4, "seed": seed,
              "num_threads": 1, "deterministic": True, "force_row_wise": True}  # fmt: skip
    if num_class:
        params["num_class"] = num_class
    return lgb.train(
        params, lgb.Dataset(x, label=y, feature_name=list(names)), num_boost_round=N_TREES
    )


def _vocab(ordered: bool) -> f.Vocabulary:
    stances = ("orthodox", "southpaw", "switch", "other", "unknown")
    if ordered:
        pairs = sorted(f.stance_matchup(a, b) for a in stances for b in stances)
    else:
        pairs = sorted({f.sorted_stance_matchup(a, b) for a in stances for b in stances})
    return f.Vocabulary(ordered, (*pairs, f.OTHER_MATCHUP), ("Lightweight", "Welterweight"))


_MODELS: dict[int, tuple[WinLossModel, MethodModel]] = {}


def tiny_models(seed: int) -> tuple[WinLossModel, MethodModel]:
    """A ~10-tree win/loss and method model. Different seeds give different predictions."""
    if seed not in _MODELS:
        win = WinLossModel(_booster(f.WIN_FEATURE_NAMES, "binary", seed), _vocab(True))
        method = MethodModel(
            _booster(f.METHOD_FEATURE_NAMES, "multiclass", seed, num_class=3), _vocab(False)
        )
        _MODELS[seed] = (win, method)
    return _MODELS[seed]


class Registry:
    """Bundles on disk and rows in model_versions, the way the trainer leaves them."""

    def __init__(self, engine: Engine, model_dir: Path):
        self.engine = engine
        self.model_dir = model_dir
        with engine.begin() as conn:
            self.run_id = conn.execute(
                text(
                    "INSERT INTO load_runs (source, file_path, sha256) VALUES"
                    " ('features', 'db:completed_fights', repeat('c', 64)) RETURNING id"
                )
            ).scalar_one()

    def add(self, version: str, *, seed: int = 0, active: bool = False) -> Manifest:
        win, method = tiny_models(seed)
        bundle = self.model_dir / version
        bundle.mkdir(parents=True)
        manifest = write_bundle(
            bundle,
            version=version,
            data_cutoff=date(2024, 9, 28),
            win_model=win,
            method_model=method,
        )
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO model_versions (version, trained_at, data_cutoff,"
                    " features_output_sha256, features_load_run_id, metrics, config,"
                    " artifact_path, is_active) VALUES (:v, now(), '2024-09-28', :fs, :run,"
                    " '{}', CAST(:cfg AS jsonb), :path, false)"
                ),
                {
                    "v": version,
                    "fs": "5b23d187" + "0" * 56,
                    "run": self.run_id,
                    "cfg": json.dumps({"artifacts_sha256": manifest.files_sha256}),
                    "path": f"relative/to/somewhere/else/{version}",  # never used by the API
                },
            )
        if active:
            self.activate(version)
        return manifest

    def activate(self, version: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE model_versions SET is_active = false WHERE is_active"))
            conn.execute(
                text("UPDATE model_versions SET is_active = true WHERE version = :v"),
                {"v": version},
            )

    def deactivate_all(self) -> None:
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE model_versions SET is_active = false"))


@pytest.fixture
def model_dir(tmp_path) -> Path:
    path = tmp_path / "models"
    path.mkdir()
    return path


@pytest.fixture
def registry(db, model_dir) -> Registry:
    return Registry(db, model_dir)


@pytest.fixture(scope="session")
def tiny():
    """`tiny(seed) -> (WinLossModel, MethodModel)`: the models behind a registered version."""
    return tiny_models
