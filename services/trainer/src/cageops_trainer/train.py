"""Train, evaluate and register one model version (D-031, D-032, D-033).

One pass, in this order: check the features are fresh, read everything in one snapshot, split by
time, fit the win/loss model (early-stopped on validation), fit the method model, score both
against the baselines, write the artifacts, register the row. The test split is scored once, at
the end, and nothing is tuned on it.
"""

from __future__ import annotations

import json
import logging
import platform
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import lightgbm
import numpy as np
from sqlalchemy import Engine

from cageops_common.inference.bundle import write_bundle
from cageops_common.inference.features import (
    CATEGORICAL_COLUMNS,
    METHOD_FEATURE_NAMES,
    NUMERIC_FEATURES,
    SCHEDULE_FEATURE_NAMES,
    WIN_FEATURE_NAMES,
    method_matrix,
    win_loss_matrix,
)
from cageops_common.inference.model import METHOD_CLASSES, MethodModel, WinLossModel
from cageops_trainer import baselines, metrics, registry
from cageops_trainer.dataset import Dataset, FeatureBuild, check_features, load_dataset
from cageops_trainer.features import (
    EXCLUDED_COLUMNS,
    fit_vocabulary,
    random_swap,
    swap_sides,
)
from cageops_trainer.model import fit, params_for
from cageops_trainer.splits import (
    Boundaries,
    SplitIndices,
    default_boundaries,
    describe,
    make_splits,
)

log = logging.getLogger(__name__)

# The first day of the Wikipedia-sourced period (D-025, D-029): results but no per-round stats,
# so these fights' fighters have rate features that miss their most recent bouts.
WIKIPEDIA_PERIOD_START = date(2026, 5, 17)
BOOTSTRAP_RESAMPLES = 1000


@dataclass(frozen=True)
class TrainResult:
    version: str
    artifact_path: str
    metrics: dict[str, Any]
    warnings: list[str]


def _plain(value: Any) -> Any:
    """json.dumps fallback: numpy scalars and dates become plain JSON values."""
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def _git_state() -> dict[str, object]:
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True, timeout=5
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"], capture_output=True, text=True, timeout=5
            ).stdout.strip()
        )
        return {"sha": sha, "dirty": dirty}
    except (OSError, subprocess.SubprocessError):
        return {"sha": None, "dirty": None}


def _win_inputs(data: Dataset, idx: np.ndarray, swap: np.ndarray | None):
    """(a_values, b_values, a_stance, b_stance, weight_class, label) for these fights, with the
    two fighters exchanged wherever `swap` says. label is 1 when the FIRST fighter won."""
    a_values, b_values = data.a_values[idx], data.b_values[idx]
    a_stance = [data.a_stance[i] for i in idx]
    b_stance = [data.b_stance[i] for i in idx]
    weight_class = [data.weight_class[i] for i in idx]
    first_won = data.winner_is_a[idx].copy()
    if swap is not None:
        a_values, b_values, a_stance, b_stance = swap_sides(
            swap, a_values, b_values, a_stance, b_stance
        )
        first_won = first_won ^ swap
    return a_values, b_values, a_stance, b_stance, weight_class, first_won.astype(int)


def _fit_win_loss(data: Dataset, splits: SplitIndices, seed: int) -> tuple[WinLossModel, float]:
    tr, va = splits.train, splits.val
    vocab = fit_vocabulary(
        [data.a_stance[i] for i in tr],
        [data.b_stance[i] for i in tr],
        [data.weight_class[i] for i in tr],
        ordered=True,
    )
    # One seeded coin flip per fight puts either fighter first, so the model cannot learn a
    # position prior. Validation is swapped too (its own seed) so early stopping sees no slot.
    train_swap = random_swap(len(tr), seed)
    val_swap = random_swap(len(va), seed + 1)
    a, b, sa, sb, wc, y_train = _win_inputs(data, tr, train_swap)
    x_train = win_loss_matrix(a, b, sa, sb, wc, vocab)
    a, b, sa, sb, wc, y_val = _win_inputs(data, va, val_swap)
    x_val = win_loss_matrix(a, b, sa, sb, wc, vocab)
    booster = fit(
        x_train,
        y_train,
        x_val,
        y_val,
        feature_names=WIN_FEATURE_NAMES,
        params=params_for("binary", seed),
    )
    return WinLossModel(booster, vocab), float(train_swap.mean())


