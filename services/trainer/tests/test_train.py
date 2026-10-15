"""Training end to end on a synthetic league: splits, targets, determinism, the leakage mirror of
rule 1, and what gets registered."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pytest
from sqlalchemy import text

from cageops_common.inference.model import METHOD_CLASSES
from cageops_trainer.dataset import load_dataset
from cageops_trainer.train import run_training

NOW = datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC)


def train(lab, tmp_path, *, seed=42, minutes=0, **kwargs):
    return run_training(
        lab.engine, seed=seed, out_dir=tmp_path, now=NOW + timedelta(minutes=minutes), **kwargs
    )


def latest_artifacts(lab) -> dict[str, str]:
    with lab.engine.connect() as conn:
        return conn.execute(
            text("SELECT config FROM model_versions ORDER BY id DESC LIMIT 1")
        ).scalar_one()["artifacts_sha256"]


@pytest.fixture
def trained(lab, tmp_path):
    return train(lab, tmp_path)


# -- splits ---------------------------------------------------------------------------------


def test_the_splits_are_the_last_two_years_of_data_and_nothing_overlaps(lab, trained):
    splits = trained.metrics["splits"]
    last = max(f.event_date for f in lab.fights if f.status == "completed")

    assert splits["boundaries"]["test_start"] == (last - timedelta(days=364)).isoformat()
    assert date.fromisoformat(splits["train"]["last"]) < date.fromisoformat(splits["val"]["first"])
    assert date.fromisoformat(splits["val"]["last"]) < date.fromisoformat(splits["test"]["first"])
    assert date.fromisoformat(splits["val"]["first"]) >= date.fromisoformat(
        splits["boundaries"]["val_start"]
    )
    assert splits["test"]["last"] == last.isoformat()


def test_only_fights_with_a_decided_winner_are_used(lab, trained, db):
    expected = sum(1 for f in lab.fights if f.status == "completed" and f.outcome == "win")
    with db.connect() as conn:
        assert len(load_dataset(conn)) == expected
    counts = trained.metrics["splits"]
    assert counts["train"]["n"] + counts["val"]["n"] + counts["test"]["n"] == expected
    # the draw, the no contest and the scheduled bout are not in it
    assert expected < sum(1 for f in lab.fights)


# -- targets --------------------------------------------------------------------------------


def test_a_dq_stays_in_win_loss_and_is_left_out_of_the_method_model(lab, trained):
    dq = [f for f in lab.fights if f.method == "dq"]
    assert len(dq) == 2  # the fixture planted two

    method = trained.metrics["method"]
    excluded = method["dq_excluded"]
    assert sum(excluded.values()) == 2
    win_total = sum(trained.metrics["splits"][s]["n"] for s in ("train", "val", "test"))
    method_total = sum(
        method[s]["n"] if s == "val" else method["test"]["all"]["n"] for s in ("val", "test")
    )
    assert method["classes"] == list(METHOD_CLASSES)
    assert win_total - excluded["train"] - excluded["val"] - excluded["test"] >= method_total


# -- the model learned the signal (a sign error in the swap would break this) ----------------


def test_the_model_beats_a_coin_flip_on_validation_and_test(trained):
    for split in (
        trained.metrics["win_loss"]["val"]["all"],
        trained.metrics["win_loss"]["test"]["all"],
    ):
        assert split["model"]["accuracy"] > 0.7
        assert split["model"]["log_loss"] < split["coin_flip"]["log_loss"]


def test_training_swaps_about_half_the_fights(lab, trained, db):
    with db.connect() as conn:
        config = conn.execute(text("SELECT config FROM model_versions")).scalar_one()
    assert 0.4 < config["training_swap_fraction"] < 0.6


# -- reproducibility ------------------------------------------------------------------------


def test_the_same_seed_gives_bit_identical_models(lab, tmp_path):
    first = train(lab, tmp_path, seed=3)
    second = train(lab, tmp_path, seed=3, minutes=1)

    with lab.engine.connect() as conn:
        rows = conn.execute(
            text("SELECT version, config FROM model_versions ORDER BY version")
        ).all()
    assert first.version != second.version
    assert rows[0].config["artifacts_sha256"] == rows[1].config["artifacts_sha256"]
    assert first.metrics["win_loss"] == second.metrics["win_loss"]
    assert first.metrics["method"] == second.metrics["method"]


def test_a_different_seed_gives_a_different_model(lab, tmp_path):
    train(lab, tmp_path, seed=3)
    train(lab, tmp_path, seed=4, minutes=1)

    with lab.engine.connect() as conn:
        shas = [
            r.config["artifacts_sha256"]["win_loss.txt"]
            for r in conn.execute(text("SELECT config FROM model_versions"))
        ]
    assert shas[0] != shas[1]


# -- rule 1, at the trainer level -----------------------------------------------------------


def test_rewriting_fights_from_the_test_year_cannot_change_the_trained_model(lab, tmp_path):
    """Rule 1 mirrored: whatever happens on or after test_start must not reach the model or the
    validation numbers. It must reach the test numbers, so the test can't pass by being blind."""
    test_start = lab.default_test_start()
    before = train(lab, tmp_path, seed=5)
    sha_before = latest_artifacts(lab)  # lab.write() below clears the registry

    for fight in lab.fights:
        if fight.event_date >= test_start and fight.status == "completed":
            if fight.outcome == "win":
                fight.winner = fight.b if fight.winner == fight.a else fight.a
            for row in fight.features.values():
                for name in list(row):
                    if isinstance(row[name], float):
                        row[name] = row[name] * -3 + 11
    lab.write()
    after = train(lab, tmp_path, seed=5, minutes=1)

    assert latest_artifacts(lab) == sha_before  # the same model, byte for byte
    assert before.metrics["win_loss"]["val"] == after.metrics["win_loss"]["val"]
    assert before.metrics["method"]["val"] == after.metrics["method"]["val"]
    assert before.metrics["win_loss"]["test"] != after.metrics["win_loss"]["test"]


