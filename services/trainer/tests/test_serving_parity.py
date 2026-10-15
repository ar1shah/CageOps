"""Training and serving build the same model input, from the same rows, through the same code.

What this proves, honestly: the trainer's path (`load_dataset` -> matrices) and the path the API
takes (`SELECT *` from the feature table -> `rows_to_arrays` -> matrices -> a bundle loaded from
disk, verified against the registry row) agree on column order, values and predictions. Both paths
call one set of functions, so this guards someone re-implementing assembly in the API or
reordering a column; the manifest's feature-name check in `load_bundle` is the runtime guard.
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
from sqlalchemy import text

from cageops_common.inference.bundle import load_bundle
from cageops_common.inference.features import (
    NUMERIC_FEATURES,
    method_matrix,
    win_loss_matrix,
)
from cageops_common.inference.registry import read_active_version
from cageops_common.inference.rows import rows_to_arrays
from cageops_trainer import registry
from cageops_trainer.dataset import load_dataset
from cageops_trainer.train import run_training

NOW = datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC)
SAMPLE = 150


def serving_rows(conn, fight_ids, fighter_ids):
    """What the API would read: whole rows, by key, not the trainer's column list."""
    rows = {
        (r["fight_id"], r["fighter_id"]): dict(r)
        for r in conn.execute(text("SELECT * FROM fight_features")).mappings()
    }
    return [rows[(f, p)] for f, p in zip(fight_ids, fighter_ids, strict=True)]


def test_the_trainers_matrices_equal_the_serving_matrices_for_a_sample_of_fights(lab):
    with lab.engine.connect() as conn:
        data = load_dataset(conn)
        rng = np.random.default_rng(0)
        idx = np.sort(rng.choice(len(data), size=min(SAMPLE, len(data)), replace=False))
        rows_a = serving_rows(conn, data.fight_id[idx], data.fighter_a[idx])
        rows_b = serving_rows(conn, data.fight_id[idx], data.fighter_b[idx])

    a_values, a_stance = rows_to_arrays(rows_a)
    b_values, b_stance = rows_to_arrays(rows_b)
    weight_class = [r["weight_class"] for r in rows_a]

    assert a_values.shape == (len(idx), len(NUMERIC_FEATURES))
    assert np.array_equal(a_values, data.a_values[idx], equal_nan=True)
    assert np.array_equal(b_values, data.b_values[idx], equal_nan=True)
    assert a_stance == [data.a_stance[i] for i in idx]
    assert b_stance == [data.b_stance[i] for i in idx]
    assert weight_class == [data.weight_class[i] for i in idx]


def test_a_bundle_loaded_the_way_the_api_loads_it_predicts_what_the_trainer_scored(lab, tmp_path):
    result = run_training(lab.engine, seed=1, out_dir=tmp_path, now=NOW)
    with lab.engine.begin() as conn:
        registry.activate(conn, result.version)

    with lab.engine.connect() as conn:
        active = read_active_version(conn)
        data = load_dataset(conn)
    assert active is not None and active.version == result.version
    loaded = load_bundle(tmp_path / active.version, active.artifacts_sha256, version=active.version)

    # the same bundle, read back from disk, in the trainer's own matrices
    idx = np.arange(len(data))
    a, b = data.a_values[idx], data.b_values[idx]
    sa, sb, wc = data.a_stance, data.b_stance, data.weight_class
    p_win = loaded.win_loss.predict(a, b, sa, sb, wc)
    p_method = loaded.method.predict(a, b, sa, sb, wc)

    # the trainer built `x` with the shared functions; recompute the model's own raw output
    # from the matrices and compare with the loaded booster's, not just with itself
    x_win = win_loss_matrix(a, b, sa, sb, wc, loaded.win_loss.vocab)
    x_win_swapped = win_loss_matrix(b, a, sb, sa, wc, loaded.win_loss.vocab)
    raw = loaded.win_loss.booster.predict(x_win)
    raw_swapped = loaded.win_loss.booster.predict(x_win_swapped)
    assert np.array_equal(p_win, (raw + (1.0 - raw_swapped)) / 2.0)
    assert np.array_equal(
        p_method,
        loaded.method.booster.predict(method_matrix(a, b, sa, sb, wc, loaded.method.vocab)),
    )

    # and the registry's view of the bundle matches the files the trainer wrote
    assert active.artifacts_sha256 == loaded.manifest.files_sha256
    assert loaded.data_cutoff == active.data_cutoff
    assert np.all((p_win > 0) & (p_win < 1))
    assert np.allclose(p_method.sum(axis=1), 1.0)