def _predict_win(model: WinLossModel, data: Dataset, idx: np.ndarray) -> np.ndarray:
    return model.predict(
        data.a_values[idx],
        data.b_values[idx],
        [data.a_stance[i] for i in idx],
        [data.b_stance[i] for i in idx],
        [data.weight_class[i] for i in idx],
    )


def _method_rows(data: Dataset, idx: np.ndarray) -> np.ndarray:
    """Fights with a fighting finish or a decision. A DQ is a foul outcome nothing in the
    features can predict, and `other` only ever comes with a no contest (already excluded)."""
    return idx[np.isin(data.method[idx], METHOD_CLASSES)]


def _method_inputs(data: Dataset, idx: np.ndarray):
    return (
        data.a_values[idx],
        data.b_values[idx],
        [data.a_stance[i] for i in idx],
        [data.b_stance[i] for i in idx],
        [data.weight_class[i] for i in idx],
    )


def _sched(data: Dataset, idx: np.ndarray, schedule: bool) -> np.ndarray | None:
    return data.schedule[idx] if schedule else None


def _fit_method(
    data: Dataset, splits: SplitIndices, seed: int, *, schedule: bool
) -> tuple[MethodModel, np.ndarray, np.ndarray]:
    tr, va = _method_rows(data, splits.train), _method_rows(data, splits.val)
    vocab = fit_vocabulary(
        [data.a_stance[i] for i in tr],
        [data.b_stance[i] for i in tr],
        [data.weight_class[i] for i in tr],
        ordered=False,
    )
    names = METHOD_FEATURE_NAMES + (SCHEDULE_FEATURE_NAMES if schedule else ())
    x_train = method_matrix(*_method_inputs(data, tr), vocab, _sched(data, tr, schedule))
    x_val = method_matrix(*_method_inputs(data, va), vocab, _sched(data, va, schedule))
    y_train = np.array([METHOD_CLASSES.index(m) for m in data.method[tr]])
    y_val = np.array([METHOD_CLASSES.index(m) for m in data.method[va]])
    booster = fit(
        x_train,
        y_train,
        x_val,
        y_val,
        feature_names=names,
        params=params_for("multiclass", seed, num_class=len(METHOD_CLASSES)),
    )
    return MethodModel(booster, vocab, with_schedule=schedule), y_train, y_val


def _predict_method(model: MethodModel, data: Dataset, idx: np.ndarray) -> np.ndarray:
    return model.predict(*_method_inputs(data, idx), _sched(data, idx, model.with_schedule))


def _method_scores(y: np.ndarray, probs: np.ndarray, prior: np.ndarray) -> dict[str, object]:
    if len(y) == 0:
        return {"n": 0, "note": "no fights in this slice"}
    base = np.tile(prior, (len(y), 1))
    return {
        "n": int(len(y)),
        "model": {
            "accuracy": metrics.multiclass_accuracy(y, probs),
            "log_loss": metrics.multiclass_log_loss(y, probs),
            "brier": metrics.multiclass_brier(y, probs),
        },
        "train_class_prior": {
            "accuracy": metrics.multiclass_accuracy(y, base),
            "log_loss": metrics.multiclass_log_loss(y, base),
            "brier": metrics.multiclass_brier(y, base),
        },
    }


