#!/usr/bin/env bash
# Download the seed datasets listed in scripts/seed_sources.json into data/raw/ and
# write data/raw/MANIFEST.json (sha256 per file) plus a committed copy in docs/.
#
#   scripts/download_seed.sh            # download missing files, re-hash everything
#   scripts/download_seed.sh --force    # re-download everything, then re-hash
#
# Needs Kaggle credentials in ~/.kaggle/ (never in this repo).
set -euo pipefail

KAGGLE_VERSION="2.2.4"   # pinned so CLI behavior can't change under us
FORCE=0
[[ "${1:-}" == "--force" ]] && FORCE=1

cd "$(dirname "$0")/.."
RAW=data/raw
mkdir -p "$RAW/.listings"

kaggle() { uvx --quiet --from "kaggle==${KAGGLE_VERSION}" kaggle "$@"; }

while IFS=$'\t' read -r dataset file; do
  dir="$RAW/${dataset//\//__}"
  target="$dir/$file"
  mkdir -p "$dir"

  # Kaggle's own file listing (includes its creation timestamp); saved for the manifest.
  kaggle datasets files "$dataset" --csv > "$RAW/.listings/${dataset//\//__}.csv"

  if [[ -f "$target" && $FORCE -eq 0 ]]; then
    echo "skip   $dataset/$file (already present; use --force to re-download)"
    continue
  fi

  echo "fetch  $dataset/$file"
  rm -f "$target" "$target.zip"
  kaggle datasets download "$dataset" -f "$file" -p "$dir" --force --quiet

  # Kaggle sometimes delivers a single file as <name>.zip.
  if [[ -f "$target.zip" ]]; then
    python3 -m zipfile -e "$target.zip" "$dir"
    rm "$target.zip"
  fi

  # Exit code 0 isn't proof the file arrived; check it did and isn't empty.
  if [[ ! -s "$target" ]]; then
    echo "ERROR: expected $target after download, but it is missing or empty" >&2
    exit 1
  fi
done < <(uv run python scripts/write_manifest.py --list)

# Always hash what's on disk, including files we skipped, so the manifest matches reality.
uv run python scripts/write_manifest.py --kaggle-cli "$KAGGLE_VERSION" --copy-to docs/seed_manifest.json
