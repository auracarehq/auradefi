# Decisions

Settled questions, so nobody relitigates them. SPEC §14 tracks the ones
still open.

| # | Decision | Why | When |
|---|---|---|---|
| 1 | **Package name: `auradefi`** (dist and import) | Owner's choice 2026-08-02. `conduit` is taken on PyPI (power-engineering package, 2019); `chainconduit`/`cryptoconduit`/`conduit-defi` were free but rejected. Matches the GitHub repo `auracarehq/auradefi`. | 2026-08-02 |
| 2 | Licence Apache-2.0 | SPEC §1.1 — patent grant, maximises adapter contributions; BUSL killed Zapper's ecosystem. | 2026-08-02 |
| 3 | Build backend hatchling | src layout without MANIFEST ceremony; ships `py.typed` cleanly. | 2026-08-02 |
| 4 | pytest `--import-mode=importlib`, **no `tests/__init__.py`** | The mirror rule produces duplicate test basenames (`tests/chains/test_registry.py` vs `tests/assets/test_registry.py`) which collide fatally under prepend mode. | 2026-08-02 |
| 5 | Domain `__init__.py` are docstring-only; no re-exports | One domain's broken import can never poison another's test run; concurrent agents never edit a shared `__init__`. Enforced by `tests/style/test_structure.py`. | 2026-08-02 |
| 6 | Offline guarantee is an autouse socket-block fixture | SPEC §13 "no API keys" enforced as a hard failure, not a timeout. | 2026-08-02 |
| 7 | Cassette harness ships as public `auradefi.testing.cassettes` | Hosts embedding the library get the same offline-testing story we use ourselves. | 2026-08-02 |
| 8 | Exceptions live in `auradefi/errors.py` only | Convention drift between concurrent agents is a red test (`tests/test_errors.py`), not a debate. | 2026-08-02 |
| 9 | httpx is the only HTTP client, runtime dependency | One client to cassette-mock; layering gate confines it to I/O domains. | 2026-08-02 |
| 10 | Version 0.1.0, Development Status :: 3 - Alpha; artifacts built but **never uploaded autonomously** | Publishing needs the owner's PyPI credentials and judgement. `docs/RELEASING.md` has the exact commands. | 2026-08-02 |

## Pinned algorithms (public stability guarantees)

