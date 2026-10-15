"""Turning two fighter ids into a prediction, in canonical order, then into A-vs-B.

The model is run once with the SMALLER fighter id as "first" (`lo`) and the larger as "second"
(`hi`), whichever order the caller asked in. That is what makes the answer for (A, B) and (B, A)
mirror each other bit for bit: both are the same computation, read from opposite ends. It is also
what lets the Redis cache (2b-2) share one entry between the two orders.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from typing import Any

from pydantic import BaseModel

from cageops_api import queries
from cageops_api.db import Database
from cageops_api.errors import ApiError
from cageops_api.metrics import ApiMetrics
from cageops_api.model_manager import ServingModel
from cageops_api.schemas import (
    DataQuality,
    FeatureContribution,
    FighterQuality,
    FighterRef,
    Matchup,
    MethodProbabilities,
    PredictRequest,
    PredictResponse,
    StatsGap,
    WinProbability,
)
from cageops_common.inference.explain import explain_win, top_contributions
from cageops_common.inference.model import METHOD_CLASSES
from cageops_common.inference.rows import rows_to_arrays, with_date_relative

# Quoted from docs/BENCHMARKS.md ("Phase 2a: model quality ... Test slices"), the only measured
# cost of incomplete rates. No other number is claimed (D-039).
KNOWN_EFFECT = (
    "At least one fighter's rates leave out fights that have no per-round stats (their results "
    "come from Wikipedia, or are from before per-round stats existed). The only measurement of "
    "what that costs is on a held-out test slice: from 2026-05-17, where every fighter's most "
    "recent bouts lack stats, the model's log loss was 0.6878 over 223 fights, against 0.6566 "
    "over the 330 test fights before that date. With 223 fights that gap is within noise "
    "(docs/BENCHMARKS.md)."
)

CONTEXT_USED_BY_MODEL = ["weight_class"]  # D-032: is_title_fight and scheduled_rounds are out


@contextmanager
def stage(metrics: ApiMetrics, name: str) -> Iterator[None]:
    started = time.perf_counter()
    try:
        yield
    finally:
        metrics.stage_seconds.labels(name).observe(time.perf_counter() - started)


class CoreContribution(BaseModel):
    feature: str
    kind: str
    value_lo: float | str | None
    value_hi: float | str | None
    contribution_lo: float  # log-odds, the lo fighter's perspective


class PredictionCore(BaseModel):
    """Everything about one matchup, oriented lo -> hi. JSON-serializable on purpose: this is
    what 2b-2 stores in Redis."""

    lo: FighterRef
    hi: FighterRef
    p_lo: float
    method: MethodProbabilities
    model_version: str
    contributions: list[CoreContribution]
    quality_lo: FighterQuality
    quality_hi: FighterQuality
    # An ad-hoc matchup has all four. A scheduled bout has the card's class and NULL for the
    # schedule: ufcstats does not say before the fight (D-044).
    weight_class: str | None
    weight_class_source: str  # "request" | "both_fighters_recent_bouts" | "card"
    scheduled_rounds: int | None
    is_title_fight: bool | None
    as_of: date
    features_built_as_of: date
    features_build_id: int
    stale_names: list[str]


def data_quality_block(
    core: PredictionCore, q_a: FighterQuality, q_b: FighterQuality
) -> DataQuality:
    """The `data_quality` of a prediction, from the two fighters' own answers (D-039)."""
    both_current = q_a.features_current and q_b.features_current
    both_complete = q_a.rates_complete and q_b.rates_complete
    return DataQuality(
        features_current=both_current,
        rates_complete=both_complete,
        fighter_a=q_a,
        fighter_b=q_b,
        features_built_as_of=core.features_built_as_of,
        features_build_id=core.features_build_id,
        known_effect=None if both_complete else KNOWN_EFFECT,
        stale_note=None
        if both_current
        else (
            f"Completed fights are missing from the features of {', '.join(core.stale_names)}; "
            "this estimate ignores them until the features are rebuilt."
        ),
    )


