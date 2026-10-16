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


# -- a small league written straight into the tables the API reads ------------------------------

from itertools import count  # noqa: E402


class League:
    """Fighters, events, completed and scheduled fights, per-fight totals, and
    `fighter_features_current` rows, inserted with SQL. The API's tests never run the feature
    builder (that is the worker's, and has its own tests): they write the rows it would write."""

    def __init__(self, engine: Engine, registry: Registry):
        self.engine = engine
        self.run_id = registry.run_id
        self._events = count(1)
        self._event_ids: dict[date, int] = {}  # one event per date: bouts on a date share a card
        self._fights = count(1)

    def fighter(self, fighter_id: int, name: str, **bio) -> int:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO fighters (id, ufcstats_id, name, dob, height_cm, reach_cm, stance)"
                    " VALUES (:id, :u, :n, :dob, :h, :r, :s)"
                ),
                {
                    "id": fighter_id,
                    "u": f"{fighter_id:016x}",
                    "n": name,
                    "dob": bio.get("dob", date(1990, 1, 1)),
                    "h": bio.get("height_cm", 180.0),
                    "r": bio.get("reach_cm", 183.0),
                    "s": bio.get("stance", "Orthodox"),
                },
            )
        return fighter_id

    def fight(
        self,
        a: int,
        b: int,
        on: date,
        *,
        winner: int | None = None,
        outcome: str | None = None,
        method: str = "decision",
        weight_class: str = "Lightweight",
        source: str = "ufcstats",
        title: bool = False,
        finish_round: int = 3,
        status: str = "completed",
        stats: bool = False,
    ) -> int:
        n = next(self._fights)
        e = next(self._events)
        low, high = sorted((a, b))
        done = status == "completed"
        if done and outcome is None:
            outcome = "win" if winner else "draw"
        with self.engine.begin() as conn:
            event_id = self._event_ids.get(on)
            if event_id is None:
                event_id = conn.execute(
                    text(
                        "INSERT INTO events (ufcstats_id, name, event_date) VALUES (:u, :n, :d)"
                        " RETURNING id"
                    ),
                    {"u": f"{e:016x}", "n": f"UFC Test {e}", "d": on},
                ).scalar_one()
                self._event_ids[on] = event_id
            fight_id = conn.execute(
                text(
                    "INSERT INTO fights (ufcstats_id, event_id, fighter_a_id, fighter_b_id,"
                    " weight_class, gender, status, is_title_fight, outcome, winner_id, method,"
                    " scheduled_rounds, finish_round, finish_time_sec, has_round_stats,"
                    " result_source) VALUES (:u, :e, :a, :b, :wc, 'M', :status, :title,"
                    " :outcome, :winner, :method, :rounds, :fr, :ft, :stats, :src) RETURNING id"
                ),
                {
                    "u": f"{n + 5000:016x}",
                    "e": event_id,
                    "a": low,
                    "b": high,
                    "wc": weight_class,
                    "status": status,
                    "title": title if done else None,
                    "outcome": outcome if done else None,
                    "winner": winner if done and outcome == "win" else None,
                    "method": method if done else None,
                    "rounds": 3 if done else None,  # a scheduled bout has no round count (D-044)
                    "fr": finish_round if done else None,
                    "ft": 300 if done else None,
                    "stats": stats and done,
                    "src": source if done else None,
                },
            ).scalar_one()
            if stats and done:
                for fighter in (a, b):
                    conn.execute(
                        text(
                            "INSERT INTO fight_totals (fight_id, fighter_id, sig_strikes_landed,"
                            " sig_strikes_att, takedowns_landed, takedowns_att, submission_att,"
                            " knockdowns) VALUES (:f, :p, 40, 80, 1, 3, 0, 0)"
                        ),
                        {"f": fight_id, "p": fighter},
                    )
        return fight_id

    def _feature_values(
        self, fighter_id: int, n_fights: int, n_with_stats: int | None, seed: int | None
    ) -> dict:
        """History-derived columns: deterministic noise for the rates, counts that follow
        `n_fights` / `n_with_stats`. No age_days or days_since_last_fight (the caller decides)."""
        rng = np.random.default_rng(fighter_id if seed is None else seed)
        n_with_stats = n_fights if n_with_stats is None else n_with_stats
        row: dict = {name: float(rng.normal(3, 1)) for name in f.NUMERIC_FEATURES}
        for window, size in (("career", None), ("last3", 3), ("last5", 5)):
            n = n_fights if size is None else min(n_fights, size)
            with_stats = min(n_with_stats, n)
            row[f"n_fights_{window}"] = n
            row[f"n_fights_with_stats_{window}"] = with_stats
            row[f"n_fights_with_duration_{window}"] = with_stats
        row["prior_ufc_fights"] = n_fights
        row["win_streak"] = int(rng.integers(0, 4)) if n_fights else 0
        row["finish_rate"] = float(rng.random()) if n_fights else None
        if n_fights == 0:
            for name in f.NUMERIC_FEATURES:
                is_rate = ("_career" in name or "_last" in name) and not name.startswith("n_fights")
                if is_rate:
                    row[name] = None
        for derived in ("age_days", "days_since_last_fight"):
            row.pop(derived)  # derived for the date asked about (D-037)
        return row

    def current(
        self,
        fighter_id: int,
        *,
        n_fights: int,
        n_with_stats: int | None = None,
        last_fight: date | None = None,
        as_of: date = date(2026, 10, 15),
        seed: int | None = None,
        **overrides,
    ) -> None:
        """A `fighter_features_current` row."""
        row = self._feature_values(fighter_id, n_fights, n_with_stats, seed) | overrides
        columns = {
            **row,
            "fighter_id": fighter_id,
            "built_as_of": as_of,
            "last_fight_date": last_fight,
            "has_unresolved_prior_fight": False,
            "load_run_id": self.run_id,
        }
        with self.engine.begin() as conn:
            fighter = conn.execute(
                text("SELECT dob, height_cm, reach_cm, stance FROM fighters WHERE id = :i"),
                {"i": fighter_id},
            ).one()
            columns.setdefault("dob", fighter.dob)
            columns.setdefault("height_cm", fighter.height_cm)
            columns.setdefault("reach_cm", fighter.reach_cm)
            columns.setdefault("stance", fighter.stance)
            names = ", ".join(columns)
            binds = ", ".join(f":{k}" for k in columns)
            conn.execute(
                text(f"INSERT INTO fighter_features_current ({names}) VALUES ({binds})"), columns
            )

    def bout_features(
        self,
        fight_id: int,
        fighter_id: int,
        *,
        event_date: date,
        n_fights: int,
        n_with_stats: int | None = None,
        last_fight: date | None = None,
        weight_class: str | None = "Lightweight",
        seed: int | None = None,
    ) -> None:
        """A `fight_features` row for a SCHEDULED bout: the fighter's history as of the bout's own
        event date, with the two calendar features computed against that date at 'build' time."""
        from cageops_common.inference.asof import date_relative_features

        with self.engine.connect() as conn:
            dob = conn.execute(
                text("SELECT dob FROM fighters WHERE id = :i"), {"i": fighter_id}
            ).scalar_one()
            bio = conn.execute(
                text("SELECT height_cm, reach_cm, stance FROM fighters WHERE id = :i"),
                {"i": fighter_id},
            ).one()
        row = self._feature_values(fighter_id, n_fights, n_with_stats, seed)
        row |= date_relative_features(event_date, dob, last_fight)
        columns = {
            **row,
            "fight_id": fight_id,
            "fighter_id": fighter_id,
            "height_cm": bio.height_cm,
            "reach_cm": bio.reach_cm,
            "stance": bio.stance,
            "weight_class": weight_class,
            "is_title_fight": None,
            "scheduled_rounds": None,
            "has_unresolved_prior_fight": False,
            "load_run_id": self.run_id,
        }
        names = ", ".join(columns)
        binds = ", ".join(f":{k}" for k in columns)
        with self.engine.begin() as conn:
            conn.execute(text(f"INSERT INTO fight_features ({names}) VALUES ({binds})"), columns)


@pytest.fixture
def league(db, registry) -> League:
    return League(db, registry)
