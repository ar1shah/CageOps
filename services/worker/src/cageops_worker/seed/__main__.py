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
from cageops_worker.seed.odds import (
    RedCornerDisagreementError,
    load_mdabbert,
    load_silver_odds,
    odds_summary,
)
from cageops_worker.seed.rankings import load_rankings
from cageops_worker.seed.silver import load_silver

MDABBERT = ("mdabbert/ultimate-ufc-dataset", "ufc-master.csv")
RANKINGS = {
    "jerzyszocik": ("jerzyszocik/ufc-rankings-history", "UFC_rankings_history.csv"),
    "martj42": ("martj42/ufc-rankings", "rankings_history.csv"),
}
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
    reports.append(load_silver_odds(engine, path, sha))
    try:
        mdabbert_path, mdabbert_sha = verified_file(*MDABBERT, args.raw_dir, args.manifest)
        reports.append(load_mdabbert(engine, mdabbert_path, mdabbert_sha))
    except (ManifestMismatchError, RedCornerDisagreementError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        if isinstance(exc, RedCornerDisagreementError):
            print(json.dumps(exc.report, indent=2, default=str), file=sys.stderr)
        return 1
    reports.append(odds_summary(engine))
    for source, (dataset, filename) in RANKINGS.items():
        try:
            rankings_path, rankings_sha = verified_file(
                dataset, filename, args.raw_dir, args.manifest
            )
        except ManifestMismatchError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        reports.append(load_rankings(engine, source, rankings_path, rankings_sha))

    print(json.dumps(reports, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
