"""One mapping table for every weight-class spelling seen in the seed files.

Unknown values fail the load: a new spelling should be a deliberate edit here, not a
silently invented category.
"""

from __future__ import annotations

from dataclasses import dataclass

from cageops_worker.seed.normalize import UnknownValueError

MENS_P4P = "Men's Pound-for-Pound"
WOMENS_P4P = "Women's Pound-for-Pound"


@dataclass(frozen=True)
class WeightClass:
    canonical: str | None  # None = the source says the weight class is unknown
    pound_for_pound: bool = False
    title_fight: bool = False  # True when the source put a title name in the weight-class column


_DIVISIONS = [
    "Flyweight",
    "Bantamweight",
    "Featherweight",
    "Lightweight",
    "Welterweight",
    "Middleweight",
    "Light Heavyweight",
    "Heavyweight",
    "Women's Strawweight",
    "Women's Flyweight",
    "Women's Bantamweight",
    "Women's Featherweight",
    "Open Weight",
    "Catch Weight",
]

WEIGHT_CLASSES: dict[str, WeightClass] = {name: WeightClass(name) for name in _DIVISIONS}

# Pound-for-pound appears under several spellings across eras (the rankings file renamed
# the list over time and has a missing-space typo). The bare "Pound-for-Pound" list runs
# 2013-02-04 to 2020-01-20, before women's P4P existed (starts 2020-01-27), so it is the men's list.
WEIGHT_CLASSES |= {
    "Pound-for-Pound": WeightClass(MENS_P4P, True),
    "Men's Pound-for-Pound": WeightClass(MENS_P4P, True),
    "Men's Pound-for-Pound Top Rank": WeightClass(MENS_P4P, True),
    "Men's Pound-for-PoundTop Rank": WeightClass(MENS_P4P, True),
    "Women's Pound-for-Pound": WeightClass(WOMENS_P4P, True),
    "Women's Pound-for-Pound Top Rank": WeightClass(WOMENS_P4P, True),
    "Women's Pound-for-PoundTop Rank": WeightClass(WOMENS_P4P, True),
}


# Recent silver rows put the title name in the weight-class column and leave title_fight
# false (a ufcstats format change). They are title fights, so the flag is set from here.
_TITLE_DIVISIONS = [
    "Light Heavyweight", "Flyweight", "Bantamweight", "Middleweight", "Heavyweight",
    "Welterweight", "Featherweight", "Women's Flyweight", "Women's Strawweight",
]  # fmt: skip
for _division in _TITLE_DIVISIONS:
    WEIGHT_CLASSES[f"UFC {_division} Title"] = WeightClass(_division, title_fight=True)
WEIGHT_CLASSES["UFC Interim Lightweight Title"] = WeightClass("Lightweight", title_fight=True)
# The first UFC tournaments had no weight classes; their finals were championship bouts.
for _n in (2, 3, 4):
    WEIGHT_CLASSES[f"UFC {_n} Tournament Title"] = WeightClass("Open Weight", title_fight=True)
# Explicitly unknown: the literal string "NULL" (early fights) and "Nieznana" (Polish for
# "unknown"). Stored as NULL, never guessed.
WEIGHT_CLASSES["NULL"] = WeightClass(None)
WEIGHT_CLASSES["Nieznana"] = WeightClass(None)


def lookup(raw: str) -> WeightClass:
    try:
        return WEIGHT_CLASSES[raw.strip()]
    except KeyError:
        raise UnknownValueError(f"unknown weight class: {raw!r}") from None


def fight_weight_class(raw: str | None) -> WeightClass:
    """Weight class of a fight. A missing value means unknown (None), not an error.

    Pound-for-pound is a rankings list, not a fight weight class.
    """
    if raw is None:
        return WeightClass(None)
    weight_class = lookup(raw)
    if weight_class.pound_for_pound:
        raise UnknownValueError(f"pound-for-pound is not a fight weight class: {raw!r}")
    return weight_class
