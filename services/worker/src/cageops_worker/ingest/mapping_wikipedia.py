"""Wikipedia bouts into our database vocabulary. Pure: no database, no network.

Everything stored is a fact (names, who won, how, round, time). The method text's parenthesis
("punches", "rear-naked choke", the judges' scorecards) and the notes are never kept: they are
prose, and Wikipedia's text is CC BY-SA (D-029). `method_detail` stays NULL for these rows.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from cageops_scraper.parsers.common import squash
from cageops_scraper.parsers.wikipedia_models import WikiBout, WikiEventPage, WikiFighter
from cageops_worker.ingest.errors import MappingError
from cageops_worker.ingest.mapping import gender_for
from cageops_worker.seed.normalize import UnknownValueError, normalize_name, parse_finish_time
from cageops_worker.seed.weight_class import fight_weight_class

DISTINCT_FIGHTERS_CSV = Path(__file__).parent / "wikipedia_distinct_fighters.csv"
SIMILAR_NAME = 0.9  # a new name this close to an existing one is held for review, not guessed

_CATCH_WEIGHT = re.compile(r"^catch\s*weight\b", re.IGNORECASE)
_OPEN_WEIGHT = re.compile(r"^open\s*weight\b", re.IGNORECASE)
_DECISION_TYPE = re.compile(r"^[^(]+\(\s*(unanimous|split|majority)\s*\)", re.IGNORECASE)


@dataclass(frozen=True)
class WikiMethod:
    kind: str  # win | draw | no_contest: what the method text says about the result
    method: str  # decision | ko_tko | submission | dq | other
    decision_type: str | None  # unanimous | split | majority


@dataclass(frozen=True)
class MappedWikiBout:
    first: WikiFighter
    second: WikiFighter
    weight_class: str | None
    gender: str
    gender_guessed: bool
    is_title_fight: bool
    outcome: str  # win | draw | no_contest
    first_won: bool  # only meaningful when outcome == "win"
    method: str
    decision_type: str | None
    finish_round: int | None
    finish_time_sec: int | None


def wikipedia_weight_class(raw: str) -> str | None:
    """'Women’s Featherweight' -> "Women's Featherweight"; 'Catchweight (150.5 lb)' -> 'Catch
    Weight' (the stated weight is prose and is dropped)."""
    text = squash(raw.replace("’", "'"))
    if _CATCH_WEIGHT.match(text):
        return "Catch Weight"
    if _OPEN_WEIGHT.match(text):
        return "Open Weight"
    return fight_weight_class(text).canonical


def normalize_wikipedia_method(raw: str) -> WikiMethod:
    """'Decision (unanimous) (30-27, ...)', 'TKO (corner stoppage)', 'Technical Submission (...)',
    'Draw (majority) (...)', 'NC (accidental eye poke)', 'DQ (illegal knee)' and their long
    spellings. Anything else raises: we would rather stop than store a guess."""
    text = squash(raw)
    head = re.split(r"\s*\(", text, maxsplit=1)[0].strip().lower()
    typed = _DECISION_TYPE.match(text)
    decision_type = typed.group(1).lower() if typed else None
    if head in ("decision", "technical decision"):
        return WikiMethod("win", "decision", decision_type)
    if head in ("ko", "tko"):
        return WikiMethod("win", "ko_tko", None)  # doctor stoppage, corner stoppage, retirement
    if head in ("submission", "technical submission"):
        return WikiMethod("win", "submission", None)
    if head in ("dq", "disqualification"):
        return WikiMethod("win", "dq", None)
    if head == "draw":
        return WikiMethod("draw", "decision", decision_type)
    if head in ("nc", "no contest"):
        return WikiMethod("no_contest", "other", None)
    raise UnknownValueError(f"unknown Wikipedia fight method: {raw!r}")