def _win_slice(
    data: Dataset,
    idx: np.ndarray,
    p_model: np.ndarray,
    heuristic_win_rate: float,
    *,
    seed: int,
) -> dict[str, object]:
    """Model, heuristic and coin flip on these fights; the favorite on those that have odds."""
    if len(idx) == 0:
        return {"n": 0, "note": "no fights in this slice"}
    y = data.winner_is_a[idx].astype(int)
    sign = baselines.heuristic_sign(
        data.rank_a[idx],
        data.rank_b[idx],
        data.a_values[idx, baselines.PRIOR_UFC_FIGHTS],
        data.b_values[idx, baselines.PRIOR_UFC_FIGHTS],
    )
    p_heuristic = baselines.heuristic_probability(sign, heuristic_win_rate)
    block: dict[str, object] = {
        "n": int(len(idx)),
        "model": metrics.score(y, p_model),
        "heuristic": metrics.score(y, p_heuristic),
        "coin_flip": metrics.score(y, np.full(len(y), 0.5)),
    }
    has_odds = ~np.isnan(data.odds_a[idx]) & ~np.isnan(data.odds_b[idx])
    block["odds_coverage"] = f"{int(has_odds.sum())} of {len(idx)}"
    if has_odds.any():
        p_fav = baselines.favorite_probability(
            data.odds_a[idx][has_odds], data.odds_b[idx][has_odds]
        )
        y_o = y[has_odds]
        block["on_fights_with_odds"] = {
            "n": int(has_odds.sum()),
            "model": metrics.score(y_o, p_model[has_odds]),
            "favorite": metrics.score(y_o, p_fav),
            "heuristic": metrics.score(y_o, p_heuristic[has_odds]),
            "model_minus_favorite_log_loss": metrics.paired_bootstrap(
                metrics.per_fight_log_loss(y_o, p_model[has_odds]),
                metrics.per_fight_log_loss(y_o, p_fav),
                resamples=BOOTSTRAP_RESAMPLES,
                seed=seed,
            ),
        }
    return block


def _pick_stats(data: Dataset, idx: np.ndarray) -> dict[str, object]:
    """How often the rule's own pick won on these fights, ignoring any fitted probability."""
    sign = baselines.heuristic_sign(
        data.rank_a[idx],
        data.rank_b[idx],
        data.a_values[idx, baselines.PRIOR_UFC_FIGHTS],
        data.b_values[idx, baselines.PRIOR_UFC_FIGHTS],
    )
    picked = sign != 0
    won = data.winner_is_a[idx]
    return {
        "n_picks": int(picked.sum()),
        "pick_win_rate": float(((sign[picked] == 1) == won[picked]).mean())
        if picked.any()
        else None,
    }


