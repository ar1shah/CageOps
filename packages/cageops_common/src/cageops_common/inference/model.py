"""The two LightGBM models as they are used to predict, and how they are read and written.

`WinLossModel.predict` averages both fighter orderings so it cannot depend on which fighter is
called A. Fitting (parameters, early stopping) is in `cageops_trainer.model`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import lightgbm as lgb
import numpy as np

from cageops_common.inference.features import (
    METHOD_FEATURE_NAMES,
    SCHEDULE_FEATURE_NAMES,
    WIN_FEATURE_NAMES,
    Vocabulary,
    method_matrix,
    win_loss_matrix,
)

METHOD_CLASSES = ("ko_tko", "submission", "decision")


@dataclass
class WinLossModel:
    booster: lgb.Booster
    vocab: Vocabulary

    feature_names = WIN_FEATURE_NAMES

    def _raw(self, a_values, b_values, a_stance, b_stance, weight_class) -> np.ndarray:
        """P(the FIRST fighter wins), for the given order."""
        x = win_loss_matrix(a_values, b_values, a_stance, b_stance, weight_class, self.vocab)
        return self.booster.predict(x, num_iteration=self.booster.best_iteration)

    def predict(
        self,
        a_values: np.ndarray,
        b_values: np.ndarray,
        a_stance: Sequence[str],
        b_stance: Sequence[str],
        weight_class: Sequence[str | None],
    ) -> np.ndarray:
        """P(A wins), averaged over both orderings so it cannot depend on which fighter is
        called A: p_a = (f(a, b) + 1 - f(b, a)) / 2. P(B wins) is exactly 1 - p_a, and asking
        with the fighters swapped returns the mirror image."""
        p_ab = self._raw(a_values, b_values, a_stance, b_stance, weight_class)
        p_ba = self._raw(b_values, a_values, b_stance, a_stance, weight_class)
        return (p_ab + (1.0 - p_ba)) / 2.0


@dataclass
class MethodModel:
    booster: lgb.Booster
    vocab: Vocabulary
    with_schedule: bool = False  # True only for the ablation model, which is never shipped

    @property
    def feature_names(self) -> tuple[str, ...]:
        return METHOD_FEATURE_NAMES + (SCHEDULE_FEATURE_NAMES if self.with_schedule else ())

    def predict(
        self,
        a_values: np.ndarray,
        b_values: np.ndarray,
        a_stance: Sequence[str],
        b_stance: Sequence[str],
        weight_class: Sequence[str | None],
        schedule: np.ndarray | None = None,
    ) -> np.ndarray:
        """(n, 3) probabilities in METHOD_CLASSES order. Symmetric in A and B by construction."""
        x = method_matrix(
            a_values,
            b_values,
            a_stance,
            b_stance,
            weight_class,
            self.vocab,
            schedule if self.with_schedule else None,
        )
        return self.booster.predict(x, num_iteration=self.booster.best_iteration)


def save_booster(booster: lgb.Booster, path: Path) -> str:
    """Write the model text file and return its sha256 (of the file's bytes)."""
    booster.save_model(str(path), num_iteration=booster.best_iteration)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_booster(path: Path) -> lgb.Booster:
    """Read a model text file. `best_iteration` is 0 on a loaded booster; that means "all
    trees", and the file was written truncated at the best iteration, so predictions match."""
    return lgb.Booster(model_file=str(path))