def orient(core: PredictionCore, a_id: int) -> PredictResponse:
    """The A-vs-B response for the caller's order. `a_id` is the caller's fighter A.

    Pure and exact: a flipped request swaps the two sides and negates every contribution, and
    the two win probabilities are `p_lo` and `1 - p_lo` in one order or the other, so B-vs-A is
    the mirror image of A-vs-B with no rounding difference."""
    flipped = a_id != core.lo.id
    first, second = (core.hi, core.lo) if flipped else (core.lo, core.hi)
    q_a, q_b = (core.quality_hi, core.quality_lo) if flipped else (core.quality_lo, core.quality_hi)
    p_a = (1.0 - core.p_lo) if flipped else core.p_lo
    sign = -1.0 if flipped else 1.0
    contributions = []
    for c in core.contributions:
        value_a, value_b = (c.value_hi, c.value_lo) if flipped else (c.value_lo, c.value_hi)
        phi = sign * c.contribution_lo
        contributions.append(
            FeatureContribution(
                feature=c.feature,
                kind=c.kind,  # type: ignore[arg-type]
                value_a=value_a,
                value_b=value_b,
                contribution=phi,
                favors="fighter_a" if phi > 0 else "fighter_b",
            )
        )
    assert core.weight_class is not None and core.scheduled_rounds is not None
    assert core.is_title_fight is not None  # an ad-hoc matchup always has its context
    return PredictResponse(
        fighter_a=first,
        fighter_b=second,
        win_probability=WinProbability(fighter_a=p_a, fighter_b=1.0 - p_a),
        method_probabilities=core.method,
        model_version=core.model_version,
        top_features=contributions,
        matchup=Matchup(
            weight_class=core.weight_class,
            weight_class_source=core.weight_class_source,  # type: ignore[arg-type]
            scheduled_rounds=core.scheduled_rounds,
            is_title_fight=core.is_title_fight,
            as_of=core.as_of,
            context_used_by_model=CONTEXT_USED_BY_MODEL,
        ),
        data_quality=data_quality_block(core, q_a, q_b),
    )


def fighter_quality(row: dict[str, Any], live_fights: int) -> FighterQuality:
    """The two separate questions about one fighter (D-039)."""
    gap = StatsGap(
        career=row["n_fights_career"] - row["n_fights_with_stats_career"],
        last3=row["n_fights_last3"] - row["n_fights_with_stats_last3"],
        last5=row["n_fights_last5"] - row["n_fights_with_stats_last5"],
    )
    has_history = row["n_fights_career"] > 0
    return FighterQuality(
        n_prior_fights=row["n_fights_career"],
        has_ufc_history=has_history,
        features_current=row["n_fights_career"] == live_fights,
        missing_fights=max(live_fights - row["n_fights_career"], 0),
        stats_gap_fights=gap,
        # "0 of 0 fights lack stats" must not read as complete: a debut has no rates at all
        rates_complete=has_history and gap.career == gap.last3 == gap.last5 == 0,
    )


def canonical_weight_class(raw: str, known: tuple[str, ...]) -> str:
    """The model's own spelling of a requested division, or a 422.

    Case and runs of whitespace are forgiven ("  lightweight " is "Lightweight"); nothing else is:
    no punctuation rewriting, no prefix matching. This runs BEFORE the cache key is built, so the
    key only ever holds a vocabulary entry: two spellings of one division share one entry, a
    misspelling is a 422 whether or not some other spelling is cached, and nothing a caller typed
    can reach the key."""
    squashed = " ".join(raw.split()).casefold()
    for entry in known:
        if entry.casefold() == squashed:
            return entry
    raise ApiError(
        422,
        "unknown_weight_class",
        f"{raw[:40]!r} is not a weight class the model knows",
        extra={"weight_classes": list(known)},
    )


def resolve_context(
    request: PredictRequest,
    names: dict[int, str],
    divisions: dict[int, str | None],
    serving: ServingModel,
) -> tuple[str, str, int, bool]:
    """(weight_class, source, scheduled_rounds, is_title_fight), or a 422 that says what to do."""
    is_title = bool(request.is_title_fight)
    rounds = request.scheduled_rounds or (5 if is_title else 3)
    known = serving.model.weight_classes

    if request.weight_class is not None:
        return canonical_weight_class(request.weight_class, known), "request", rounds, is_title

    a, b = request.fighter_a_id, request.fighter_b_id
    div_a, div_b = divisions[a], divisions[b]
    if div_a is not None and div_a == div_b:
        return div_a, "both_fighters_recent_bouts", rounds, is_title

    def say(fighter_id: int) -> str:
        found = divisions[fighter_id]
        return (
            f"{names[fighter_id]} most recently fought at {found}"
            if found
            else f"{names[fighter_id]} has no standard-division bout on record"
        )

    raise ApiError(
        422,
        "weight_class_required",
        f"{say(a)} and {say(b)}; say which weight class this matchup is at with weight_class",
        extra={"recent_divisions": {str(a): div_a, str(b): div_b}, "weight_classes": list(known)},
    )


