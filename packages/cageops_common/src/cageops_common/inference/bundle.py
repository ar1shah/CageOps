"""A model bundle on disk: two LightGBM text files and a manifest, and how to trust one.

Layout (written by the trainer, read by the API):

    <MODEL_ARTIFACT_DIR>/<version>/win_loss.txt
                                  /method.txt
                                  /manifest.json

`load_bundle` refuses unless every check passes. It never falls back to another file: a model
that cannot be verified is not served.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np
from pydantic import BaseModel, ValidationError

from cageops_common.inference.features import (
    METHOD_FEATURE_NAMES,
    NUMERIC_FEATURES,
    WIN_FEATURE_NAMES,
    Vocabulary,
)
from cageops_common.inference.model import (
    METHOD_CLASSES,
    MethodModel,
    WinLossModel,
    load_booster,
    save_booster,
)

MANIFEST_NAME = "manifest.json"
WIN_LOSS_FILE = "win_loss.txt"
METHOD_FILE = "method.txt"


class BundleError(Exception):
    """The bundle cannot be trusted or loaded. The message says why and is safe to log."""


class VocabularyDoc(BaseModel):
    ordered: bool
    stance_matchups: list[str]
    weight_classes: list[str]


class ModelEntry(BaseModel):
    file: str
    feature_names: list[str]
    vocabulary: VocabularyDoc


class Manifest(BaseModel):
    version: str
    data_cutoff: str  # ISO date: the last event the model was fit on
    method_classes: list[str]
    win_loss: ModelEntry
    method: ModelEntry
    numeric_features: list[str]
    files_sha256: dict[str, str]


@dataclass(frozen=True)
class LoadedModel:
    """Everything a request needs, immutable: swapped as one reference (D-036)."""

    version: str
    win_loss: WinLossModel
    method: MethodModel
    manifest: Manifest

    @property
    def data_cutoff(self) -> date:
        return date.fromisoformat(self.manifest.data_cutoff)

    @property
    def weight_classes(self) -> tuple[str, ...]:
        return tuple(self.manifest.win_loss.vocabulary.weight_classes)


def file_sha256(path: Path) -> str:
    """sha256 of the file's bytes, which is how the trainer recorded it."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_manifest(bundle_dir: Path) -> Manifest:
    path = bundle_dir / MANIFEST_NAME
    try:
        return Manifest.model_validate(json.loads(path.read_text()))
    except FileNotFoundError as exc:
        raise BundleError(f"no {MANIFEST_NAME} in {bundle_dir}") from exc
    except (json.JSONDecodeError, ValidationError) as exc:
        raise BundleError(f"{MANIFEST_NAME} in {bundle_dir} is not a valid manifest") from exc


def write_bundle(
    bundle_dir: Path,
    *,
    version: str,
    data_cutoff: date,
    win_model: WinLossModel,
    method_model: MethodModel,
) -> Manifest:
    """Write both model files and the manifest into an existing directory.

    The returned manifest's `files_sha256` is what the registry row stores as
    `config.artifacts_sha256`, so the two cannot disagree."""
    files = {
        WIN_LOSS_FILE: save_booster(win_model.booster, bundle_dir / WIN_LOSS_FILE),
        METHOD_FILE: save_booster(method_model.booster, bundle_dir / METHOD_FILE),
    }
    manifest = Manifest(
        version=version,
        data_cutoff=data_cutoff.isoformat(),
        method_classes=list(METHOD_CLASSES),
        win_loss=ModelEntry(
            file=WIN_LOSS_FILE,
            feature_names=list(WIN_FEATURE_NAMES),
            vocabulary=VocabularyDoc(**win_model.vocab.as_dict()),  # type: ignore[arg-type]
        ),
        method=ModelEntry(
            file=METHOD_FILE,
            feature_names=list(METHOD_FEATURE_NAMES),
            vocabulary=VocabularyDoc(**method_model.vocab.as_dict()),  # type: ignore[arg-type]
        ),
        numeric_features=list(NUMERIC_FEATURES),
        files_sha256=files,
    )
    (bundle_dir / MANIFEST_NAME).write_text(
        json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True)
    )
    return manifest


def verify_bundle(
    bundle_dir: Path, expected_sha256: Mapping[str, str], *, version: str | None = None
) -> Manifest:
    """Every check that does not need LightGBM.

    `expected_sha256` comes from the registry row (`config.artifacts_sha256`), which is the
    authority: the manifest is cross-checked against it, never trusted on its own."""
    if not bundle_dir.is_dir():
        raise BundleError(f"bundle directory {bundle_dir} does not exist")
    if not expected_sha256:
        raise BundleError("the registry row has no artifacts_sha256 to verify against")
    manifest = read_manifest(bundle_dir)
    if version is not None and manifest.version != version:
        raise BundleError(f"manifest is for version {manifest.version!r}, expected {version!r}")
    if manifest.numeric_features != list(NUMERIC_FEATURES):
        raise BundleError("manifest numeric_features differ from this code's NUMERIC_FEATURES")
    if manifest.win_loss.feature_names != list(WIN_FEATURE_NAMES):
        raise BundleError("manifest win_loss feature names differ from this code's")
    if manifest.method.feature_names != list(METHOD_FEATURE_NAMES):
        raise BundleError("manifest method feature names differ from this code's")
    if manifest.method_classes != list(METHOD_CLASSES):
        raise BundleError("manifest method classes differ from this code's")
    if dict(manifest.files_sha256) != dict(expected_sha256):
        raise BundleError("manifest files_sha256 does not match the registry's artifacts_sha256")
    for name in (manifest.win_loss.file, manifest.method.file):
        if name not in expected_sha256:
            raise BundleError(f"{name} is not listed in the registry's artifacts_sha256")
    for name, expected in expected_sha256.items():
        path = bundle_dir / name
        if not path.is_file():
            raise BundleError(f"artifact {name} is missing")
        if file_sha256(path) != expected:
            raise BundleError(f"artifact {name} does not match its recorded sha256")
    return manifest


def load_bundle(
    bundle_dir: Path, expected_sha256: Mapping[str, str], *, version: str | None = None
) -> LoadedModel:
    """Verify, then load both boosters, then smoke-predict one all-missing row."""
    manifest = verify_bundle(bundle_dir, expected_sha256, version=version)
    try:
        win = WinLossModel(
            load_booster(bundle_dir / manifest.win_loss.file),
            Vocabulary.from_dict(manifest.win_loss.vocabulary.model_dump()),
        )
        method = MethodModel(
            load_booster(bundle_dir / manifest.method.file),
            Vocabulary.from_dict(manifest.method.vocabulary.model_dump()),
        )
    except Exception as exc:  # LightGBM raises LightGBMError, which has no useful base class
        raise BundleError(f"a model file could not be loaded: {exc}") from exc

    n = len(NUMERIC_FEATURES)
    blank = np.full((1, n), np.nan)
    try:
        p = win.predict(blank, blank, ["unknown"], ["unknown"], [None])
        m = method.predict(blank, blank, ["unknown"], ["unknown"], [None])
    except Exception as exc:
        raise BundleError(f"the smoke prediction failed: {exc}") from exc
    if p.shape != (1,) or m.shape != (1, len(METHOD_CLASSES)):
        raise BundleError(f"the smoke prediction had shapes {p.shape} and {m.shape}")
    if not (np.isfinite(p).all() and np.isfinite(m).all()):
        raise BundleError("the smoke prediction was not finite")
    return LoadedModel(version=manifest.version, win_loss=win, method=method, manifest=manifest)