# -- the baselines and the report -----------------------------------------------------------


def test_the_favorite_is_scored_only_on_fights_that_have_odds_and_coverage_is_stated(trained):
    test = trained.metrics["win_loss"]["test"]["all"]
    odds = test["on_fights_with_odds"]

    assert test["odds_coverage"] == f"{odds['n']} of {test['n']}"
    assert 0 < odds["n"] < test["n"]
    assert odds["favorite"]["n"] == odds["model"]["n"] == odds["n"]
    interval = odds["model_minus_favorite_log_loss"]
    assert interval["ci95_low"] <= interval["mean_difference"] <= interval["ci95_high"]
    assert any("favorite" in line for line in trained.metrics["summary"])


def test_the_wikipedia_period_slices_are_reported_separately(trained):
    test = trained.metrics["win_loss"]["test"]
    assert "before_2026-05-17" in test and "from_2026-05-17" in test
    # the synthetic league ends in 2023, so every test fight is before the Wikipedia period
    assert test["before_2026-05-17"]["n"] == test["all"]["n"]
    assert test["from_2026-05-17"] == {"n": 0, "note": "no fights in this slice"}


def test_calibration_bins_cover_every_fight(trained):
    for split in ("val", "test"):
        calibration = trained.metrics["win_loss"][split]["calibration"]
        n = trained.metrics["win_loss"][split]["all"]["n"]
        assert sum(b["n"] for b in calibration["bins"]) == n
        assert 0 <= calibration["expected_calibration_error"] <= 1


def test_the_schedule_columns_ablation_is_measured_on_validation_only(trained):
    ablation = trained.metrics["ablation"]["method_with_schedule_features"]
    assert ablation["split"] == "val"
    assert ablation["columns"] == ["is_title_fight", "scheduled_rounds"]
    assert ablation["difference_with_minus_without"] == pytest.approx(
        ablation["val_log_loss_with"] - ablation["val_log_loss_without"]
    )
    assert ablation["n"] > 0


# -- what is registered ---------------------------------------------------------------------