def _fmt(value: float | None, digits: int = 3) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def _summary(test: dict[str, Any], heuristic: dict[str, Any]) -> list[str]:
    """Plain sentences, generated from the numbers, so the report cannot oversell itself."""
    lines = []
    allf = test["all"]
    lines.append(
        f"Test: {allf['n']} fights. Model accuracy {_fmt(allf['model']['accuracy'])}, "
        f"log loss {allf['model']['log_loss']:.4f}, Brier {allf['model']['brier']:.4f}; "
        f"experience/ranking heuristic accuracy {_fmt(allf['heuristic']['accuracy'])}, "
        f"log loss {allf['heuristic']['log_loss']:.4f}; coin flip log loss "
        f"{allf['coin_flip']['log_loss']:.4f}."
    )
    train_rate = heuristic["pick_win_rate"]["train"]["pick_win_rate"]
    test_rate = heuristic["pick_win_rate"]["test"]["pick_win_rate"]
    if (
        train_rate is not None
        and test_rate is not None
        and (train_rate - 0.5) * (test_rate - 0.5) < 0
    ):
        lines.append(
            f"The experience/ranking rule picked the winner {train_rate:.1%} of the time in "
            f"training but {test_rate:.1%} on test: its edge reversed, so its probabilities (fit "
            "on train) score worse than a coin flip. That is a shift in the data, not a "
            "bug; any feature that carries the same signal (prior_ufc_fights) can be hurt too."
        )
    odds = allf.get("on_fights_with_odds")
    if odds:
        diff = odds["model_minus_favorite_log_loss"]
        lo, hi = diff["ci95_low"], diff["ci95_high"]
        verdict = (
            "The model's log loss is lower than the favorite's, and the interval excludes 0; "
            "treat that as suspicious (the odds are closing lines) and check for leakage "
            "before believing it"
            if hi < 0
            else "The model's log loss is higher than the favorite's, and the interval excludes 0: "
            "it loses to the favorite, which is the expected result"
            if lo > 0
            else "The difference from the favorite is not distinguishable from zero on this many "
            "fights"
        )
        lines.append(
            f"Against the betting favorite on the {odds['n']} test fights that have odds "
            f"({allf['odds_coverage']}): model accuracy {_fmt(odds['model']['accuracy'])} vs "
            f"favorite {_fmt(odds['favorite']['accuracy'])}; "
            f"log loss {odds['model']['log_loss']:.4f} "
            f"vs {odds['favorite']['log_loss']:.4f} (model minus favorite "
            f"{diff['mean_difference']:+.4f}, 95% interval [{lo:+.4f}, {hi:+.4f}]). {verdict}."
        )
    else:
        lines.append("No test fight has odds, so there is no favorite comparison.")
    return lines


