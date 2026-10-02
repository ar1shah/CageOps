from datetime import date

from cageops_worker.seed.resolver import NameResolver

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

    assert resolver.resolve("Jan Blachowicz").fighter_id == 1
    assert resolver.resolve("kai kara france").fighter_id == 2


def test_alias_resolves_a_name_with_no_primary_match():
    resolver = NameResolver({7: "King Green"}, aliases={"bobby green": 7})

    result = resolver.resolve("Bobby Green")

    assert (result.fighter_id, result.status) == (7, "alias")


def test_unknown_name_is_unmatched():
    result = NameResolver({1: "A B"}).resolve("Nobody Here")

    assert (result.fighter_id, result.status) == (None, "unmatched")


def test_shared_name_is_disambiguated_by_weight_class():
    resolver = two_brunos(overlap=True)

    middle = resolver.resolve("Bruno Silva", weight_class="Middleweight", on=date(2023, 4, 22))
    fly = resolver.resolve("Bruno Silva", weight_class="Flyweight", on=date(2023, 3, 11))

    assert (middle.fighter_id, middle.status) == (BRUNO_MIDDLE, "disambiguated")
    assert (fly.fighter_id, fly.status) == (BRUNO_FLY, "disambiguated")


def test_shared_name_is_disambiguated_by_active_dates_alone():
    resolver = two_brunos(overlap=False)

    result = resolver.resolve("Bruno Silva", on=date(2022, 1, 1))

    assert result.fighter_id == BRUNO_MIDDLE  # the other Bruno retired in 2012


def test_shared_name_is_never_guessed_without_enough_context():
    resolver = two_brunos(overlap=True)

    no_context = resolver.resolve("Bruno Silva")
    only_date = resolver.resolve("Bruno Silva", on=date(2023, 1, 1))  # both active then
    wrong_class = resolver.resolve("Bruno Silva", weight_class="Bantamweight", on=date(2019, 10, 5))

    for result in (no_context, only_date, wrong_class):
        assert (result.fighter_id, result.status) == (None, "ambiguous")
