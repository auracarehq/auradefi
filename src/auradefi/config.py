"""Library configuration.

A frozen dataclass the host constructs directly, plus an environment loader
for convenience. No dotenv magic and no framework settings object — an
embedding host owns its own configuration story (SPEC §8).

Environment variables use the AURADEFI_ prefix. No test and no default
requires any of them to be set: the suite must pass with no API keys
(SPEC §13).

Two fields exist for cross-surface agreement rather than for I/O:

``project_id`` is the project the *library* surface hashes its tenant ids
under. It defaults to ``"embed"``, which is the 0.1.0 value, so existing
library-ingested data stays addressable. A host that runs the library and
the HTTP API over one ledger sets this to its real project id, otherwise
``GET /crypto/sync`` derives a different ``end_user_id`` and reads an
empty account (RELEASE_0.1.1 §5 #19).

``trusted_proxy_hops`` is how many rightmost ``X-Forwarded-For`` hops the
deployment's own proxies contribute, and therefore how far back a
trustworthy client IP can be read from. It defaults to **0** — no proxy is
trusted, the socket peer is the only verified source — because an audit
row that records a caller-supplied IP is permanently wrong and cannot be
told from a real one (RELEASE_0.1.1 §4 #30).
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
    project_id: str = "embed"
    trusted_proxy_hops: int = 0

    def __post_init__(self) -> None:
        if self.http_timeout_s <= 0:
            raise ConfigError(f"http_timeout_s must be positive: {self.http_timeout_s!r}")
        if self.sync_min_interval_s < 0:
            raise ConfigError(
                f"sync_min_interval_s must be non-negative: {self.sync_min_interval_s!r}"
            )
        if not self.project_id:
            raise ConfigError("project_id must be a non-empty string")
        if self.trusted_proxy_hops < 0:
            raise ConfigError(
                f"trusted_proxy_hops must be non-negative: {self.trusted_proxy_hops!r}"
            )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        source = os.environ if env is None else env
        kwargs: dict[str, object] = {}
        for key, field in (
            ("ETHERSCAN_API_KEY", "etherscan_api_key"),
            ("HELIUS_API_KEY", "helius_api_key"),
            ("PROJECT_ID", "project_id"),
        ):
            value = source.get(_ENV_PREFIX + key)
            if value:
                kwargs[field] = value
        for key, field, cast in (
            ("HTTP_TIMEOUT_S", "http_timeout_s", float),
            ("SYNC_MIN_INTERVAL_S", "sync_min_interval_s", int),
            ("TRUSTED_PROXY_HOPS", "trusted_proxy_hops", int),
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