def run_training(
    engine: Engine,
    *,
    seed: int,
    out_dir: Path,
    val_start: date | None = None,
    test_start: date | None = None,
    train_start: date | None = None,
    now: datetime | None = None,
) -> TrainResult:
    trained_at = now or datetime.now(UTC)
    version = registry.version_name(trained_at)
    bundle = out_dir / version
    if bundle.exists():
        raise FileExistsError(f"{bundle} already exists; wait a second or pick another --out-dir")

    # One snapshot for the freshness check and every read, so nothing can change between them.
    with engine.connect() as conn:
        conn = conn.execution_options(isolation_level="REPEATABLE READ")
        with conn.begin():
            build = check_features(conn)
            data = load_dataset(conn)
    for warning in build.warnings:
        log.warning("features built before newer data: %s", warning)

    last_event = max(data.event_date)
    defaults = default_boundaries(last_event, train_start)
    boundaries = Boundaries(
        val_start=val_start or defaults.val_start,
        test_start=test_start or defaults.test_start,
        train_start=train_start,
    )
    splits = make_splits(data.event_date, boundaries)
    dates = data.dates

    # ---- win/loss ----
    win_model, swapped = _fit_win_loss(data, splits, seed)
    train_y = data.winner_is_a[splits.train].astype(int)
    train_sign = baselines.heuristic_sign(
        data.rank_a[splits.train],
        data.rank_b[splits.train],
        data.a_values[splits.train, baselines.PRIOR_UFC_FIGHTS],
        data.b_values[splits.train, baselines.PRIOR_UFC_FIGHTS],
    )
    heuristic_rate = baselines.fit_heuristic(train_sign, train_y == 1)

    p_val = _predict_win(win_model, data, splits.val)
    p_test = _predict_win(win_model, data, splits.test)
    wiki = np.datetime64(WIKIPEDIA_PERIOD_START)
    test_dates = dates[splits.test]
    before, after = test_dates < wiki, test_dates >= wiki

    def sliced(mask: np.ndarray) -> dict[str, object]:
        return _win_slice(data, splits.test[mask], p_test[mask], heuristic_rate, seed=seed)

    test_block = {
        "all": _win_slice(data, splits.test, p_test, heuristic_rate, seed=seed),
        f"before_{WIKIPEDIA_PERIOD_START}": sliced(before),
        f"from_{WIKIPEDIA_PERIOD_START}": sliced(after),
        "calibration": metrics.calibration(data.winner_is_a[splits.test].astype(int), p_test),
    }
    val_block = {
        "all": _win_slice(data, splits.val, p_val, heuristic_rate, seed=seed),
        "calibration": metrics.calibration(data.winner_is_a[splits.val].astype(int), p_val),
    }

    heuristic_info = {
        "rule": "better division rank if both fighters are ranked (point-in-time view), otherwise "
        "more prior UFC fights; its probability is the training pick win rate",
        "fitted_pick_win_rate": heuristic_rate,
        "pick_win_rate": {
            "train": _pick_stats(data, splits.train),
            "val": _pick_stats(data, splits.val),
            "test": _pick_stats(data, splits.test),
        },
    }

    # ---- method ----
    method_model, y_train_m, _ = _fit_method(data, splits, seed, schedule=False)
    prior = np.bincount(y_train_m, minlength=len(METHOD_CLASSES)) / len(y_train_m)
    m_val, m_test = _method_rows(data, splits.val), _method_rows(data, splits.test)
    y_val_m = np.array([METHOD_CLASSES.index(m) for m in data.method[m_val]])
    y_test_m = np.array([METHOD_CLASSES.index(m) for m in data.method[m_test]])
    probs_val = _predict_method(method_model, data, m_val)
    probs_test = _predict_method(method_model, data, m_test)
    mt_dates = dates[m_test]
    method_block = {
        "classes": list(METHOD_CLASSES),
        "train_class_prior": [float(p) for p in prior],
        "dq_excluded": {
            "train": int((data.method[splits.train] == "dq").sum()),
            "val": int((data.method[splits.val] == "dq").sum()),
            "test": int((data.method[splits.test] == "dq").sum()),
        },
        "val": _method_scores(y_val_m, probs_val, prior),
        "test": {
            "all": _method_scores(y_test_m, probs_test, prior),
            f"before_{WIKIPEDIA_PERIOD_START}": _method_scores(
                y_test_m[mt_dates < wiki], probs_test[mt_dates < wiki], prior
            ),
            f"from_{WIKIPEDIA_PERIOD_START}": _method_scores(
                y_test_m[mt_dates >= wiki], probs_test[mt_dates >= wiki], prior
            ),
        },
    }

    # The cost of leaving is_title_fight / scheduled_rounds out, measured on validation only
    # (every validation fight is ufcstats-sourced, so both are filled). Never scored on test,
    # never registered, no artifact.
    ablation_model, _, _ = _fit_method(data, splits, seed, schedule=True)
    probs_val_with = _predict_method(ablation_model, data, m_val)
    without = metrics.multiclass_log_loss(y_val_m, probs_val)
    with_schedule = metrics.multiclass_log_loss(y_val_m, probs_val_with)
    ablation = {
        "method_with_schedule_features": {
            "split": "val",
            "n": int(len(y_val_m)),
            "columns": list(SCHEDULE_FEATURE_NAMES),
            "val_log_loss_without": without,
            "val_log_loss_with": with_schedule,
            "difference_with_minus_without": with_schedule - without,
            "val_fights_missing_a_value": int(np.isnan(data.schedule[m_val]).any(axis=1).sum()),
            "note": "early stopping uses the same validation fights for both, so each is "
            "slightly optimistic; the difference is the comparable number",
        }
    }

    report: dict[str, Any] = {
        "splits": {
            "boundaries": boundaries.as_dict(),
            "train": describe(data.event_date, splits.train),
            "val": describe(data.event_date, splits.val),
            "test": describe(data.event_date, splits.test),
        },
        "win_loss": {"val": val_block, "test": test_block, "heuristic": heuristic_info},
        "method": method_block,
        "ablation": ablation,
        "summary": _summary(test_block, heuristic_info),
        "caveats": [
            "Odds have no capture time (captured_at is NULL), so they are probably closing "
            "lines: a strong baseline, and not proven to be pre-fight.",
            f"Fights from {WIKIPEDIA_PERIOD_START} come from Wikipedia and have no per-round "
            "stats, so the rate features for those fighters miss their most recent bouts. "
            "They are scored separately above.",
            "The model was fit on the training split only; it has not seen validation or test "
            "fights. Early stopping used the validation split.",
            "Calibration bins hold about 55 fights each, so a bin's observed rate is noisy "
            "(roughly +-7 points).",
        ],
        "warnings": build.warnings,
    }

    # ---- artifacts and the registry row ----
    bundle.mkdir(parents=True)
    try:
        data_cutoff = max(data.event_date[i] for i in splits.train)
        # Both model files and the manifest, through the same writer the API's tests use. The
        # checksums it returns are what the registry row stores, so the two cannot disagree.
        artifacts = write_bundle(
            bundle,
            version=version,
            data_cutoff=data_cutoff,
            win_model=win_model,
            method_model=method_model,
        ).files_sha256
        config = _config(
            seed=seed,
            data=data,
            splits=splits,
            boundaries=boundaries,
            build=build,
            win_model=win_model,
            method_model=method_model,
            swapped=swapped,
            artifacts=artifacts,
        )
        # Strict JSON: a NaN would be rejected by Postgres JSONB far from the cause, so fail here,
        # naming the field. (default=str turns dates and numpy scalars into plain values.)
        report = json.loads(json.dumps(report, allow_nan=False, default=_plain))
        config = json.loads(json.dumps(config, allow_nan=False, default=_plain))
        with engine.begin() as conn:
            registry.register(
                conn,
                version=version,
                trained_at=trained_at,
                data_cutoff=data_cutoff,
                features_output_sha256=build.output_sha256,
                features_load_run_id=build.load_run_id,
                metrics=report,
                config=config,
                artifact_path=str(bundle),
            )
    except BaseException:
        shutil.rmtree(bundle, ignore_errors=True)
        raise
    return TrainResult(version, str(bundle), report, build.warnings)


