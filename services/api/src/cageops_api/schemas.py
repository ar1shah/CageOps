"""Request and response bodies. Pydantic at every boundary; examples show up in /docs."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# -- fighters ---------------------------------------------------------------------------------


class FighterSummary(BaseModel):
    id: int
    name: str
    weight_class: str | None = Field(
        description="Division of the fighter's most recent bout in a standard weight class."
    )
    n_ufc_fights: int = Field(description="Completed UFC fights we hold for this fighter.")
    last_fight_date: date | None


class FighterSearchResponse(BaseModel):
    items: list[FighterSummary]
    total: int = Field(description="All matches, not just this page.")
    limit: int
    offset: int

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "items": [
                        {
                            "id": 101,
                            "name": "Example Fighter",
                            "weight_class": "Lightweight",
                            "n_ufc_fights": 12,
                            "last_fight_date": "2026-03-14",
                        }
                    ],
                    "total": 1,
                    "limit": 20,
                    "offset": 0,
                }
            ]
        }
    )


class Record(BaseModel):
    wins: int
    losses: int
    draws: int
    no_contests: int
    unknown_results: int = Field(description="Completed fights whose result the source lacks.")
    wins_by_ko_tko: int
    wins_by_submission: int
    wins_by_decision: int
    wins_by_other: int


class OpponentRef(BaseModel):
    id: int
    name: str


class RecentFight(BaseModel):
    event_date: date
    event_name: str
    opponent: OpponentRef
    result: Literal["win", "loss", "draw", "no_contest", "unknown"]
    method: str | None
    finish_round: int | None
    is_title_fight: bool | None
    weight_class: str | None


class FighterProfile(BaseModel):
    id: int
    name: str
    date_of_birth: date | None
    age_years: int | None = Field(description="Whole years as of today (APP_TIMEZONE).")
    height_cm: float | None
    reach_cm: float | None
    stance: str | None
    weight_class: str | None
    record: Record
    last_fights: list[RecentFight]


# -- predict ----------------------------------------------------------------------------------


class FighterRef(BaseModel):
    id: int
    name: str


class PredictRequest(BaseModel):
    """Who is fighting, and (optionally) the bout's details. Features are as of today."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {"fighter_a_id": 101, "fighter_b_id": 202},
                {
                    "fighter_a_id": 101,
                    "fighter_b_id": 202,
                    "weight_class": "Lightweight",
                    "is_title_fight": True,
                    "scheduled_rounds": 5,
                },
            ]
        },
    )

    fighter_a_id: int = Field(ge=1)
    fighter_b_id: int = Field(ge=1)
    weight_class: str | None = Field(
        default=None,
        max_length=40,
        description="Required when the two fighters' most recent divisions differ. Must be one "
        "of the classes the active model knows.",
    )
    scheduled_rounds: Literal[3, 5] | None = Field(
        default=None, description="Default 5 for a title fight, otherwise 3."
    )
    is_title_fight: bool | None = Field(default=None, description="Default false.")

    @model_validator(mode="after")
    def _consistent(self) -> PredictRequest:
        if self.fighter_a_id == self.fighter_b_id:
            raise ValueError("fighter_a_id and fighter_b_id must be different fighters")
        if self.is_title_fight and self.scheduled_rounds == 3:
            raise ValueError("a title fight is scheduled for 5 rounds, not 3")
        return self


class WinProbability(BaseModel):
    fighter_a: float
    fighter_b: float


class MethodProbabilities(BaseModel):
    ko_tko: float
    submission: float
    decision: float


class FeatureContribution(BaseModel):
    feature: str = Field(description="Base feature name, e.g. sig_str_landed_pm_career.")
    kind: Literal["numeric", "categorical"]
    value_a: float | str | None = Field(description="Fighter A's value (or A's stance).")
    value_b: float | str | None
    contribution: float = Field(
        description="Effect on fighter A's log-odds of winning. Positive favors fighter A."
    )
    favors: Literal["fighter_a", "fighter_b"]


class StatsGap(BaseModel):
    career: int
    last3: int
    last5: int


class FighterQuality(BaseModel):
    n_prior_fights: int
    has_ufc_history: bool
    features_current: bool = Field(
        description="The feature row accounts for every completed fight this fighter has."
    )
    missing_fights: int = Field(
        description="Completed fights the feature row does not include (0 when current)."
    )
    stats_gap_fights: StatsGap = Field(
        description="Fights in each window that have no per-round stats, so the fighter's "
        "rates were computed without them."
    )
    rates_complete: bool = Field(
        description="Has history and every fight in every window has per-round stats."
    )


class DataQuality(BaseModel):
    features_current: bool = Field(description="True for both fighters.")
    rates_complete: bool = Field(description="True for both fighters.")
    fighter_a: FighterQuality
    fighter_b: FighterQuality
    features_built_as_of: date = Field(
        description="Completed fights strictly before this date were counted in the features."
    )
    features_build_id: int
    known_effect: str | None = Field(
        description="What an incomplete-rates prediction is known to cost, when it applies."
    )
    stale_note: str | None = Field(
        description="Set when a completed fight is missing from the features."
    )


class Matchup(BaseModel):
    weight_class: str
    weight_class_source: Literal["request", "both_fighters_recent_bouts"]
    scheduled_rounds: int
    is_title_fight: bool
    as_of: date = Field(description="The date the features are for (today, APP_TIMEZONE).")
    context_used_by_model: list[str] = Field(
        description="Which of weight_class, scheduled_rounds, is_title_fight the active model "
        "actually uses. The others are validated and echoed but cannot change the numbers."
    )


class PredictResponse(BaseModel):
    fighter_a: FighterRef
    fighter_b: FighterRef
    win_probability: WinProbability
    method_probabilities: MethodProbabilities = Field(
        description="How the fight ends, whoever wins: it is the same for A vs B and B vs A."
    )
    model_version: str
    top_features: list[FeatureContribution]
    matchup: Matchup
    data_quality: DataQuality
    disclaimer: str = "A statistical estimate based on past UFC fights. Not betting advice."

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "fighter_a": {"id": 101, "name": "Example Fighter"},
                    "fighter_b": {"id": 202, "name": "Another Fighter"},
                    "win_probability": {"fighter_a": 0.57, "fighter_b": 0.43},
                    "method_probabilities": {"ko_tko": 0.31, "submission": 0.18, "decision": 0.51},
                    "model_version": "20261014-225801",
                    "top_features": [
                        {
                            "feature": "sig_str_landed_pm_career",
                            "kind": "numeric",
                            "value_a": 4.9,
                            "value_b": 3.6,
                            "contribution": 0.12,
                            "favors": "fighter_a",
                        }
                    ],
                    "matchup": {
                        "weight_class": "Lightweight",
                        "weight_class_source": "both_fighters_recent_bouts",
                        "scheduled_rounds": 3,
                        "is_title_fight": False,
                        "as_of": "2026-10-15",
                        "context_used_by_model": ["weight_class"],
                    },
                    "data_quality": {
                        "features_current": True,
                        "rates_complete": True,
                        "features_built_as_of": "2026-10-15",
                        "features_build_id": 38,
                        "known_effect": None,
                        "stale_note": None,
                    },
                }
            ]
        }
    )
