import pytest

from cageops_worker.ingest.config import (
    IngestSettings,
    UnsupportedPlatform,
    ensure_supported_platform,
)


def test_defaults():
    settings = IngestSettings(_env_file=None)

    assert settings.ingest_max_retries == 5
    assert (settings.ingest_retry_base_s, settings.ingest_retry_cap_s) == (30, 900)
    assert settings.ingest_job_timeout_s == 300
    assert settings.ingest_claim_ttl_s == 7200


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("INGEST_RETRY_BASE_S", "0")
    monkeypatch.setenv("INGEST_CLAIM_TTL_S", "43200")

    settings = IngestSettings(_env_file=None)

    assert (settings.ingest_retry_base_s, settings.ingest_claim_ttl_s) == (0, 43200)


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_linux_and_macos_are_supported(platform):
    ensure_supported_platform(platform)


def test_windows_gets_a_clear_message_instead_of_failing_on_the_first_job():
    with pytest.raises(UnsupportedPlatform, match="WSL2 or Docker"):
        ensure_supported_platform("win32")


def test_the_current_machine_is_supported():
    ensure_supported_platform()  # the dev box (WSL2) and CI (Ubuntu) both pass
