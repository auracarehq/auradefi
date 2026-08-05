"""One object that satisfies BOTH source seams over Etherscan V2.

``Auradefi`` needs a source answering two questions — what does this
address hold NOW (``balances``, for holdings and pricing) and what is in
this block window (``fetch_txlist``, for history). Nothing shipped
satisfied both: ``EtherscanV2`` has ``balances`` only, so every host wrote
the same ~20 lines of window-aware paging before it could bind the facade,
and got refused at bind time until it did. That glue existed three times
in this repository — twice in tests, once in a published example — which
is two times too many for code a host cannot avoid writing.

So it ships here. ``balances`` delegates to ``EtherscanV2`` and
``fetch_txlist`` delegates to ``txfetch.fetch_page``: this module adds no
HTTP knowledge of its own, it only presents the two existing halves under
the shape the engine asks for.

Neither seam is imported. Both are ``runtime_checkable`` structural
Protocols living in ``portfolio`` and ``embed``, which the layer contract
forbids ``sources`` from importing (``tests/style/test_layering.py``) —
conformance is by shape, verified in this module's mirrored test.

One Etherscan V2 key covers every ``eip155:N`` chain; the chain travels in
the ``chainid`` param, derived from CAIP-2. NO retry and NO rate limiting,
here or anywhere else in the package: the free tier is 3 requests/second
and a burst surfaces as ``SourceError``, so a host syncing many
connections paces its own ticks with ``sync(budget=...)``.
"""

from __future__ import annotations

import httpx

from auradefi.chains.evm import chain_id_from_caip2
from auradefi.sources.evm.etherscan import BalanceRecord, EtherscanV2
from auradefi.sources.evm.txfetch import BASE_URL, fetch_page

DEFAULT_TIMEOUT_S = 10.0


class EtherscanSource:
    """Both source seams over one Etherscan V2 client.

    ``balances(chain_id, address)`` -> ``list[BalanceRecord]`` and
    ``fetch_txlist(chain_id, address, *, start_block, end_block, page,
    offset, sort)`` -> ``list[dict]`` of RAW rows for the decoder seam.

    The client is injected — this constructor performs no I/O and opens no
    connection. :meth:`from_key` is the convenience that builds one.
    """

    def __init__(
        self,
        client: httpx.Client,
        api_key: str | None = None,
        base_url: str = BASE_URL,
        page_size: int = 1000,
    ) -> None:
        """Bind the client and credentials. ZERO I/O happens here.

        ``api_key=None`` is servable: the ``apikey`` param is omitted and
        Etherscan's keyless tier applies. ``page_size`` is the balance
        source's token-discovery page size; the history seam is paged by
        the engine, which passes its own ``offset`` per call.
        """
        self._client = client
        self._api_key = api_key
        self._base_url = base_url
        self._balances = EtherscanV2(
            client, api_key=api_key, base_url=base_url, page_size=page_size
        )

    @classmethod
    def from_key(
        cls,
        api_key: str | None = None,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        base_url: str = BASE_URL,
        page_size: int = 1000,
    ) -> EtherscanSource:
        """Build a source owning its own ``httpx.Client``.

        The one place in this package that constructs a client from a
        credential, which is why it lives in ``sources`` — an I/O domain.
        A host that wants connection pooling, proxies, custom TLS or its
        own retry policy passes the client to ``__init__`` instead; this
        classmethod is a default, never a requirement.
        """
        return cls(
            httpx.Client(timeout=timeout_s),
            api_key=api_key,
            base_url=base_url,
            page_size=page_size,
        )

    @property
    def client(self) -> httpx.Client:
        """The client this source uses, for a caller that wants to share it.

        One client for chain data and prices is how a host should wire this
        — connection reuse, one timeout, one place to add a proxy — and
        every example in the documentation does exactly that. Exposed
        read-only so sharing does not mean reaching for a private
        attribute.
        """
        return self._client

    def balances(self, chain_id: str, address: str) -> list[BalanceRecord]:
        """What ``address`` holds on ``chain_id`` now (SPEC §6.1).

        Delegated verbatim to ``EtherscanV2.balances``: native coin plus
        every token the address has ever touched, deduplicated, with
        undecodable rows skipped rather than guessed at. Raises
        ``CaipParseError`` for a non-CAIP-2 chain before any request, and
        ``SourceError`` for anything Etherscan refuses.
        """
        return self._balances.balances(chain_id, address)

    def fetch_txlist(
        self,
        chain_id: str,
        address: str,
        *,
        start_block: int,
        end_block: int,
        page: int,
        offset: int,
        sort: str,
    ) -> list[dict]:
        """One page of raw history rows for exactly the window asked for.

        The engine owns the window and the budget: it picks
        ``start_block``/``end_block``/``page``/``sort``, and a page shorter
        than ``offset`` is how it learns the window drained. So this method
        widens nothing, retries nothing and pages nothing — one request,
        one answer.

        Rows are returned RAW (``list[dict]``) because parsing belongs to
        the decoder seam, which a host may replace. Raises ``SourceError``
        on any upstream refusal, which ``Auradefi.sync`` contains to this
        one connection's report row rather than losing the whole tick.
        """
        return fetch_page(
            self._client,
            chain_id=chain_id_from_caip2(chain_id),
            address=address,
            action="txlist",
            start_block=start_block,
            end_block=end_block,
            page=page,
            offset=offset,
            sort=sort,
            api_key=self._api_key,
            base_url=self._base_url,
        )
