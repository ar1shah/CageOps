"""Write the load test's inputs from the REAL database: unique same-division pairs, fighter ids
and search terms. Nothing is invented: a pair is two fighters who both have current features and
whose most recent standard divisions are the same, so every /predict in the test is a valid 200
and none is a 422 that would make the numbers a measurement of error handling.

    uv run python scripts/loadtest/make_inputs.py [--database-url URL] [--out PATH]
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import random
import re
from collections import defaultdict
from pathlib import Path

from sqlalchemy import text

from cageops_api.queries import STANDARD_DIVISIONS
from cageops_common.db.session import make_engine

DEFAULT_OUT = Path("data/bench/api/inputs.json")
LOADTEST_DB = "cageops_loadtest"

_SQL = """
SELECT c.fighter_id, d.weight_class
  FROM fighter_features_current c
  JOIN LATERAL (
    SELECT cf.weight_class
      FROM completed_fights cf JOIN events e ON e.id = cf.event_id
     WHERE (cf.fighter_a_id = c.fighter_id OR cf.fighter_b_id = c.fighter_id)
       AND cf.weight_class = ANY(:divisions)
     ORDER BY e.event_date DESC, cf.id DESC LIMIT 1
  ) d ON true
 WHERE c.n_fights_career >= 3
"""


def loadtest_url() -> str:
    base = os.environ["DATABASE_URL"]
    head, _, _ = base.rpartition("/")
    return f"{head}/{LOADTEST_DB}"


def build(database_url: str, *, pair_count: int, warm_pairs: int, seed: int) -> dict:
    engine = make_engine(database_url)
    rng = random.Random(seed)
    with engine.connect() as conn:
        rows = conn.execute(text(_SQL), {"divisions": list(STANDARD_DIVISIONS)}).all()
        names = conn.execute(
            text(
                "SELECT f.id, f.name FROM fighters f WHERE EXISTS"
                " (SELECT 1 FROM completed_fights cf WHERE cf.fighter_a_id = f.id"
                "  OR cf.fighter_b_id = f.id)"
            )
        ).all()
    by_division: dict[str, list[int]] = defaultdict(list)
    for fighter_id, division in rows:
        by_division[division].append(fighter_id)

    pairs: list[list[int]] = []
    for _, ids in sorted(by_division.items()):
        ids.sort()
        pairs += [list(p) for p in itertools.combinations(ids, 2)]
    available = len(pairs)
    rng.shuffle(pairs)
    pairs = pairs[:pair_count]
    if len(pairs) < warm_pairs + 1000:
        raise SystemExit(f"only {available} same-division pairs: not enough for a cold run")

    terms = sorted(
        {m.group(0).lower() for _, name in names for m in [re.search(r"[A-Za-z]{4,}$", name)] if m}
    )
    rng.shuffle(terms)
    ids = sorted(i for i, _ in names)
    rng.shuffle(ids)
    return {
        "pairs": pairs,
        "warm_pairs": warm_pairs,
        "search_terms": terms[:300],
        "fighter_ids": ids[:3000],
        "meta": {
            "seed": seed,
            "same_division_pairs_available": available,
            "pairs_written": len(pairs),
            "divisions": {k: len(v) for k, v in sorted(by_division.items())},
            "fighters_with_fights": len(names),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--pairs", type=int, default=60_000)
    parser.add_argument("--warm", type=int, default=500)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    data = build(
        args.database_url or loadtest_url(),
        pair_count=args.pairs,
        warm_pairs=args.warm,
        seed=args.seed,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(data))
    print(json.dumps(data["meta"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
