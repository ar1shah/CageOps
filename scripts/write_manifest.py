"""Fingerprint the seed files in data/raw/ and write MANIFEST.json.

Kaggle won't serve old dataset versions and its CLI doesn't expose a version number,
so we can't re-download "the same data" later. Instead we record a sha256 of every
file we actually loaded. If a row count differs next time, the manifest shows whether
the input files changed.

Usage:
    python scripts/write_manifest.py            # write the manifest
    python scripts/write_manifest.py --list     # print "dataset<TAB>file" per source
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

SOURCES_FILE = Path(__file__).with_name("seed_sources.json")


def load_sources(path: Path = SOURCES_FILE) -> list[dict[str, str]]:
    return json.loads(path.read_text())


def dataset_dir_name(dataset: str) -> str:
    return dataset.replace("/", "__")


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_listing(raw_dir: Path, dataset: str, file: str) -> str | None:
    """Kaggle's own creation timestamp for the file, from a saved `datasets files --csv`."""
    listing = raw_dir / ".listings" / f"{dataset_dir_name(dataset)}.csv"
    if not listing.exists():
        return None
    with listing.open(newline="") as f:
        for row in csv.DictReader(f):
            if row["name"] == file:
                return row["creationDate"]
    return None


def build_manifest(
    raw_dir: Path,
    sources: list[dict[str, str]],
    previous: dict | None = None,
    now: datetime | None = None,
    kaggle_cli: str | None = None,
) -> dict:
    """Hash every expected file on disk. Raises if one is missing, so the manifest can
    never describe a file that isn't there."""
    now = now or datetime.now(UTC)
    previous_by_path = {f["path"]: f for f in (previous or {}).get("files", [])}
    files = []
    for source in sources:
        rel = f"{dataset_dir_name(source['dataset'])}/{source['file']}"
        path = raw_dir / rel
        if not path.is_file():
            raise FileNotFoundError(f"expected seed file is missing: {path}")
        digest = sha256_of(path)
        old = previous_by_path.get(rel)
        # Same bytes as last time: keep the original download time. Otherwise it's new.
        downloaded_at = old["downloaded_at"] if old and old["sha256"] == digest else now.isoformat()
        files.append(
            {
                "dataset": source["dataset"],
                "file": source["file"],
                "path": rel,
                "size_bytes": path.stat().st_size,
                "sha256": digest,
                "downloaded_at": downloaded_at,
                "kaggle_file_created": read_listing(raw_dir, source["dataset"], source["file"]),
                "license": source["license"],
                "source_url": f"https://www.kaggle.com/datasets/{source['dataset']}",
            }
        )
    return {
        "manifest_version": 1,
        "generated_at": now.isoformat(),
        "kaggle_cli": kaggle_cli,
        "files": files,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--raw-dir", type=Path, default=Path("data/raw"))
    parser.add_argument(
        "--copy-to", type=Path, help="also write the manifest here (committed copy)"
    )
    parser.add_argument("--kaggle-cli", help="Kaggle CLI version string to record")
    parser.add_argument("--list", action="store_true", help="print sources and exit")
    args = parser.parse_args()

    sources = load_sources()
    if args.list:
        for s in sources:
            print(f"{s['dataset']}\t{s['file']}")
        return 0

    manifest_path = args.raw_dir / "MANIFEST.json"
    previous = json.loads(manifest_path.read_text()) if manifest_path.exists() else None
    manifest = build_manifest(args.raw_dir, sources, previous, kaggle_cli=args.kaggle_cli)
    text = json.dumps(manifest, indent=2) + "\n"
    manifest_path.write_text(text)
    if args.copy_to:
        args.copy_to.parent.mkdir(parents=True, exist_ok=True)
        args.copy_to.write_text(text)
    print(f"wrote {manifest_path} ({len(manifest['files'])} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
