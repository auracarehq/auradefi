# auradefi 0.1.1 — release-readiness spec

Self-contained handoff. Everything needed to take auradefi from "0.1.0 is
published and should not be used" to "0.1.1 is correct and installable".

---

## 1. Situation

**0.1.0 is published to PyPI and Docker Hub.** It passes 3,027 tests offline,
its release gate is green, and it runs clean in a network-isolated container.

Independent adversarial review then found **19 verified defects**, filed as
GitHub issues **#18–#36**. Five are security, four cause silent data loss or
silent empty results, and the rest produce wrong numbers, unhandled 500s, or
break any host-supplied implementation of a declared interface.

The last four (#33–#36) came from a *single-file* review of
`api/routes/auth.py` — a whole-repo sweep had already covered that file and
found two. Narrow scope concentrates attention: when the release work starts,
review the touched files individually rather than in one broad pass.

None of them fail a test. That is the point: every one of them is green today.

A separate set of issues, **#1–#17**, records gaps between `docs/SPEC.md` §3.2's
declared module layout and what actually shipped. **Those are roadmap, not
release blockers** — see §7.

---

## 2. Containment — do this before writing any code

**PyPI.** A version number can never be reused, even after deletion. The fix
release is therefore **0.1.1**, not a re-upload.

- Yank 0.1.0 (PyPI project page → Manage → Releases → Yank). Yanking (PEP 592)
  hides it from resolvers while keeping exact pins working, which is the right
  semantics for "installable but known-bad".
- Give a yank reason that names the problem: *"Security defects in token
  scope handling and key rotation; silent transaction loss in embed sync. Use
  0.1.1."*

**Docker Hub.** Tags are mutable, but overwriting `0.1.0` in place would make
an already-pulled image and a freshly-pulled one differ under the same name.
Instead:

- Repoint or remove `latest` so it does not serve the defective build.
- Leave `0.1.0` in place and note the defect in the repository description.

**GitHub.** Consider a security advisory covering #20, #25 and #30, since the
package was publicly installable. Optional, but it is the mechanism that
notifies anyone who did install it.

---

## 3. Definition of done for 0.1.1

All of the following, none assumed:

1. Every issue in §4 and §5 closed (#18–#36), each with a regression test that **fails
   against the unfixed code** (see §6).
2. `.venv/bin/pytest` green — the count will exceed 3,027; new tests only.
3. `bash scripts/release_check.sh` → PASSED.
4. `docker build --target test` + `docker run --rm --network none` → green.
5. All notebooks execute clean in the network-less container.
6. `pyproject.toml` version **and** `src/auradefi/__init__.py` `__version__`
   both read `0.1.1` — `release_check.sh` fails on a half-bump.
7. `CHANGELOG.md` has a `[0.1.1]` section listing every fixed issue by number.
8. `README.md` and `STATUS.md` state the gaps honestly (#17).

---

## 4. Release blockers — security

Fix these first. All three are exploitable by an ordinary caller.

### #20 — `scopes: []` mints a full-privilege token
`src/auradefi/api/routes/auth.py:114`

`body.scopes or key.scopes` treats an explicitly-empty scope list as
"omitted", so a request for a **zero**-privilege token returns one carrying
every scope the API key holds.

- **Fix:** `body.scopes if body.scopes is not None else key.scopes`.
- **Test:** posting `{"external_user_id": "u-1", "scopes": []}` yields a token
  whose `scopes` claim is empty, and that token is refused by any scoped route.

### #25 — `rotate()` revives a revoked key
`src/auradefi/tenancy/keys.py:128`

No revoked/expired guard, so rotating a revoked key id mints a **live** key
with the revoked key's project, environment and full scope set. Any bulk
rotation job silently re-privileges a key an operator revoked.

- **Fix:** refuse to rotate a revoked or expired key. Separately, `expires_at`
  must only ever be **shortened**, never extended (line 132).
- **Test:** issue → revoke → rotate raises; and rotate on a key with a shorter
  existing `expires_at` does not extend it.
- **Also in scope:** `revoke` and `rotate` look the key up in the global
  `self._keys` dict with **no project filter**. Add the tenant gate and a test
  that project A cannot revoke or rotate project B's key.

### #33 — `POST /auth/revoke` is a cross-tenant authenticity oracle
`src/auradefi/api/routes/auth.py:154`

`_signing_secret` resolves the secret from the token's **own unverified**
`project_id` claim, so a foreign token is verified under that project's real
secret. The response then distinguishes three cases an outsider must not be
able to tell apart: genuine-and-live → **404**, bad signature → **401
AuthError**, genuine-but-expired → **401 TokenExpiredError** (separable by
`error.type`).

An attacker who registers their own free project and issues their own
`users:admin` key can POST captured JWTs — from logs, `Referer` headers, crash
reports — and learn which ones are authentic and still live for projects they
hold **no credential for**, then replay only those. They cannot compute this
offline, since they do not know the victim's signing secret.

The module docstring claims another project's token is "indistinguishable from
a token that never existed". It is plainly distinguishable from an invalid one.

- **Fix:** make every failure path on this route indistinguishable — one
  status, one error type, one timing profile — regardless of whether the token
  was authentic, expired, foreign or forged.
- **Test:** all four cases return byte-identical responses.

### #34 — unauthenticated `RecursionError` becomes an unhandled 500
`src/auradefi/api/routes/auth.py:148` → `src/auradefi/api/deps.py:172-188`

`_peek_project_id` base64-decodes and `json.loads()`es a caller-supplied string
guarding only `(ValueError, UnicodeDecodeError)`. A ~26 KB token of ~10,000
nested arrays raises `RecursionError` — a `RuntimeError`, not caught — which
escapes into an **unformatted 500** instead of the pinned
`401 {"error": {"type": "AuthError"}}`.

Reachable with **no credentials at all** via `GET /users/me`, where
`require_user_token` peeks the raw `Authorization` header. Each request burns a
worker unwinding a 10,000-frame C recursion and leaks a stack trace into logs.

- **Fix:** bound the token length before decoding, and catch `RecursionError`
  (or use a depth-limited parse) so malformed input is always a 401.
- **Test:** the nested payload returns 401 with the pinned error body, on both
  `/auth/revoke` and `/users/me`.

### #30 — audit log records a caller-controlled IP
`src/auradefi/api/routes/auth.py:65`

`_client_ip` returns the first `X-Forwarded-For` hop verbatim — no trusted
proxy count, no allowlist, no marker. `AuditLog` is deliberately mutation-free,
so a forged attribution is permanent and indistinguishable from a real one.

- **Fix:** a configurable trusted-proxy hop count, defaulting to **0** (socket
  peer only). Record which source the value came from so a header-derived IP is
  never mistaken for a verified one.
- **Test:** with the default config, a request carrying `X-Forwarded-For` is
  audited with the socket peer, not the header.

---

## 5. Release blockers — correctness

### Wave A — identity and persistence (breaking derivations; do together)

These two change values that are already persisted. Update
`docs/DECISIONS.md` in the same change, and note in `CHANGELOG.md` that any
0.1.0 data is not portable to 0.1.1.

**#19 — library and API address different ledger tenants.**
`src/auradefi/embed/models.py:48`. The facade keys rows by
`sha256("embed|{external_user_id}")`; `GET /crypto/sync` keys them by
`sha256("{project_id}|{external_user_id}")`. Ingest with the library, read over
HTTP, and the client sees an account with **zero transactions, forever**, no
error on either side.
*Fix direction:* make the embed facade derive through
`tenancy.models.end_user_id` so there is one derivation. Keep the change inside
`embed/` so `api/routes/sync.py` stays owned by #32.
*Test:* ingest via the facade, read via the API app, get the rows back.

**#26 — a connection id drops `chain_id`.**
`src/auradefi/embed/models.py:54`. Only `(tenant_id, address)` is hashed, so the
same address can only ever be connected on **one** chain — and the
`ConflictError` names an id the caller already owns. Worse, `SyncEngine` keys
sync state by `(tenant_id, connection.id)`, so two chains would share one
cursor.
*Fix:* include `chain_id` in the hash input.
*Test:* the same address connects on `eip155:1` and `eip155:137` and yields two
distinct connections with independent cursors.

### Wave B — sync correctness

**#18 — backfill drops transactions and reports success.**
`src/auradefi/embed/sync.py:297`. The window restarts strictly *below* the
lowest block ingested, so when a full page ends inside a block the rest of that
block is never fetched — and `backfill_complete` still flips to `True`.
Permanent, unrecoverable, silent.
*Fix:* make the boundary inclusive of the boundary block and de-duplicate by
transaction id rather than treating `block_number` as a unique cursor.
*Test:* three transactions in one block with `page_size=2` — all three land,
and `backfill_complete` is only `True` when it is.

**#21 — `sync()` no-ops after a restart.**
`src/auradefi/embed/facade.py:167`. Connections are enumerated from the
in-process `self._tenants` list, not the injected `SyncStatePort`. A restarted
worker returns `SyncReport(no_op=True)` — success-shaped — while ingesting
nothing, forever.
*Fix:* enumerate from the port. `SyncStatePort` declares no tenant-enumeration
method, so it must grow one.
*Test:* store a connection, rebind a fresh `Auradefi` over the same state
object, and `sync()` must do work.

**#24 — an unseeded chain connects, then breaks the whole sync loop.**
`src/auradefi/embed/facade.py:324`. Only the CAIP-2 *shape* is validated, but
the decoder requires registry membership. Connecting an Arbitrum address
succeeds, then every `sync()` raises `UnknownChainError` forever — and because
the exception escapes `_run_sync`, **every other connection is starved too**.
*Fix:* check `ChainRegistry` membership at connect time, and isolate
per-connection failures so one bad row cannot stop the loop.
*Test:* both halves — connect is refused, and a failing connection does not
prevent siblings from syncing.

**#22 — an orphaned transaction can never be resurrected.**
`src/auradefi/ledger/reorg.py:61`. `plan_reorg` decides re-add by
`payload_equal`, which ignores the `removed` flag, so a transaction orphaned by
an earlier reorg and now back on-chain **unchanged** lands in neither
`remove_ids` nor `add` and stays `removed=True` forever.
*Fix:* include the stored `removed` flag in the re-add decision.
*Note:* `tests/contract/test_phase3_reorg.py::test_gate_resurrection_re_add_of_c`
passes only because its fixture changes `block_number` (106), routing through
the `changed` bucket. **Fix the fixture, do not weaken the test.**
`tests/ledger/test_reorg.py::test_bookkeeping_only_difference_is_not_readded`
pins the opposite behaviour while only exercising `last_modified_seq` — it needs
a `removed=True` case.

### Wave C — declared interfaces that lie

Both are the same class: the route requires more than `WebhookSink` promises,
so every host-supplied sink gets an unhandled 500 while the shipped store works
by accident.

**#27** — `api/routes/admin.py:203` calls `store.create_replay`, undeclared.
**#28** — `api/routes/admin.py:158` unpacks a 2-tuple from `register_endpoint`,
which the Protocol types as returning a single object.

*Fix:* widen `WebhookSink` in `src/auradefi/api/deps.py` to promise exactly what
the routes use, including the return shapes.
*Test:* one test binds a **minimal sink written only from the Protocol** and
drives every webhook route through it. That single test is what neither
existing test does, and it catches this whole class.

### Wave D — wrong numbers and brittle degradation

**#23 — non-USD price relabelled USD.** `src/auradefi/portfolio/holdings.py:120`
stamps `Money(..., "USD")` without reading `price.currency`. A EUR oracle price
yields a total off by the FX rate, labelled USD, with nothing in `unpriced`.
*Fix:* validate at the `Inquirer` boundary (its own documented contract says
every returned `Money` is USD) and carry `price.currency` through.

**#29 — `rounded_basis` flag discarded.** `src/auradefi/accounting/report.py`
lines 231, 241, 283-286, 287-289 all index `[0]` and drop `fraction_to_money`'s
`is_exact` bit. `docs/DECISIONS.md` pins that this boundary is *always flagged*.
`AssetPnL` / `PnLReport` / `TaxLot` carry no flags field at all — add one.

**#31 — one stale descriptor drops the whole staking slice.**
`src/auradefi/positions/adapters/tokens.py:178` indexes unguarded, so a
`KeyError` on the first iteration removes every Lido/Rocket Pool position from
`net_worth`.
*Fix:* match the Aave adapter — `.get(...)` + `continue`, so one unknown
descriptor costs one position.

**#32 — a 422 burns quota.** `src/auradefi/api/routes/sync.py:193` consumes
quota before `_resolved_limit` and the cursor decode run. A client with a
hard-coded bad limit drains its per-day window and then 429s **every other user
of the project**.
*Fix:* validate first, as `POST /batch/holdings` already does — its
`_checked_size` docstring states the rule.

**#35 — `POST /auth/token` 500s on wire-string scopes.**
`src/auradefi/api/routes/auth.py:113`. `{scope.value for scope in key.scopes}`
assumes `Scope` members, while the *very next line* defensively uses
`str(scope)`. `ApiKeyStore.issue` stores `frozenset(scopes)` with no coercion
and `has_scope` compares with `in`, which succeeds for plain strings because
`Scope` is a `StrEnum` — so a key rehydrated from JSON or SQL authenticates
everywhere else and then `AttributeError`s here into an unformatted 500. Token
minting is dead for that key while `GET /users` on it returns 200.
*Fix:* coerce at the store boundary, and make line 113 as tolerant as line 114.

**#36 — a refused mint costs the caller nothing.**
`src/auradefi/api/routes/auth.py:115`. The three sibling handlers call
`consume_quota` immediately after authentication; this one defers quota to
`mint_user_token`, and the `raise ScopeError` returns before reaching it. A
tenant can drive unlimited authenticated `POST /auth/token` requests — each one
walking and HMAC-comparing every stored key — that always request a scope the
key lacks, and decrement nothing.
*Fix:* consume quota after authentication, before the privilege check, as the
siblings do.

### Wave E — honesty

**#17** — `README.md`'s *What is not there* omits four absent things the spec
declares: the whole `jobs/` package, four `api/routes/` modules,
`project/plaid.py`+`native.py`, and `prices/historian.py`+`store.py`.
*Fix:* correct the section, and add a test that diffs the shipped tree against
`docs/SPEC.md` §3.2 so the docs cannot drift silently again.

---

## 6. Per-fix protocol — non-negotiable

Every one of these bugs is **green today**. A fix without a discriminating test
leaves the next regression invisible.

For each issue:

1. **Write the regression test first**, with a `# pins:` comment naming the
   behaviour it discriminates.
2. **Prove it fails against the current code.** The bug is the mutant — you do
   not have to construct one. A test that passes before the fix is testing
   something else.
3. Fix the source. Never edit a test to make it pass; if a test is wrong (as
   in #22), fix the **fixture** so it reaches the branch it claims to pin.
4. **Prove it fails again if you revert the fix.** This is the invariant that
   makes the test worth keeping.
5. Close the issue with the test name and the before/after output.

`loop.md` describes the agent loop that automates this if you want it;
`.claude/agents/` has the roles. Not required — the protocol above is the part
that matters.

---

## 7. Explicitly not in scope for 0.1.1

Issues **#1–#16** are gaps between the spec's declared layout and what shipped:
no concrete `ContractReader` (#1), no multicall (#2), missing source and oracle
modules (#3–#7), `decode/` is pipeline-only (#8), four route modules absent
(#9), `PATCH /users/me/external_id` (#10), no `jobs/` package (#11),
`project/` projections in `api/wire.py` (#12), in-memory-only stores (#13),
Postgres unexercised (#14), no live reconciliation (#15), ACB pool-vs-lots
(#16).

These are **roadmap**. Shipping 0.1.1 without them is correct — they are
documented limitations, not defects. #17 is the exception and **is** in scope,
because a README that understates its own gaps is a correctness problem.
