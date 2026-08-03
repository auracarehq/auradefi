"""Library configuration.

A frozen dataclass the host constructs directly, plus an environment loader
for convenience. No dotenv magic and no framework settings object — an
embedding host owns its own configuration story (SPEC §8).

Environment variables use the AURADEFI_ prefix. No test and no default
requires any of them to be set: the suite must pass with no API keys
(SPEC §13).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from auradefi.errors import ConfigError

_ENV_PREFIX = "AURADEFI_"


@dataclass(frozen=True, slots=True)
class Settings:
    etherscan_api_key: str | None = None
    helius_api_key: str | None = None
    http_timeout_s: float = 10.0
    sync_min_interval_s: int = 60

    def __post_init__(self) -> None:
        if self.http_timeout_s <= 0:
            raise ConfigError(f"http_timeout_s must be positive: {self.http_timeout_s!r}")
        if self.sync_min_interval_s < 0:
            raise ConfigError(
                f"sync_min_interval_s must be non-negative: {self.sync_min_interval_s!r}"
            )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        source = os.environ if env is None else env
        kwargs: dict[str, object] = {}
        for key, field in (
            ("ETHERSCAN_API_KEY", "etherscan_api_key"),
            ("HELIUS_API_KEY", "helius_api_key"),
        ):
            value = source.get(_ENV_PREFIX + key)
            if value:
                kwargs[field] = value
        for key, field, cast in (
            ("HTTP_TIMEOUT_S", "http_timeout_s", float),
            ("SYNC_MIN_INTERVAL_S", "sync_min_interval_s", int),
        ):
            raw = source.get(_ENV_PREFIX + key)
            if raw is not None:
                try:
                    kwargs[field] = cast(raw)
                except ValueError as exc:
                    raise ConfigError(
                        f"{_ENV_PREFIX}{key} must be a {cast.__name__}: {raw!r}"
                    ) from exc
        return cls(**kwargs)  # type: ignore[arg-type]