def _config(
    *,
    seed: int,
    data: Dataset,
    splits: SplitIndices,
    boundaries: Boundaries,
    build: FeatureBuild,
    win_model: WinLossModel,
    method_model: MethodModel,
    swapped: float,
    artifacts: dict[str, str],
) -> dict[str, Any]:
    return {
        "seed": seed,
        "boundaries": boundaries.as_dict(),
        "split_counts": {
            "train": int(len(splits.train)),
            "val": int(len(splits.val)),
            "test": int(len(splits.test)),
        },
        # The model is fit on `data_cutoff` (the end of train); this is the end of the whole
        # dataset the run read, which includes validation and test.
        "dataset_last_event": max(data.event_date).isoformat(),
        "dataset_fights": len(data),
        "features": {
            "numeric": list(NUMERIC_FEATURES),
            "categorical": list(CATEGORICAL_COLUMNS),
            "win_loss": list(WIN_FEATURE_NAMES),
            "method": list(METHOD_FEATURE_NAMES),
            "excluded": EXCLUDED_COLUMNS,
        },
        "vocabularies": {
            "win_loss": win_model.vocab.as_dict(),
            "method": method_model.vocab.as_dict(),
        },
        "training_swap_fraction": swapped,
        "lightgbm": {
            "win_loss": {
                "params": params_for("binary", seed),
                "best_iteration": win_model.booster.best_iteration,
            },
            "method": {
                "params": params_for("multiclass", seed, num_class=len(METHOD_CLASSES)),
                "best_iteration": method_model.booster.best_iteration,
            },
        },
        "inputs_sha256": build.inputs_sha256,
        "features_load_run_id": build.load_run_id,
        "odds_snapshot": data.odds_fingerprint(),
        "versions": {
            "lightgbm": lightgbm.__version__,
            "numpy": np.__version__,
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "git": _git_state(),
        "artifacts_sha256": artifacts,
    }
