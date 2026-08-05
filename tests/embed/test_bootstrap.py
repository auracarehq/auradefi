"""Default port sets — the promise that `pip install` plus five lines works.

`Auradefi.sandbox()` and `.from_env()` exist so that binding ports is a
choice rather than an entry fee. That promise is only kept if:

1. **sandbox works with nothing configured** — no env vars, no keys, no
   network. The autouse socket guard makes the last part real;
2. **every port stays overridable.** The whole point is that "bring your
   own database" is one keyword, not a fork of the wiring;
3. **the defaults are honest about durability.** A default that silently
   loses a host's data would be worse than no default;
4. **the sandbox numbers are constants**, because they are a recording.
   The documentation quotes them, so they are pinned here.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from auradefi.clock import FrozenClock
from auradefi.config import Settings
from auradefi.embed import bootstrap
from auradefi.embed.facade import Auradefi
from auradefi.errors import ValidationError
from auradefi.ledger.backends.memory import MemoryLedger
from auradefi.money.fiat import Money
from auradefi.sources import sandbox as recording

SANDBOX_TOTAL = Money(Decimal("5025"), "USD")


class TestSandboxNeedsNothing:
    def test_five_lines_with_no_configuration(self, monkeypatch):
        """The published quickstart, verbatim, with a hostile environment."""
        for name in ("AURADEFI_ETHERSCAN_API_KEY", "AURADEFI_HTTP_TIMEOUT_S",
                     "AURADEFI_PROJECT_ID"):
            monkeypatch.delenv(name, raising=False)

        aura = Auradefi.sandbox()
        (report,) = aura.holdings()

        assert report.total_value == SANDBOX_TOTAL
        assert [holding.symbol for holding in report.holdings] == ["ETH", "USDC"]
        assert report.unpriced == (), "the recording must cover the price leg"

    def test_arrives_connected_so_holdings_is_not_empty(self):
        aura = Auradefi.sandbox()
        (connection,) = aura.user(recording.SANDBOX_USER).connections()
        assert connection.chain_id == recording.SANDBOX_CHAIN
        assert connection.address == recording.SANDBOX_ADDRESS

    def test_connect_false_leaves_it_to_the_caller(self):
        aura = Auradefi.sandbox(connect=False)
        assert aura.holdings() == ()
        # …and connecting by hand still works, on the recorded probe.
        aura.user("someone").connect_address(
            recording.SANDBOX_CHAIN, recording.SANDBOX_ADDRESS
        )
        assert len(aura.holdings()) == 1

    def test_the_clock_is_frozen_at_the_recorded_instant(self):
        (report,) = Auradefi.sandbox().holdings()
        assert report.as_of_ms == recording.SANDBOX_NOW_MS

    def test_one_generous_tick_ingests_the_whole_recorded_history(self):
        aura = Auradefi.sandbox()

        report = aura.sync(budget=10)

        assert report.no_op is False
        assert (report.pages_fetched, report.transactions_ingested) == (5, 7)
        assert [row.backfill_complete for row in report.connections] == [True]

    def test_a_second_tick_in_the_same_instant_is_throttled(self):
        """The frozen clock makes the throttle visible instead of theoretical."""
        aura = Auradefi.sandbox()
        aura.sync(budget=10)

        assert aura.sync(budget=10).no_op is True

    def test_a_small_budget_resumes_across_ticks(self):
        """What a host's scheduler actually does: spend a little, come back."""
        clock = FrozenClock(recording.SANDBOX_NOW_MS)
        aura = Auradefi.sandbox(clock=clock)

        ingested = []
        for _ in range(4):
            ingested.append(aura.sync(budget=2).transactions_ingested)
            clock.advance(60_000)

        assert ingested == [3, 2, 2, 0]
        assert sum(ingested) == 7, "the recording holds seven transactions"


class TestOverrides:
    def test_a_single_port_can_be_replaced(self):
        mine = MemoryLedger()
        aura = Auradefi.sandbox(ledger=mine)
        aura.sync(budget=2)
        # Proof it is MY store being written, not a default one.
        assert mine.sync(aura.user(recording.SANDBOX_USER).tenant_id).events

    def test_overrides_win_over_defaults(self):
        clock = FrozenClock(1_700_000_000_000)
        (report,) = Auradefi.sandbox(clock=clock).holdings()
        assert report.as_of_ms == 1_700_000_000_000

    def test_ports_map_is_a_plain_kwargs_mapping(self):
        """The override mechanism is a dict update, not a special case."""
        ports = bootstrap.sandbox_ports()
        assert {"ledger", "source", "prices", "clock", "settings",
                "sync_state", "sync_page_size"} <= set(ports)
        assert Auradefi(**{**ports, "settings": Settings(project_id="mine")})


class TestEnvPorts:
    def test_reads_the_prefixed_key_and_timeout(self, monkeypatch):
        monkeypatch.setenv("AURADEFI_ETHERSCAN_API_KEY", "KEY123")
        monkeypatch.setenv("AURADEFI_HTTP_TIMEOUT_S", "4.5")

        ports = bootstrap.env_ports()

        assert ports["settings"].etherscan_api_key == "KEY123"
        assert ports["source"].client.timeout.read == 4.5

    def test_an_unprefixed_key_is_ignored_as_settings_documents(self, monkeypatch):
        monkeypatch.delenv("AURADEFI_ETHERSCAN_API_KEY", raising=False)
        monkeypatch.setenv("ETHERSCAN_API_KEY", "leaky")

        assert bootstrap.env_ports()["settings"].etherscan_api_key is None

    def test_works_with_no_key_at_all(self, monkeypatch):
        monkeypatch.delenv("AURADEFI_ETHERSCAN_API_KEY", raising=False)
        ports = bootstrap.env_ports()
        assert ports["settings"].etherscan_api_key is None
        assert Auradefi(**ports)          # binds; the keyless tier applies

    def test_storage_defaults_to_memory_and_says_so(self):
        ports = bootstrap.env_ports(Settings())
        assert isinstance(ports["ledger"], MemoryLedger)
        assert "NOT durable" in Auradefi.from_env.__doc__

    def test_chain_data_and_prices_share_one_client(self):
        """One connection pool, one timeout — as the docs teach a host to do."""
        ports = bootstrap.env_ports(Settings(http_timeout_s=7.0))
        oracle_client = ports["prices"]._oracles[0]._client
        assert oracle_client is ports["source"].client

    def test_explicit_settings_bypass_the_environment(self, monkeypatch):
        monkeypatch.setenv("AURADEFI_ETHERSCAN_API_KEY", "FROM-ENV")
        ports = bootstrap.env_ports(Settings(etherscan_api_key="EXPLICIT"))
        assert ports["settings"].etherscan_api_key == "EXPLICIT"

    def test_a_bad_numeric_env_var_fails_at_config_not_at_first_request(
        self, monkeypatch
    ):
        monkeypatch.setenv("AURADEFI_HTTP_TIMEOUT_S", "soon")
        from auradefi.errors import ConfigError

        with pytest.raises(ConfigError, match="AURADEFI_HTTP_TIMEOUT_S"):
            bootstrap.env_ports()


class TestBindTimeContract:
    def test_a_broken_override_is_still_refused_at_bind_time(self):
        """Defaults must not weaken the seam check a host relies on."""
        with pytest.raises(ValidationError, match="balances"):
            Auradefi.sandbox(source=object())
