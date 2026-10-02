"""Schema for the CageOps seed data.

Design rules (see docs/DECISIONS.md D-006..D-010):
- Natural keys come from ufcstats ids, never from names.
- A fight stores its two fighters in neutral order (fighter_a_id < fighter_b_id), so row
  order says nothing about who won. Corner (red) is stored separately as a raw fact.
- Odds carry a `source`, and `captured_at` is NULL when the source doesn't say when.
- No scrape-time career stats live here. Anything a model sees must be computable from
  fights strictly before the fight in question.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Fighter(Base):
    """Bio facts only. Career stats from the seed files are deliberately not stored."""

    __tablename__ = "fighters"

    id: Mapped[int] = mapped_column(primary_key=True)
    ufcstats_id: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(Text)
    dob: Mapped[date | None] = mapped_column(Date)
    height_cm: Mapped[float | None]
    reach_cm: Mapped[float | None]
    stance: Mapped[str | None] = mapped_column(Text)


class FighterAlias(Base):
    """An alternate spelling that resolves to a fighter, for ONE source only.

    The key is (source, alias_norm): an alias seen in mdabbert rows must not resolve a
    same-looking name in some other source, since common names ("Tim Johnson") and single
    tokens ("Derrick") are different people elsewhere.
    """

    __tablename__ = "fighter_aliases"

    source: Mapped[str] = mapped_column(Text, primary_key=True)
    alias_norm: Mapped[str] = mapped_column(Text, primary_key=True)
    fighter_id: Mapped[int] = mapped_column(ForeignKey("fighters.id"))


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    ufcstats_id: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(Text)
    event_date: Mapped[date] = mapped_column(Date, index=True)
    city: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str | None] = mapped_column(Text)
    country: Mapped[str | None] = mapped_column(Text)


class Fight(Base):
    __tablename__ = "fights"
    __table_args__ = (
        CheckConstraint("fighter_a_id < fighter_b_id", name="neutral_fighter_order"),
        CheckConstraint(
            "winner_id IS NULL OR winner_id IN (fighter_a_id, fighter_b_id)",
            name="winner_is_a_participant",
        ),
        CheckConstraint(
            "red_fighter_id IS NULL OR red_fighter_id IN (fighter_a_id, fighter_b_id)",
            name="red_is_a_participant",
        ),
        CheckConstraint("(outcome = 'win') = (winner_id IS NOT NULL)", name="win_has_winner"),
        CheckConstraint(
            "outcome IN ('win', 'draw', 'no_contest', 'unknown')", name="outcome_values"
        ),
        CheckConstraint(
            "method IN ('decision', 'ko_tko', 'submission', 'dq', 'other')", name="method_values"
        ),
        CheckConstraint(
            "decision_type IS NULL OR decision_type IN ('unanimous', 'split', 'majority')",
            name="decision_type_values",
        ),
        CheckConstraint("gender IN ('M', 'F')", name="gender_values"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ufcstats_id: Mapped[str] = mapped_column(String(32), unique=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    # Neutral order: the smaller id is always fighter_a. Says nothing about the outcome.
    fighter_a_id: Mapped[int] = mapped_column(ForeignKey("fighters.id"), index=True)
    fighter_b_id: Mapped[int] = mapped_column(ForeignKey("fighters.id"), index=True)
    # Corner is assigned before the fight, so it is a legitimate raw fact (Phase 1c decides
    # whether it becomes a feature). NULL when no source tells us.
    red_fighter_id: Mapped[int | None] = mapped_column(ForeignKey("fighters.id"))
    # NULL when the source says unknown (a few early fights and one 2025 row).
    weight_class: Mapped[str | None] = mapped_column(Text)
    gender: Mapped[str] = mapped_column(String(1))
    is_title_fight: Mapped[bool] = mapped_column(Boolean)
    scheduled_rounds: Mapped[int | None] = mapped_column(SmallInteger)
    outcome: Mapped[str] = mapped_column(Text)
    winner_id: Mapped[int | None] = mapped_column(ForeignKey("fighters.id"))
    method: Mapped[str] = mapped_column(Text)
    decision_type: Mapped[str | None] = mapped_column(Text)
    method_detail: Mapped[str | None] = mapped_column(Text)
    finish_round: Mapped[int | None] = mapped_column(SmallInteger)
    finish_time_sec: Mapped[int | None] = mapped_column(Integer)
    referee: Mapped[str | None] = mapped_column(Text)
    # False for fights with no per-round data (mostly pre-2000). Never filled with zeros.
    has_round_stats: Mapped[bool] = mapped_column(Boolean)


class _CoreStats:
    """Stats reported both per round and for the whole fight."""

    knockdowns: Mapped[int | None] = mapped_column(SmallInteger)
    sig_strikes_landed: Mapped[int | None] = mapped_column(SmallInteger)
    sig_strikes_att: Mapped[int | None] = mapped_column(SmallInteger)
    total_strikes_landed: Mapped[int | None] = mapped_column(SmallInteger)
    total_strikes_att: Mapped[int | None] = mapped_column(SmallInteger)
    takedowns_landed: Mapped[int | None] = mapped_column(SmallInteger)
    takedowns_att: Mapped[int | None] = mapped_column(SmallInteger)
    submission_att: Mapped[int | None] = mapped_column(SmallInteger)
    reversals: Mapped[int | None] = mapped_column(SmallInteger)
    ctrl_sec: Mapped[int | None] = mapped_column(SmallInteger)


class FightTotals(_CoreStats, Base):
    """Whole-fight stats as reported by the source."""

    __tablename__ = "fight_totals"

    fight_id: Mapped[int] = mapped_column(ForeignKey("fights.id"), primary_key=True)
    fighter_id: Mapped[int] = mapped_column(ForeignKey("fighters.id"), primary_key=True)


class FightRoundStats(_CoreStats, Base):
    """Per-round stats. One row per (fight, fighter, round) that has data."""

    __tablename__ = "fight_round_stats"
    __table_args__ = (CheckConstraint("round BETWEEN 1 AND 5", name="round_range"),)

    fight_id: Mapped[int] = mapped_column(ForeignKey("fights.id"), primary_key=True)
    fighter_id: Mapped[int] = mapped_column(ForeignKey("fighters.id"), primary_key=True)
    round: Mapped[int] = mapped_column(SmallInteger, primary_key=True)

    head_landed: Mapped[int | None] = mapped_column(SmallInteger)
    head_att: Mapped[int | None] = mapped_column(SmallInteger)
    body_landed: Mapped[int | None] = mapped_column(SmallInteger)
    body_att: Mapped[int | None] = mapped_column(SmallInteger)
    leg_landed: Mapped[int | None] = mapped_column(SmallInteger)
    leg_att: Mapped[int | None] = mapped_column(SmallInteger)
    distance_landed: Mapped[int | None] = mapped_column(SmallInteger)
    distance_att: Mapped[int | None] = mapped_column(SmallInteger)
    clinch_landed: Mapped[int | None] = mapped_column(SmallInteger)
    clinch_att: Mapped[int | None] = mapped_column(SmallInteger)
    ground_landed: Mapped[int | None] = mapped_column(SmallInteger)
    ground_att: Mapped[int | None] = mapped_column(SmallInteger)


class Odds(Base):
    """Moneyline odds from a named source. Never used as model features (baseline only).

    captured_at is NULL when the source doesn't say when the odds were taken, which means
    we can't prove they were known before the fight.
    """

    __tablename__ = "odds"
    __table_args__ = (
        CheckConstraint("source IN ('mdabbert', 'silver')", name="odds_source_values"),
        CheckConstraint(
            "decimal_odds_a > 1 AND decimal_odds_b > 1", name="decimal_odds_greater_than_one"
        ),
    )

    fight_id: Mapped[int] = mapped_column(ForeignKey("fights.id"), primary_key=True)
    source: Mapped[str] = mapped_column(Text, primary_key=True)
    source_detail: Mapped[str | None] = mapped_column(Text)
    decimal_odds_a: Mapped[Decimal | None] = mapped_column(Numeric(9, 3))
    decimal_odds_b: Mapped[Decimal | None] = mapped_column(Numeric(9, 3))
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Ranking(Base):
    """One row of the weekly official rankings from one source. rank 0 is the champion.

    Only snapshots that are internally consistent are loaded (a fighter appearing twice in
    one list means two lists were merged, so that whole snapshot date is skipped).
    """

    __tablename__ = "rankings"
    __table_args__ = (
        CheckConstraint("source IN ('jerzyszocik', 'martj42')", name="rankings_source_values"),
        CheckConstraint(
            "ranking_type IN ('division', 'pound_for_pound')", name="ranking_type_values"
        ),
        CheckConstraint("rank >= 0", name="rank_non_negative"),
        Index("ix_rankings_fighter_snapshot", "fighter_id", "snapshot_date"),
    )

    source: Mapped[str] = mapped_column(Text, primary_key=True)
    snapshot_date: Mapped[date] = mapped_column(Date, primary_key=True)
    ranking_type: Mapped[str] = mapped_column(Text, primary_key=True)
    weight_class: Mapped[str] = mapped_column(Text, primary_key=True)
    name_raw: Mapped[str] = mapped_column(Text, primary_key=True)
    # NULL when the name couldn't be matched to exactly one fighter (kept, not dropped).
    fighter_id: Mapped[int | None] = mapped_column(ForeignKey("fighters.id"))
    rank: Mapped[int] = mapped_column(SmallInteger)


class LoadRun(Base):
    """One loader run over one source file, tied to the file's sha256 from the manifest."""

    __tablename__ = "load_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source: Mapped[str] = mapped_column(Text)
    file_path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rows_read: Mapped[int | None]
    rows_loaded: Mapped[int | None]
    rows_rejected: Mapped[int | None]
    report: Mapped[dict | None] = mapped_column(JSONB)
