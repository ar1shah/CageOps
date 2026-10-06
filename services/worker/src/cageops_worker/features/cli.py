"""The feature command line: `uv run python -m cageops_worker.features <command>`.

  rebuild  recompute every fight_features row from scratch (one transaction)
  show     print one fighter's feature rows in date order

Exit codes match the ingest CLI: 0 ok, 1 error (nothing found, a build check failed), 2 bad usage
(argparse), or a name that matches more than one fighter.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import date
from typing import IO, Any

from sqlalchemy import Engine, text

from cageops_common.config import get_settings
from cageops_common.db.session import make_engine
from cageops_worker.features.build import FeatureBuildError, rebuild
from cageops_worker.seed.normalize import normalize_name

PROG = "python -m cageops_worker.features"
EXIT_OK, EXIT_ERROR, EXIT_USAGE = 0, 1, 2

# The columns `show` prints as a table; `--json` prints all of them.
SHOW_COLUMNS = [
    "event_date",
    "opponent",
    "prior_ufc_fights",
    "win_streak",
    "days_since_last_fight",
    "age_days",
    "finish_rate",
    "sig_str_landed_pm_career",
    "sig_str_absorbed_pm_career",
    "sig_str_def_career",
    "td_def_career",
]

_ROWS = """
SELECT e.event_date, opp.name AS opponent, ff.*
FROM fight_features ff
JOIN fights f ON f.id = ff.fight_id
JOIN events e ON e.id = f.event_id
JOIN fighters opp
  ON opp.id = CASE WHEN f.fighter_a_id = ff.fighter_id THEN f.fighter_b_id ELSE f.fighter_a_id END
WHERE ff.fighter_id = :fighter_id
ORDER BY e.event_date, ff.fight_id
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROG, description="Point-in-time fight features.")
    commands = parser.add_subparsers(dest="command", required=True)

    rebuild_cmd = commands.add_parser("rebuild", help="recompute every feature row from scratch")
    rebuild_cmd.add_argument(
        "--today",
        type=date.fromisoformat,
        default=None,
        metavar="YYYY-MM-DD",
        help="treat this as today; scheduled fights dated before it are left out (default: today)",
    )

    show = commands.add_parser("show", help="print one fighter's feature rows in date order")
    who = show.add_mutually_exclusive_group(required=True)
    who.add_argument("--fighter", metavar="NAME", help="fighter name (accents and case ignored)")
    who.add_argument("--fighter-id", type=int, metavar="ID", help="use when a name is ambiguous")
    show.add_argument("--json", action="store_true", help="print every column as JSON")
    return parser


def cmd_rebuild(engine: Engine, args: argparse.Namespace, out: IO[str]) -> int:
    try:
        report = rebuild(engine, args.today or date.today())
    except FeatureBuildError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print(json.dumps(exc.report, indent=2, default=str), file=sys.stderr)
        return EXIT_ERROR
    print(json.dumps(report, indent=2, default=str), file=out)
    return EXIT_OK


def find_fighters(engine: Engine, name: str) -> list[tuple[int, str]]:
    """Fighters whose normalized name, or any stored alias, equals the normalized input."""
    key = normalize_name(name)
    with engine.connect() as conn:
        by_name = {
            r.id: r.name
            for r in conn.execute(text("SELECT id, name FROM fighters"))
            if normalize_name(r.name) == key
        }
        aliased = conn.execute(
            text(
                "SELECT f.id, f.name FROM fighter_aliases a JOIN fighters f ON f.id = a.fighter_id"
                " WHERE a.alias_norm = :key"
            ),
            {"key": key},
        )
        by_name |= {r.id: r.name for r in aliased}
    return sorted(by_name.items())


def _cell(value: Any) -> str:
    if value is None:
        return "-"
    return f"{value:.2f}" if isinstance(value, float) else str(value)


def cmd_show(engine: Engine, args: argparse.Namespace, out: IO[str]) -> int:
    if args.fighter_id is not None:
        fighter_id = args.fighter_id
    else:
        matches = find_fighters(engine, args.fighter)
        if not matches:
            print(f"ERROR: no fighter matches {args.fighter!r}", file=sys.stderr)
            return EXIT_ERROR
        if len(matches) > 1:
            print(f"{args.fighter!r} matches {len(matches)} fighters; use --fighter-id:", file=out)
            for found_id, found_name in matches:
                print(f"  {found_id}  {found_name}", file=out)
            return EXIT_USAGE
        fighter_id = matches[0][0]

    with engine.connect() as conn:
        rows = [dict(r) for r in conn.execute(text(_ROWS), {"fighter_id": fighter_id}).mappings()]
    if not rows:
        print(f"ERROR: no feature rows for fighter {fighter_id} (run `rebuild`?)", file=sys.stderr)
        return EXIT_ERROR

    if args.json:
        print(json.dumps(rows, indent=2, default=str), file=out)
        return EXIT_OK
    table = [[_cell(r[c]) for c in SHOW_COLUMNS] for r in rows]
    widths = [max(len(c), *(len(row[i]) for row in table)) for i, c in enumerate(SHOW_COLUMNS)]
    print("  ".join(c.ljust(w) for c, w in zip(SHOW_COLUMNS, widths, strict=True)), file=out)
    for row in table:
        print("  ".join(v.ljust(w) for v, w in zip(row, widths, strict=True)), file=out)
    return EXIT_OK


COMMANDS = {"rebuild": cmd_rebuild, "show": cmd_show}


def main(
    argv: Sequence[str] | None = None,
    *,
    engine: Engine | None = None,
    out: IO[str] | None = None,
) -> int:
    args = build_parser().parse_args(argv)  # exits 2 on bad usage
    engine = engine or make_engine(get_settings().database_url)
    return COMMANDS[args.command](engine, args, out or sys.stdout)