These are wire-format contracts (SPEC rule #3). Changing any of them is a
breaking change to persisted data, guarded by golden vectors in tests.

- **Quantity wire form**: `{"raw": "<decimal int string>", "decimals": <int>, "numeric": "<exact decimal string>", "float": <lossy float>}`. `raw` is a JSON **string**. `numeric` never uses scientific notation.
- **Asset id**: `"ast_" + sha256("\n".join(sorted(deduplicated(canonical_caip19s)))).hexdigest()[:16]` — the hash runs over the sorted, deduplicated canonical CAIP-19 set; empty input is rejected. Canonical CAIP-19 lowercases EVM addresses; Solana references keep base58 case.
- **Transaction id**: `"txn_" + sha256(f"{chain_id}|{tx_hash}|{account_id}").hexdigest()[:16]` (SPEC §4.4: hash over chain, tx hash, account).
- **Cursor token**: opaque to callers; internally a backend-monotonic last-modified sequence serialised as `f"{seq:020d}"` so lexicographic order equals numeric order.
- **JWT wire form** (tenancy): header exactly `{"alg":"HS256","typ":"JWT"}`; each segment is base64url **without padding** over `json.dumps(obj, separators=(",", ":"), sort_keys=True).encode("utf-8")`; signature = HMAC-SHA256(`secret.encode("utf-8")`, `header_b64 + "." + payload_b64`); claims are exactly `{exp, external_user_id, iat, jti, project_id, scopes}`; `iat`/`exp` are **ms-epoch ints** (deliberate deviation from RFC 7519 NumericDate seconds — SPEC §4.4 "ms epoch, everywhere, always"; these tokens are consumed by our own verify path); `scopes` is a sorted de-duplicated list; `exp` is exclusive (`now_ms >= exp` is expired). Rejection order: malformed → bad signature → expired → revoked, signature before expiry.
- **API key format**: plaintext `f"adk_{env}_{body}"`, env ∈ {`live`,`test`} (both 4 chars), body = 48 lowercase hex chars (`secrets.token_hex(24)`), total length 57; stored prefix = `plaintext[:17]`; at rest only `sha256(plaintext.encode()).hexdigest()`; compare via `hmac.compare_digest`; plaintext returned exactly once at issue/rotate.
- **Deterministic tenancy ids**: `end_user_id = "usr_" + sha256(f"{project_id}|{external_user_id}")[:16]`; `connection_id = "conn_" + sha256(f"{project_id}|{end_user_id}|{kind}|{normalized_descriptor}")[:16]`; descriptor normalization = strip, then lowercase iff kind == address and startswith "0x". Random ids: `org_`/`proj_`/`key_` + `token_hex(8)`, jti = `token_hex(16)`, project `signing_secret` = `token_hex(32)` (the per-project isolation root).
- **external_user_id invariant**: `re.fullmatch(r"[A-Za-z0-9._:-]{1,128}")` — charset excludes `@`, so email-shaped input is a ValidationError (SPEC §7.2: it is a bearer-equivalent secret and an email is guessable).
- **Audit record shape**: `{seq (per-project, from 1), event: "token.minted", project_id, external_user_id, key_id, ip, at_ms}`; append-only, no delete/update/clear.
- **Quota windows**: second = `now_ms // 1000`; day = `now_ms // 86_400_000` (UTC); month = UTC calendar `(year, month)`; `reset_at_ms` = start of the next window; a rejected hit consumes nothing.
- **TxType derivation** (decode): over `parts[]` only — fees are siblings, structurally excluded. Empty → `interaction`; all directions `in` → `receive`; all `out` → `send`; all `self` → `self`; any mixture → `trade`. `Transaction.type` is a computed property, never a stored field (SPEC §4.4: derived, never ground truth).
- **Act id**: `f"act_{n}"`, n = zero-based position in `acts[]`. Phase 3 emits exactly one act per transaction (`act_0`); every part/fee back-references it. Multi-act decomposition (multicalls, 4337 bundles) arrives with protocol decoders.
- **decoder_version**: module constant `DECODER_VERSION = 1` in `decode/pipeline.py`, stamped identically on `Transaction.decoder_version` and `DataQuality.decoder_version` (ValidationError if they differ); bump whenever identical input would decode differently (rule #7).
- **Gas fee** (decode): `fee_wei = gas_used * gas_price_wei` from the hash's normal row, else its first tokentx row; asset = the chain's native CAIP-19; `borne_by` = `self` iff the gas row's `from` == account else `counterparty` (inbound transfers keep the fee visible but summation skips it — Vezgo's failure inverted); fees never appear in `parts[]`; failed transactions keep the fee and emit zero parts.
- **Decode timestamps**: Etherscan `timeStamp` seconds × 1000 → ms epoch; `initiated_at == confirmed_at` for mined rows.
- **Part.asset_id**: a canonical CAIP-19 string (native = `Chain.native_caip19`; ERC-20 = `f"{caip2}/erc20:{contract_lowercase}"`), never the `ast_` registry id.
- **Duplication waiver**: `decode.models.Direction` and `decode.models.transaction_id` are value-identical duplicates of `ledger.models` — the layer contract forbids decode→ledger imports. Golden vectors in `tests/ledger/test_bridge.py` pin both to the same bytes, so drift is a red test, not a debate. Same waiver: `positions.models.MetaType` duplicates `decode.models.MetaType` (positions→decode forbidden); both pin the same seven SPEC §4.3 (name, value) literals in their own test trees.
- **Position id**: `"pos_" + sha256(f"{adapter_id}|{chain_id}|{contract_lower}|{discriminator}").hexdigest()[:16]`, discriminator `""` unless sub-addressed (Uniswap V3 uses the NFT token_id decimal string). **Group id**: `"grp_" + sha256(f"{adapter_id}|{chain_id}|{group_key}").hexdigest()[:16]`, group_key = the risk-unit contract lowercase (V2 pair, V3 pool, Aave Pool). 0x addresses lowercased.
- **Position sign convention** (SPEC §4.3): an underlying's value is negative iff `meta_type == BORROWED` (unit price stays positive); `gross_assets` = exact sum of non-negative values, `total_debt` = exact sum of |negative values| (both ≥ 0), `net_worth = gross_assets - total_debt` == the naive signed sum; all Money USD.
- **Drill rounding = NONE**: value = `quantity.as_decimal() × price.amount` via context-free coefficient multiplication (sign XOR, integer coefficient product, exponents added); drill is pure — raw balances persist and re-drill against fresh prices without an RPC (SPEC §5.3).
- **Uniswap V2 pro-rata**: `underlying_raw_i = lp_raw * reserve_i_raw // total_supply_raw` (integer floor, burn semantics).
- **Uniswap V3**: sqrt ratios via the canonical TickMath integer algorithm — pinned vectors tick 0 → 79228162514264337593543950336, −887272 → 4295128739, 887272 → 1461446703485210103287273052203988822378723970342; amounts `amount0 = ((L << 96) * (sqrtB - sqrtP) // sqrtB) // sqrtP`, `amount1 = L * (sqrtP - sqrtA) // 2**96`, standard single-sided branches out of range; `in_range = tick_lower <= tick < tick_upper`; unclaimed fees = tokensOwed0/1 verbatim as CLAIMABLE underlyings.
- **Receipt-token redemption** (SPEC §4.3): `underlying_raw = share_raw * rate_raw // 10**18`, rate an 18-decimal fixed point, identity `10**18` for rebasing 1:1 receipts (stETH).
- **Aave scaling**: `health_factor = Quantity(hf_raw, 18).as_decimal()`; `ltv = Quantity(ltv_bp, 4).as_decimal()`.
- **SyntheticHolding projection** (SPEC §6.3): one holding per valued underlying; quantity = signed Decimal (negated iff BORROWED — Plaid negative-quantity extension, consistent with tax_lots `position_type: SHORT`); institution_price positive; `institution_value = exact_mul(signed_quantity, price)`; invariant: `sum(institution_value) == net_worth` exactly (Decimal).