def test_the_version_is_registered_inactive_with_what_reproduces_it(lab, trained, db):
    with db.connect() as conn:
        row = conn.execute(text("SELECT * FROM model_versions")).mappings().one()

    assert row["version"] == trained.version
    assert row["is_active"] is False
    config = row["config"]
    assert config["seed"] == 42
    assert config["boundaries"]["test_start"] == lab.default_test_start().isoformat()
    assert config["features"]["win_loss"][0] == "diff_sig_str_landed_pm_career"
    assert config["lightgbm"]["win_loss"]["params"]["deterministic"] is True
    assert config["lightgbm"]["win_loss"]["params"]["num_threads"] == 1
    assert config["versions"]["lightgbm"]
    assert len(config["inputs_sha256"]) == 64
    assert config["odds_snapshot"]["rows"] > 0
    assert set(config["artifacts_sha256"]) == {"win_loss.txt", "method.txt"}
    assert row["features_output_sha256"] == "c" * 64
    assert row["features_load_run_id"] == lab.run_id


def test_data_cutoff_is_the_end_of_training_and_the_dataset_end_is_kept_separately(
    lab, trained, db
):
    with db.connect() as conn:
        row = conn.execute(text("SELECT data_cutoff, config FROM model_versions")).mappings().one()
    last_event = max(f.event_date for f in lab.fights if f.status == "completed")

    assert row["data_cutoff"] == date.fromisoformat(trained.metrics["splits"]["train"]["last"])
    assert row["data_cutoff"] < date.fromisoformat(row["config"]["boundaries"]["val_start"])
    assert row["config"]["dataset_last_event"] == last_event.isoformat()
    assert row["data_cutoff"] < last_event


def test_the_artifact_directory_holds_both_models_and_a_manifest(trained):
    from pathlib import Path

    bundle = Path(trained.artifact_path)
    assert {p.name for p in bundle.iterdir()} == {"win_loss.txt", "method.txt", "manifest.json"}
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert manifest["method_classes"] == list(METHOD_CLASSES)
    assert manifest["win_loss"]["vocabulary"]["ordered"] is True
    assert manifest["method"]["vocabulary"]["ordered"] is False


def test_odds_and_rankings_are_never_model_inputs(trained):
    names = (
        trained.metrics["splits"]  # touch it so the fixture is used
        and json.dumps(trained.metrics)
    )
    assert names  # the report exists
    from cageops_common.inference.features import METHOD_FEATURE_NAMES, WIN_FEATURE_NAMES

    for feature in (*WIN_FEATURE_NAMES, *METHOD_FEATURE_NAMES):
        assert "odds" not in feature and "rank" not in feature


def test_boundaries_can_be_overridden_and_are_recorded(lab, tmp_path):
    result = train(lab, tmp_path, test_start=date(2023, 6, 1), val_start=date(2022, 6, 1))
    assert result.metrics["splits"]["boundaries"]["test_start"] == "2023-06-01"
    assert result.metrics["splits"]["val"]["first"] >= "2022-06-01"
    assert np.isfinite(result.metrics["win_loss"]["test"]["all"]["model"]["log_loss"])


def test_the_report_gives_the_heuristics_own_pick_win_rate_per_split(trained):
    heuristic = trained.metrics["win_loss"]["heuristic"]

    assert set(heuristic["pick_win_rate"]) == {"train", "val", "test"}
    assert heuristic["pick_win_rate"]["train"]["n_picks"] > 0
    assert 0 <= heuristic["fitted_pick_win_rate"] <= 1
    assert "rule" in heuristic


def test_a_reversed_heuristic_edge_is_said_in_plain_words():
    from cageops_trainer.train import _summary

    block = {
        "n": 100,
        "model": {"accuracy": 0.6, "log_loss": 0.66, "brier": 0.23},
        "heuristic": {"accuracy": 0.42, "log_loss": 0.71},
        "coin_flip": {"log_loss": 0.6931},
    }
    flipped = {
        "pick_win_rate": {
            "train": {"pick_win_rate": 0.54, "n_picks": 900},
            "test": {"pick_win_rate": 0.42, "n_picks": 90},
        }
    }
    held = {
        "pick_win_rate": {
            "train": {"pick_win_rate": 0.54, "n_picks": 900},
            "test": {"pick_win_rate": 0.55, "n_picks": 90},
        }
    }

    assert any("edge reversed" in line for line in _summary({"all": block}, flipped))
    assert not any("edge reversed" in line for line in _summary({"all": block}, held))
