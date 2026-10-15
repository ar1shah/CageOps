"""Trainer settings, read from environment variables (see .env.example)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class TrainerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Where trained model bundles are written, one directory per version. Gitignored: artifacts
    # are reproducible from the registry row, and never belong in git.
    model_artifact_dir: Path = Path("data/models")


@lru_cache
def get_trainer_settings() -> TrainerSettings:
    return TrainerSettings()