def compute_core(
    *,
    request: PredictRequest,
    db: Database,
    serving: ServingModel,
    as_of: date,
    top_n: int,
    metrics: ApiMetrics,
) -> PredictionCore:
    """Read the rows, run the model, and describe the matchup in canonical (lo, hi) order. This is
    the expensive part, and the only part that is cached."""
    ids = [request.fighter_a_id, request.fighter_b_id]
    lo_id, hi_id = sorted(ids)

    with stage(metrics, "db"), db.connect() as conn:
        names = queries.fighter_names(conn, ids)
        missing = [(label, i) for label, i in (("fighter_a_id", ids[0]), ("fighter_b_id", ids[1]))
                   if i not in names]  # fmt: skip
        if missing:
            raise ApiError(
                404,
                "fighter_not_found",
                "; ".join(f"no fighter with {label} {i}" for label, i in missing),
            )
        rows = queries.current_feature_rows(conn, ids)
        divisions = queries.recent_divisions(conn, ids)
        live = queries.completed_fight_counts(conn, ids, as_of)

    unbuilt = [i for i in ids if i not in rows]
    if unbuilt:
        raise ApiError(
            503,
            "features_unavailable",
            f"no features have been built for fighter(s) {', '.join(map(str, unbuilt))}; "
            "run `features rebuild`",
        )

    weight_class, source, rounds, is_title = resolve_context(request, names, divisions, serving)

    with stage(metrics, "assemble"):
        dated = {i: with_date_relative(rows[i], as_of) for i in ids}
        values, stances = rows_to_arrays([dated[lo_id], dated[hi_id]])
        a_values, b_values = values[0:1], values[1:2]
        wc = [weight_class]

    with stage(metrics, "model"):
        explained = explain_win(
            serving.model.win_loss, a_values, b_values, [stances[0]], [stances[1]], wc
        )
        method = serving.model.method.predict(a_values, b_values, [stances[0]], [stances[1]], wc)[0]
        shown = top_contributions(
            explained.contributions[0], values[0], values[1], stances[0], stances[1],
            weight_class, top_n,
        )  # fmt: skip

    with stage(metrics, "build"):
        quality = {i: fighter_quality(rows[i], live[i]) for i in ids}
        stale_names = [names[i] for i in (lo_id, hi_id) if not quality[i].features_current]
        core = PredictionCore(
            lo=FighterRef(id=lo_id, name=names[lo_id]),
            hi=FighterRef(id=hi_id, name=names[hi_id]),
            p_lo=float(explained.p_a[0]),
            method=MethodProbabilities(
                **{c: float(method[k]) for k, c in enumerate(METHOD_CLASSES)}
            ),
            model_version=serving.version,
            contributions=[
                CoreContribution(
                    feature=c.feature,
                    kind=c.kind,
                    value_lo=c.value_a,
                    value_hi=c.value_b,
                    contribution_lo=c.phi,
                )
                for c in shown
            ],  # fmt: skip
            quality_lo=quality[lo_id],
            quality_hi=quality[hi_id],
            weight_class=weight_class,
            weight_class_source=source,
            scheduled_rounds=rounds,
            is_title_fight=is_title,
            as_of=as_of,
            features_built_as_of=max(rows[i]["built_as_of"] for i in ids),
            features_build_id=max(rows[i]["load_run_id"] for i in ids),
            stale_names=stale_names,
        )
    return core


def record_prediction(metrics: ApiMetrics, core: PredictionCore, endpoint: str = "predict") -> None:
    """Count one prediction served, whether it was computed or came from the cache."""
    metrics.predictions.labels(endpoint).inc()
    if not (_no_gap(core.quality_lo) and _no_gap(core.quality_hi)):
        metrics.predictions_stats_gap.inc()
    if core.stale_names:
        metrics.predictions_stale.inc()


