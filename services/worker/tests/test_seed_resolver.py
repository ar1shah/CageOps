import csv
import re
from datetime import date

import pytest
from sqlalchemy import text

from cageops_worker.seed.normalize import normalize_name
from cageops_worker.seed.resolver import ALIASES_CSV, NameResolver, load_aliases

BRUNO_MIDDLE, BRUNO_FLY = 188, 428


def two_brunos(overlap=True):
    """Two different fighters named Bruno Silva, like the real data."""
    fly_years = [(date(2020, 3, 14), "Flyweight"), (date(2026, 3, 14), "Flyweight")]
    mid_years = [(date(2021, 6, 19), "Middleweight"), (date(2025, 5, 10), "Middleweight")]
    if not overlap:
        fly_years = [(date(2008, 1, 1), "Flyweight"), (date(2012, 1, 1), "Flyweight")]
    return NameResolver(
        {BRUNO_MIDDLE: "Bruno Silva", BRUNO_FLY: "Bruno Silva"},
        activity={BRUNO_MIDDLE: mid_years, BRUNO_FLY: fly_years},
    )


def test_unique_name_matches_ignoring_accents_and_hyphens():
    resolver = NameResolver({1: "Jan Błachowicz", 2: "Kai Kara-France"})

    assert resolver.resolve("Jan Blachowicz", source="x").fighter_id == 1
    assert resolver.resolve("kai kara france", source="x").fighter_id == 2


def test_alias_resolves_a_name_with_no_primary_match():
    resolver = NameResolver({7: "King Green"}, aliases={("mdabbert", "bobby green"): 7})

    result = resolver.resolve("Bobby Green", source="mdabbert")

    assert (result.fighter_id, result.status) == (7, "alias")


def test_unknown_name_is_unmatched():
    result = NameResolver({1: "A B"}).resolve("Nobody Here", source="x")

    assert (result.fighter_id, result.status) == (None, "unmatched")


def test_shared_name_is_disambiguated_by_weight_class():
    resolver = two_brunos(overlap=True)

    middle = resolver.resolve(
        "Bruno Silva", source="x", weight_class="Middleweight", on=date(2023, 4, 22)
    )
    fly = resolver.resolve(
        "Bruno Silva", source="x", weight_class="Flyweight", on=date(2023, 3, 11)
    )

    assert (middle.fighter_id, middle.status) == (BRUNO_MIDDLE, "disambiguated")
    assert (fly.fighter_id, fly.status) == (BRUNO_FLY, "disambiguated")


def test_shared_name_is_disambiguated_by_active_dates_alone():
    resolver = two_brunos(overlap=False)

    result = resolver.resolve("Bruno Silva", source="x", on=date(2022, 1, 1))

    assert result.fighter_id == BRUNO_MIDDLE  # the other Bruno retired in 2012


def test_shared_name_is_never_guessed_without_enough_context():
    resolver = two_brunos(overlap=True)

    no_context = resolver.resolve("Bruno Silva", source="x")
    only_date = resolver.resolve("Bruno Silva", source="x", on=date(2023, 1, 1))  # both active then
    wrong_class = resolver.resolve(
        "Bruno Silva", source="x", weight_class="Bantamweight", on=date(2019, 10, 5)
    )

    for result in (no_context, only_date, wrong_class):
        assert (result.fighter_id, result.status) == (None, "ambiguous")


def test_alias_only_applies_to_the_source_it_was_reviewed_for():
    # "derrick" is a real alias in mdabbert rows, but a bare first name elsewhere is not him
    resolver = NameResolver({9: "Derrick Krantz"}, aliases={("mdabbert", "derrick"): 9})

    in_mdabbert = resolver.resolve("Derrick", source="mdabbert")
    in_rankings = resolver.resolve("Derrick", source="jerzyszocik")

    assert (in_mdabbert.fighter_id, in_mdabbert.status) == (9, "alias")
    assert (in_rankings.fighter_id, in_rankings.status) == (None, "unmatched")


def test_primary_name_match_wins_over_an_alias():
    resolver = NameResolver(
        {1: "Tim Johnson", 2: "Someone Else"}, aliases={("mdabbert", "tim johnson"): 2}
    )

    assert resolver.resolve("Tim Johnson", source="mdabbert").fighter_id == 1


def test_committed_aliases_file_is_well_formed():
    with ALIASES_CSV.open(newline="") as f:
        rows = list(csv.DictReader(f))

    assert rows, "aliases.csv should not be empty"
    keys = [(r["source"], normalize_name(r["alias"])) for r in rows]
    assert len(keys) == len(set(keys)), "duplicate (source, alias)"
    for row in rows:
        assert row["source"] in {"mdabbert", "jerzyszocik", "martj42"}
        assert row["alias"] == row["alias"].strip() and row["alias"]
        assert re.fullmatch(r"[0-9a-f]{16}", row["ufcstats_id"])


def test_load_aliases_is_scoped_idempotent_and_rejects_unknown_fighters(db, tmp_path):
    with db.begin() as conn:
        conn.execute(
            text("INSERT INTO fighters (id, ufcstats_id, name) VALUES (1, 'aaaa', 'King Green')")
        )
    good = tmp_path / "aliases.csv"
    good.write_text("source,alias,ufcstats_id,note\nmdabbert,Bobby Green,aaaa,x\n")

    with db.begin() as conn:
        load_aliases(conn, good)
        load_aliases(conn, good)
        resolver = NameResolver.from_db(conn)
    assert resolver.resolve("Bobby Green", source="mdabbert").fighter_id == 1
    assert resolver.resolve("Bobby Green", source="martj42").fighter_id is None

    bad = tmp_path / "bad.csv"
    bad.write_text("source,alias,ufcstats_id,note\nmdabbert,Nobody,zzzz,x\n")
    with pytest.raises(ValueError, match="unknown fighter"), db.begin() as conn:
        load_aliases(conn, bad)
