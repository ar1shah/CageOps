import hashlib
import json
from datetime import UTC, datetime

import pytest
import write_manifest

SOURCES = [{"dataset": "owner/data", "file": "a.csv", "license": "CC0-1.0"}]
T1 = datetime(2026, 10, 2, tzinfo=UTC)
T2 = datetime(2026, 11, 1, tzinfo=UTC)


def make_file(raw_dir, content=b"hello\n"):
    path = raw_dir / "owner__data" / "a.csv"
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    return path


def test_sha256_and_size_match_file(tmp_path):
    make_file(tmp_path, b"hello\n")

    entry = write_manifest.build_manifest(tmp_path, SOURCES, now=T1)["files"][0]

    assert entry["sha256"] == hashlib.sha256(b"hello\n").hexdigest()
    assert entry["size_bytes"] == 6
    assert entry["path"] == "owner__data/a.csv"
    assert entry["license"] == "CC0-1.0"


def test_missing_file_raises_instead_of_describing_nothing(tmp_path):
    with pytest.raises(FileNotFoundError):
        write_manifest.build_manifest(tmp_path, SOURCES, now=T1)


def test_unchanged_file_keeps_original_download_time(tmp_path):
    make_file(tmp_path)
    first = write_manifest.build_manifest(tmp_path, SOURCES, now=T1)

    second = write_manifest.build_manifest(tmp_path, SOURCES, previous=first, now=T2)

    assert second["files"][0]["downloaded_at"] == T1.isoformat()


def test_changed_file_gets_new_download_time_and_hash(tmp_path):
    path = make_file(tmp_path)
    first = write_manifest.build_manifest(tmp_path, SOURCES, now=T1)
    path.write_bytes(b"different\n")

    second = write_manifest.build_manifest(tmp_path, SOURCES, previous=first, now=T2)

    assert second["files"][0]["downloaded_at"] == T2.isoformat()
    assert second["files"][0]["sha256"] != first["files"][0]["sha256"]


def test_kaggle_creation_date_read_from_saved_listing(tmp_path):
    make_file(tmp_path)
    listing = tmp_path / ".listings" / "owner__data.csv"
    listing.parent.mkdir()
    listing.write_text("name,size,creationDate\na.csv,6,2026-10-01 15:14:16.825000\n")

    entry = write_manifest.build_manifest(tmp_path, SOURCES, now=T1)["files"][0]

    assert entry["kaggle_file_created"] == "2026-10-01 15:14:16.825000"


def test_real_sources_file_is_valid():
    sources = write_manifest.load_sources()

    assert {"dataset", "file", "license"} <= set(sources[0])
    assert json.dumps(sources)
