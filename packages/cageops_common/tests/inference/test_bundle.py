"""A bundle is written, verified and loaded; every way it can be wrong is refused.

The booster is a tiny LightGBM model trained right here (random data, ~10 trees): these tests are
about the file handling and the checks, not about prediction quality.
"""

import json
import shutil
from datetime import date

import lightgbm as lgb
import numpy as np
import pytest

from cageops_common.inference import features as f
from cageops_common.inference.bundle import (
    MANIFEST_NAME,
    BundleError,
    file_sha256,
    load_bundle,
    read_manifest,
    verify_bundle,
    write_bundle,
)
from cageops_common.inference.model import MethodModel, WinLossModel

VERSION = "20300101-000000"


def vocab(ordered: bool) -> f.Vocabulary:
    return f.Vocabulary(
        ordered=ordered,
        stance_matchups=("orthodox_vs_orthodox", f.OTHER_MATCHUP),
        weight_classes=("Lightweight", "Welterweight"),
    )


def booster(names, objective, num_class=None, seed=0) -> lgb.Booster:
    rng = np.random.default_rng(seed)
    x = rng.normal(size=(300, len(names)))
    if objective == "binary":
        y = (x[:, 0] + rng.normal(0, 0.5, 300) > 0).astype(int)
    else:
        y = rng.integers(0, 3, 300)
    params = {"objective": objective, "verbosity": -1, "num_leaves": 4, "seed": seed}
    if num_class:
        params["num_class"] = num_class
    return lgb.train(params, lgb.Dataset(x, label=y, feature_name=list(names)), num_boost_round=10)


@pytest.fixture(scope="module")
def models():
    win = WinLossModel(booster(f.WIN_FEATURE_NAMES, "binary"), vocab(True))
    method = MethodModel(booster(f.METHOD_FEATURE_NAMES, "multiclass", 3), vocab(False))
    return win, method


@pytest.fixture
def bundle(tmp_path, models):
    win, method = models
    path = tmp_path / VERSION
    path.mkdir()
    manifest = write_bundle(
        path, version=VERSION, data_cutoff=date(2024, 9, 28), win_model=win, method_model=method
    )
    return path, manifest


def test_a_written_bundle_has_exactly_three_files_and_checksums_of_file_bytes(bundle):
    path, manifest = bundle

    assert sorted(p.name for p in path.iterdir()) == ["manifest.json", "method.txt", "win_loss.txt"]
    assert manifest.files_sha256 == {
        "win_loss.txt": file_sha256(path / "win_loss.txt"),
        "method.txt": file_sha256(path / "method.txt"),
    }
    assert read_manifest(path) == manifest


def test_a_loaded_bundle_predicts_what_the_in_memory_models_predict(bundle, models):
    path, manifest = bundle
    win, method = models
    rng = np.random.default_rng(1)
    n = len(f.NUMERIC_FEATURES)
    a, b = rng.normal(size=(20, n)), rng.normal(size=(20, n))
    sa = sb = ["orthodox"] * 20
    wc = ["Lightweight"] * 10 + ["Welterweight"] * 5 + [None] * 5

    loaded = load_bundle(path, manifest.files_sha256, version=VERSION)

    assert loaded.version == VERSION
    assert loaded.data_cutoff == date(2024, 9, 28)
    assert loaded.weight_classes == ("Lightweight", "Welterweight")
    assert np.array_equal(loaded.win_loss.predict(a, b, sa, sb, wc), win.predict(a, b, sa, sb, wc))
    assert np.array_equal(loaded.method.predict(a, b, sa, sb, wc), method.predict(a, b, sa, sb, wc))


def test_a_changed_model_file_is_refused(bundle):
    path, manifest = bundle
    with (path / "win_loss.txt").open("a") as fh:
        fh.write("\n# tampered\n")

    with pytest.raises(BundleError, match="win_loss.txt does not match"):
        load_bundle(path, manifest.files_sha256)


def test_a_wrong_expected_checksum_is_refused(bundle):
    path, manifest = bundle
    expected = {**manifest.files_sha256, "method.txt": "0" * 64}

    with pytest.raises(BundleError, match="does not match the registry"):
        load_bundle(path, expected)


def test_a_missing_file_a_missing_manifest_and_a_missing_directory_are_refused(bundle, tmp_path):
    path, manifest = bundle
    (path / "method.txt").unlink()
    with pytest.raises(BundleError, match="method.txt is missing"):
        verify_bundle(path, manifest.files_sha256)

    (path / MANIFEST_NAME).unlink()
    with pytest.raises(BundleError, match="no manifest.json"):
        verify_bundle(path, manifest.files_sha256)

    with pytest.raises(BundleError, match="does not exist"):
        verify_bundle(tmp_path / "nope", manifest.files_sha256)


def test_a_manifest_for_another_version_is_refused(bundle):
    path, manifest = bundle

    with pytest.raises(BundleError, match="expected '20301231-000000'"):
        verify_bundle(path, manifest.files_sha256, version="20301231-000000")


def test_a_manifest_whose_feature_names_differ_from_this_code_is_refused(bundle):
    path, manifest = bundle
    doc = json.loads((path / MANIFEST_NAME).read_text())
    doc["numeric_features"] = doc["numeric_features"][::-1]
    (path / MANIFEST_NAME).write_text(json.dumps(doc))

    with pytest.raises(BundleError, match="NUMERIC_FEATURES"):
        verify_bundle(path, manifest.files_sha256)


def test_nothing_to_verify_against_is_refused(bundle):
    path, _ = bundle

    with pytest.raises(BundleError, match="no artifacts_sha256"):
        verify_bundle(path, {})


def test_a_garbage_model_file_with_a_matching_checksum_is_refused_at_load(bundle, tmp_path):
    path, manifest = bundle
    broken = tmp_path / "broken"
    shutil.copytree(path, broken)
    (broken / "win_loss.txt").write_text("this is not a lightgbm model")
    files = {**manifest.files_sha256, "win_loss.txt": file_sha256(broken / "win_loss.txt")}
    doc = json.loads((broken / MANIFEST_NAME).read_text())
    doc["files_sha256"] = files
    (broken / MANIFEST_NAME).write_text(json.dumps(doc))

    with pytest.raises(BundleError, match="could not be loaded"):
        load_bundle(broken, files)
