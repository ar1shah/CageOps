"""Scores, written out by hand so every number can be checked against a pencil calculation.

`y` is 1 when the first-named side won; `p` is the predicted probability that it did.
"""

from __future__ import annotations

import numpy as np

EPS = 1e-15


def _clip(p: np.ndarray) -> np.ndarray:
    return np.clip(np.asarray(p, dtype=float), EPS, 1 - EPS)


def per_fight_log_loss(y: np.ndarray, p: np.ndarray) -> np.ndarray:
    p = _clip(p)
    y = np.asarray(y, dtype=float)
    return -(y * np.log(p) + (1 - y) * np.log(1 - p))


def log_loss(y: np.ndarray, p: np.ndarray) -> float:
    return float(per_fight_log_loss(y, p).mean())


def brier(y: np.ndarray, p: np.ndarray) -> float:
    return float(np.mean((np.asarray(p, dtype=float) - np.asarray(y, dtype=float)) ** 2))


def accuracy(y: np.ndarray, p: np.ndarray) -> tuple[float | None, int, int]:
    """(accuracy, fights scored, ties). A prediction of exactly 0.5 picks nobody, so it is
    counted as a tie and left out of accuracy rather than scored as a coin flip."""
    y, p = np.asarray(y), np.asarray(p, dtype=float)
    scored = p != 0.5
    n_scored = int(scored.sum())
    if n_scored == 0:
        return None, 0, int((~scored).sum())
    correct = ((p[scored] > 0.5) == (y[scored] == 1)).mean()
    return float(correct), n_scored, int((~scored).sum())


def score(y: np.ndarray, p: np.ndarray) -> dict[str, float | int | None]:
    acc, n_scored, ties = accuracy(y, p)
    return {
        "n": int(len(y)),
        "accuracy": acc,
        "accuracy_n": n_scored,
        "ties": ties,
        "log_loss": log_loss(y, p),
        "brier": brier(y, p),
    }


def calibration(y: np.ndarray, p: np.ndarray, bins: int = 10) -> dict[str, object]:
    """Equal-count bins of predicted probability: mean predicted vs observed rate.

    Equal-count rather than equal-width because predictions bunch near 0.5; a fixed grid would
    leave most bins empty. Each bin is small, so its rate is noisy (about +-7 points at 55)."""
    y, p = np.asarray(y, dtype=float), np.asarray(p, dtype=float)
    order = np.argsort(p, kind="stable")
    out, ece = [], 0.0
    for chunk in np.array_split(order, bins):
        if len(chunk) == 0:
            continue
        predicted, observed = float(p[chunk].mean()), float(y[chunk].mean())
        ece += len(chunk) / len(y) * abs(predicted - observed)
        out.append(
            {
                "n": int(len(chunk)),
                "mean_predicted": predicted,
                "observed_rate": observed,
                "p_min": float(p[chunk].min()),
                "p_max": float(p[chunk].max()),
            }
        )
    return {"bins": out, "expected_calibration_error": float(ece)}


def paired_bootstrap(
    loss_a: np.ndarray, loss_b: np.ndarray, *, resamples: int = 1000, seed: int
) -> dict[str, float | int]:
    """Mean of (loss_a - loss_b) over the same fights, with a 95% percentile interval.

    Negative favours `a`. If the interval contains 0, the two are not distinguishable on this
    many fights, whatever the point estimate says."""
    diff = np.asarray(loss_a, dtype=float) - np.asarray(loss_b, dtype=float)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(diff), size=(resamples, len(diff)))
    means = diff[idx].mean(axis=1)
    low, high = np.percentile(means, [2.5, 97.5])
    return {
        "n": int(len(diff)),
        "mean_difference": float(diff.mean()),
        "ci95_low": float(low),
        "ci95_high": float(high),
        "resamples": resamples,
    }


def multiclass_log_loss(y: np.ndarray, probs: np.ndarray) -> float:
    probs = _clip(probs)
    return float(-np.log(probs[np.arange(len(y)), y]).mean())


def multiclass_brier(y: np.ndarray, probs: np.ndarray) -> float:
    onehot = np.zeros_like(probs, dtype=float)
    onehot[np.arange(len(y)), y] = 1.0
    return float(np.mean(np.sum((probs - onehot) ** 2, axis=1)))


def multiclass_accuracy(y: np.ndarray, probs: np.ndarray) -> float:
    return float((np.argmax(probs, axis=1) == y).mean())