def map_bout(bout: WikiBout) -> MappedWikiBout:
    method = normalize_wikipedia_method(bout.method_raw)
    names = f"{bout.first.name} {bout.versus} {bout.second.name}"
    if (bout.versus == "def.") != (method.kind == "win"):
        raise MappingError(f"{names}: the method {bout.method_raw!r} doesn't fit '{bout.versus}'")
    if bout.round is not None and not 1 <= bout.round <= 5:
        raise MappingError(f"{names}: round {bout.round} is not between 1 and 5")
    weight_class = wikipedia_weight_class(bout.weight_class_raw)
    gender, guessed = gender_for(weight_class)
    return MappedWikiBout(
        first=bout.first,
        second=bout.second,
        weight_class=weight_class,
        gender=gender,
        gender_guessed=guessed,
        # Only a championship footnote makes a title fight. The BMF belt is not one (the seed
        # agrees: UFC 244's main event is false there), and a (c) marker alone is not enough.
        is_title_fight=bout.note_kind == "championship",
        outcome=method.kind,
        first_won=method.kind == "win",
        method=method.method,
        decision_type=method.decision_type,
        finish_round=bout.round,
        finish_time_sec=parse_finish_time(bout.time_raw),
    )


def map_event(page: WikiEventPage) -> dict[str, object]:
    return {
        "name": page.name,
        "event_date": page.event_date,
        "wikipedia_article_id": page.article_id,
    }


# -- new names that might be old fighters ------------------------------------------------------


def _within_one_edit(a: str, b: str) -> bool:
    """True if b is a with one character inserted, deleted, changed or two neighbours swapped.
    (A similarity ratio alone misses these in short names: one typo in "petr yan" scores 0.875.)"""
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        diffs = [i for i, (x, y) in enumerate(zip(a, b, strict=True)) if x != y]
        if len(diffs) == 1:
            return True  # one character changed
        return (
            len(diffs) == 2
            and diffs[1] == diffs[0] + 1
            and a[diffs[0]] == b[diffs[1]]
            and a[diffs[1]] == b[diffs[0]]
        )
    short, long = sorted((a, b), key=len)
    return any(long[:i] + long[i + 1 :] == short for i in range(len(long)))  # one character added


class DuplicateIndex:
    """Finds existing fighters a new name might really be. A new name that matches nobody exactly
    is held for review if it looks like an existing fighter by any of these rules (measured on six
    real cards: of 17 unmatched names, 7 were old fighters; these rules catch 6 and flag none of
    the 10 genuinely new ones):

    - the same words in another order ("Zhang Weili" / "Weili Zhang")
    - the same letters, however spaced or hyphenated ("Park Jun-yong" / "JunYong Park")
    - one typo, or a similarity of 0.9 or more
    - one name's words are all in the other ("Michelle Waterson" / "Michelle Waterson-Gomez",
      "Mizuki Inoue" / "Mizuki")
    - the same surname and first initial ("Beatriz Mesquita" / "Bia Mesquita")

    A different fighter with the same surname and another initial is not flagged. A transliteration
    ("Alexey Oleynik" / "Aleksei Oleinik") is not caught: it shows in the run's stub list.
    """

    def __init__(self, fighters: dict[int, str]):
        self._normalized = {fid: normalize_name(name) for fid, name in fighters.items()}
        self._names = fighters

    def similar(self, name: str) -> list[tuple[int, str, str]]:
        """(fighter id, their name, why it looks the same) for each existing fighter it might be."""
        key = normalize_name(name)
        found = []
        for fid, other in self._normalized.items():
            if other == key:
                continue  # an exact match is resolved before this check
            if reason := _why_similar(key, other):
                found.append((fid, self._names[fid], reason))
        return found


def _why_similar(key: str, other: str) -> str | None:
    words, other_words = key.split(), other.split()
    if not words or not other_words:
        return None
    if len(words) > 1 and sorted(words) == sorted(other_words):
        return "the same words in another order"
    if sorted(key.replace(" ", "")) == sorted(other.replace(" ", "")):
        return "the same letters spaced differently"
    if _within_one_edit(key, other):
        return "one letter off"
    if set(words) <= set(other_words) or set(other_words) <= set(words):
        return "one name's words are all in the other"
    if words[-1] == other_words[-1] and words[0][0] == other_words[0][0]:
        return "the same surname and first initial"
    matcher = SequenceMatcher(None, key, other)
    if matcher.real_quick_ratio() >= SIMILAR_NAME and matcher.ratio() >= SIMILAR_NAME:
        return "a very similar spelling"
    return None


def load_distinct_titles(path: Path = DISTINCT_FIGHTERS_CSV) -> frozenset[str]:
    """Link titles a person has confirmed are NEW fighters, so the similar-name check lets them
    through (a different Michael Johnson, say)."""
    with path.open(newline="", encoding="utf-8") as handle:
        return frozenset(row["link_title"] for row in csv.DictReader(handle) if row["link_title"])