def _no_gap(q: FighterQuality) -> bool:
    g = q.stats_gap_fights
    return g.career == g.last3 == g.last5 == 0


__all__ = [
    "PredictionCore",
    "compute_bout_cores",
    "compute_core",
    "data_quality_block",
    "orient",
    "record_prediction",
]


def compute_bout_cores(
    *,
    bouts: list[dict[str, Any]],
    db: Database,
    serving: ServingModel,
    top_n: int,
    metrics: ApiMetrics,
) -> tuple[dict[int, PredictionCore | str], dict[int, str]]:
    """Predict many scheduled bouts at once: the rows, the names and the freshness counts each
    come from ONE query, and the model is called once per kind for the whole batch.

    Returns (fight_id -> core, or the reason there is no prediction because no features were built
    for that bout yet; fighter id -> name for every fighter in `bouts`). A bout's features are as
    of its own event date, so its date-relative features were fixed when the build ran and cannot
    go stale with the calendar (D-037)."""
    ids = sorted({f for b in bouts for f in (b["fighter_a_id"], b["fighter_b_id"])})
    with stage(metrics, "db"), db.connect() as conn:
        names = queries.fighter_names(conn, ids)
        rows = queries.bout_feature_rows(conn, [b["fight_id"] for b in bouts])
        have = [
            b
            for b in bouts
            if (b["fight_id"], b["fighter_a_id"]) in rows
            and (b["fight_id"], b["fighter_b_id"]) in rows
        ]
        counts = queries.completed_counts_before(
            conn,
            sorted(
                {(f, b["event_date"]) for b in have for f in (b["fighter_a_id"], b["fighter_b_id"])}
            ),
        )

    result: dict[int, PredictionCore | str] = {
        b["fight_id"]: "no features have been built for this bout yet (run `features rebuild`)"
        for b in bouts
        if b not in have
    }
    if not have:
        return result, names

    with stage(metrics, "assemble"):
        side_a = [rows[(b["fight_id"], b["fighter_a_id"])] for b in have]
        side_b = [rows[(b["fight_id"], b["fighter_b_id"])] for b in have]
        a_values, a_stance = rows_to_arrays(side_a)
        b_values, b_stance = rows_to_arrays(side_b)
        classes = [r["weight_class"] for r in side_a]  # as of the build, like the rest of the row

    with stage(metrics, "model"):
        explained = explain_win(
            serving.model.win_loss, a_values, b_values, a_stance, b_stance, classes
        )
        method = serving.model.method.predict(a_values, b_values, a_stance, b_stance, classes)

    with stage(metrics, "build"):
        for i, bout in enumerate(have):
            a_id, b_id, day = bout["fighter_a_id"], bout["fighter_b_id"], bout["event_date"]
            q_a = fighter_quality(side_a[i], counts[(a_id, day)])
            q_b = fighter_quality(side_b[i], counts[(b_id, day)])
            shown = top_contributions(
                explained.contributions[i], a_values[i], b_values[i], a_stance[i], b_stance[i],
                classes[i], top_n,
            )  # fmt: skip
            result[bout["fight_id"]] = PredictionCore(
                lo=FighterRef(id=a_id, name=names[a_id]),
                hi=FighterRef(id=b_id, name=names[b_id]),
                p_lo=float(explained.p_a[i]),
                method=MethodProbabilities(
                    **{c: float(method[i][k]) for k, c in enumerate(METHOD_CLASSES)}
                ),
                model_version=serving.version,
                contributions=[
                    CoreContribution(
                        feature=c.feature,
                        kind=c.kind,
                        value_lo=c.value_a,
                        value_hi=c.value_b,
                        contribution_lo=c.phi,
                    )
                    for c in shown
                ],  # fmt: skip
                quality_lo=q_a,
                quality_hi=q_b,
                weight_class=classes[i],
                weight_class_source="card",
                scheduled_rounds=bout["scheduled_rounds"],
                is_title_fight=bout["is_title_fight"],
                as_of=day,
                features_built_as_of=day,
                features_build_id=int(side_a[i]["load_run_id"]),
                stale_names=[
                    names[f] for f, q in ((a_id, q_a), (b_id, q_b)) if not q.features_current
                ],
            )
    return result, names
