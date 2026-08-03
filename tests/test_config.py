"""Foundation: Settings — frozen, env-loadable, never required (SPEC §13)."""

from __future__ import annotations

import dataclasses

import pytest

from auradefi.config import Settings
from auradefi.errors import ConfigError


def test_defaults_need_no_environment():
    settings = Settings.from_env(env={})
    assert settings.etherscan_api_key is None
    assert settings.helius_api_key is None
    assert settings.http_timeout_s == 10.0
    assert settings.sync_min_interval_s == 60


def test_from_env_reads_prefixed_variables():
    settings = Settings.from_env(
        env={
            "AURADEFI_ETHERSCAN_API_KEY": "key123",
            "AURADEFI_HTTP_TIMEOUT_S": "2.5",
            "AURADEFI_SYNC_MIN_INTERVAL_S": "300",
        }
    )
    assert settings.etherscan_api_key == "key123"
    assert settings.http_timeout_s == 2.5
    assert settings.sync_min_interval_s == 300


def test_unprefixed_variables_are_ignored():
    settings = Settings.from_env(env={"ETHERSCAN_API_KEY": "leaky"})
    assert settings.etherscan_api_key is None


def test_bad_numeric_value_raises_config_error():
    with pytest.raises(ConfigError):
        Settings.from_env(env={"AURADEFI_HTTP_TIMEOUT_S": "fast"})


def test_non_positive_timeout_rejected():
    with pytest.raises(ConfigError):
        Settings(http_timeout_s=0)


def test_settings_is_frozen():
    settings = Settings()
    with pytest.raises(dataclasses.FrozenInstanceError):
        settings.http_timeout_s = 99  # type: ignore[misc]
