"""Pure cleaning functions for the seed data. No database access, easy to test."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal

# Letters that don't decompose into "base letter + accent" under Unicode normalization.
_SPECIAL_LETTERS = str.maketrans(
    {"ł": "l", "ø": "o", "đ": "d", "ð": "d", "æ": "ae", "œ": "oe", "ß": "ss", "ı": "i"}
)


class UnknownValueError(ValueError):
    """A raw value we have no mapping for. The loader fails instead of guessing."""


def normalize_name(name: str) -> str:
    """Lowercase, strip accents, drop punctuation, collapse spaces.

    "José Aldo" -> "jose aldo", "Kai Kara-France" -> "kai kara france",
    "Khalil Rountree Jr." -> "khalil rountree jr".
    """
    text = unicodedata.normalize("NFKD", name.lower().translate(_SPECIAL_LETTERS))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("-", " ")
    text = re.sub(r"[^a-z0-9 ]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def clean_text(value: str | None) -> str | None:
    """Silver stores missing text as the literal string "NULL" (referee, event_state, stance,
    ...) as well as real nulls. Both mean missing."""
    if value is None:
        return None
    value = _squash(value)
    return None if value in ("", "NULL") else value


def ufcstats_id(url: str) -> str:
    """The id at the end of a ufcstats URL, e.g. .../fight-details/2556b7520536ce1d."""
    return url.rstrip("/").rsplit("/", 1)[-1]


@dataclass(frozen=True)
class Method:
    method: str  # decision | ko_tko | submission | dq | other
    decision_type: str | None  # unanimous | split | majority | None
    method_detail: str | None


# Cleaned raw `result` value -> (method, decision_type). Anything else fails the load.
_RESULTS: dict[str, tuple[str, str | None]] = {
    "Decision": ("decision", None),
    "Decision - Unanimous": ("decision", "unanimous"),
    "Decision - Split": ("decision", "split"),
    "Decision - Majority": ("decision", "majority"),
    "KO/TKO": ("ko_tko", None),
    "TKO - Doctor's Stoppage": ("ko_tko", None),  # UFC records it as a TKO
    "Submission": ("submission", None),
    "DQ": ("dq", None),
    "Could Not Continue": ("other", None),
}


def _squash(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def normalize_result(raw_result: str | None, raw_details: str | None) -> Method:
    """Clean silver's `result` and `result_details` into one method vocabulary.

    A bare "Decision" carries its type in the details column ("Unanimous", ...). Otherwise
    the details are free text (a submission name, a strike, or judges' scorecards) and are
    kept as method_detail. Doctor's stoppage stays visible in method_detail.
    """
    result = _squash(raw_result)
    details = _squash(raw_details) or None
    if result not in _RESULTS:
        raise UnknownValueError(f"unknown fight result: {raw_result!r}")
    method, decision_type = _RESULTS[result]

    if result == "Decision":
        if details is None:
            return Method(method, None, None)
        if details.lower() not in {"unanimous", "split", "majority"}:
            raise UnknownValueError(f"unknown decision type: {raw_details!r}")
        return Method(method, details.lower(), None)
    if result == "TKO - Doctor's Stoppage":
        return Method(method, None, "Doctor's Stoppage" + (f" ({details})" if details else ""))
    return Method(method, decision_type, details)


def parse_finish_time(value: str | None) -> int | None:
    """'3:33' -> 213 seconds into the finishing round."""
    if not value:
        return None
    match = re.fullmatch(r"(\d+):(\d{2})", value.strip())
    if not match:
        raise UnknownValueError(f"unexpected finish time: {value!r}")
    return int(match.group(1)) * 60 + int(match.group(2))


def american_to_decimal(moneyline: float) -> Decimal:
    """+150 -> 2.500, -200 -> 1.500."""
    if moneyline == 0:
        raise ValueError("moneyline odds cannot be 0")
    value = 1 + moneyline / 100 if moneyline > 0 else 1 + 100 / abs(moneyline)
    return Decimal(str(round(value, 3)))
