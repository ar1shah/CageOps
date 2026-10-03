"""Read docs/seed_manifest.json and refuse to load files whose bytes have changed."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

DEFAULT_MANIFEST = Path("docs/seed_manifest.json")
DEFAULT_RAW_DIR = Path("data/raw")


class ManifestMismatchError(RuntimeError):
    pass


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verified_file(
    dataset: str, filename: str, raw_dir: Path, manifest_path: Path
) -> tuple[Path, str]:
    """Return (path, sha256) for a seed file after checking it matches the manifest."""
    manifest = json.loads(manifest_path.read_text())
    entry = next(
        (f for f in manifest["files"] if f["dataset"] == dataset and f["file"] == filename), None
    )
    if entry is None:
        raise ManifestMismatchError(f"{dataset}/{filename} is not listed in {manifest_path}")
    path = raw_dir / entry["path"]
    if not path.is_file():
        raise ManifestMismatchError(f"{path} is missing; run scripts/download_seed.sh")
    actual = sha256_of(path)
    if actual != entry["sha256"]:
        raise ManifestMismatchError(
            f"{path} has sha256 {actual[:12]}... but the manifest says {entry['sha256'][:12]}... "
            "The data changed since the manifest was written. Re-run scripts/download_seed.sh "
            "and review the new manifest before loading."
        )
    return path, actual
