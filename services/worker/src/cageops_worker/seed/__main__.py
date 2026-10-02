"""Load the seed data: `uv run python -m cageops_worker.seed`.

Refuses to run if a file's sha256 differs from docs/seed_manifest.json.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from cageops_common.config import get_settings
from cageops_common.db.session import make_engine
from cageops_worker.seed.manifest import (
    DEFAULT_MANIFEST,
    DEFAULT_RAW_DIR,
    ManifestMismatchError,
    verified_file,
)
from cageops_worker.seed.silver import load_silver

SILVER = (
    "jerzyszocik/ufc-fight-forecast-complete-gold-modeling-dataset",
    "full_data_silver_plus.parquet",
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Load seed data into Postgres (idempotent).")
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args(argv)

    engine = make_engine(get_settings().database_url)
    reports = []
    try:
        path, sha = verified_file(*SILVER, args.raw_dir, args.manifest)
    except ManifestMismatchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    reports.append(load_silver(engine, path, sha))

    print(json.dumps(reports, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
