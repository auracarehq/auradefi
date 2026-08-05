"""Foundation: Settings: frozen, env-loadable, never required (SPEC §13)."""

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
    assert settings.project_id == "embed"
    assert settings.trusted_proxy_hops == 0


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


def test_negative_sync_interval_rejected():
    # pins: the guard exists and is reached. Without a test executing it,
    #       deleting the branch leaves the suite green and a negative
    #       interval becomes a permanently-due sync.
    with pytest.raises(ConfigError):
        Settings(sync_min_interval_s=-1)


def test_project_id_defaults_to_the_0_1_0_library_value():
    # pins: the default stays "embed" so library data ingested under 0.1.0
    #       remains addressable after the field is introduced
    assert Settings().project_id == "embed"
    assert Settings.from_env(env={"AURADEFI_PROJECT_ID": "proj_abc"}).project_id == (
        "proj_abc"
    )


def test_empty_project_id_rejected():
    with pytest.raises(ConfigError):
        Settings(project_id="")


def test_trusted_proxy_hops_defaults_to_zero_and_reads_from_env():
    # pins: the default trusts NO proxy: a header-derived client IP is
    #       never the audited source unless the deployment opts in
    assert Settings().trusted_proxy_hops == 0
    settings = Settings.from_env(env={"AURADEFI_TRUSTED_PROXY_HOPS": "2"})
    assert settings.trusted_proxy_hops == 2


def test_negative_trusted_proxy_hops_rejected():
    with pytest.raises(ConfigError):
        Settings(trusted_proxy_hops=-1)


def test_bad_trusted_proxy_hops_raises_config_error():
    with pytest.raises(ConfigError):
        Settings.from_env(env={"AURADEFI_TRUSTED_PROXY_HOPS": "many"})


def test_settings_is_frozen():
    settings = Settings()
    with pytest.raises(dataclasses.FrozenInstanceError):
        settings.http_timeout_s = 99  # type: ignore[misc]
