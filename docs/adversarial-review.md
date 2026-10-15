# Adversarial Review: MultiSourcePriceGuard

Reviewer posture: every caller is adversarial. The owner, any keeper that can
call `submit_deterministic_quote`, the validators' inputs, the external pages
fetched by `fetch_and_verify_live_price`, and the LLM that extracts prices from
those pages are all treated as hostile or unreliable. This note records what was
checked against the actual code in `contracts/multi_source_price_guard.py` and
the direct test that proves each result. Every finding below is FIXED or
GUARDED with a named test. There are no open findings.

## 1. Feed state machine

| Status | Who may act | Allowed transitions | Effect on consumed price (`latest_price`) |
| --- | --- | --- | --- |
| Active, healthy | authorized keeper or owner (`submit_deterministic_quote`), any caller (`fetch_and_verify_live_price`) | ACCEPTED stays healthy; spread or jump breach trips the circuit | only an ACCEPTED round writes `latest_price` |
| Circuit broken (spread or jump) | owner via `reset_circuit_breaker` | reset returns to healthy | frozen; every update path reverts while broken |
| Inactive (`active = false`) | owner via `set_feed_active` | `set_feed_active(true)` returns to active | frozen while inactive; both update paths revert |

`register_feed`, `set_keeper`, `set_feed_active`, and `reset_circuit_breaker` are
owner gated. `submit_deterministic_quote` is gated to the owner or an address in
`authorized_keepers`. `fetch_and_verify_live_price` is callable by anyone because
its output is still forced through the same deterministic guard and through
comparative consensus before any state is written, so an untrusted caller cannot
push a price that violates the invariants.

## 2. Liveness and stuck state

The circuit breaker and the inactive flag both intentionally freeze consumption.
Each has an owner recovery path (`reset_circuit_breaker` and `set_feed_active`),
so no feed becomes permanently unusable. The primitive holds no funds; the asset
it protects is the price consumers read.

- Proven by `test_reset_circuit_breaker_by_owner_recovers`.
- Proven by `test_update_blocked_while_circuit_broken`.
- Proven by `test_set_feed_active_blocks_and_restores`.

## 3. Races between competing transitions

Writes serialize on chain; there is no interleaving inside one transaction. Both
`reset_circuit_breaker` and `set_feed_active` re-read the stored `FeedConfig` and
write it back, and `_apply_quotes` re-evaluates `active`, `circuit_broken`, the
spread, and the jump from the raw quotes before touching `latest_price`. A stale
read cannot bypass the guard, and a grant or reset followed by a quote in the same
block still runs the full guard.

- Proven by `test_set_keeper_grant_and_revoke` (revoke takes effect immediately).
- Proven by `test_set_feed_active_blocks_and_restores`.

## 4. Accounting and safety invariant

Invariant: `latest_price[feed_id]` only ever equals the midpoint of a quote pair
that passed both the spread and the jump guard while the feed was active and
healthy. A tripped or inactive round must never move `latest_price`.

Enforced in `_apply_quotes`: the write `latest_price[feed_id] = mid` runs only on
the ACCEPTED branch, after both guards pass. Every trip branch records the round
and returns before touching `latest_price`.

- Proven by `test_spread_breach_trips_and_does_not_poison_price`.
- Proven by `test_temporal_jump_breach_trips`.
- Proven by `test_fetch_outlier_trips_spread_guard`.

## 5. Input validation at every entry point

- `register_feed`: `symbol` must be 1..32 chars, URLs must be absolute http(s),
  8..512 chars, with no whitespace, tab, newline, or control byte, primary must
  differ from secondary, and both thresholds are bounded to `1..5000` (floor and
  ceiling, reject rather than clamp).
- `submit_deterministic_quote`: caller must be the owner or an authorized keeper;
  `note` is capped at 256 chars; both quotes must be positive.
- `set_keeper`: owner only; the address argument must be a 42-char hex value,
  otherwise it reverts before any state write.
- `set_feed_active` and `reset_circuit_breaker`: owner only, unknown feed reverts.

Tests: `test_register_feed_empty_symbol_reverts`, `test_register_symbol_too_long_reverts`,
`test_register_url_too_long_reverts`, `test_register_url_too_short_reverts`,
`test_register_url_with_whitespace_reverts`, `test_register_feed_bad_url_scheme_reverts`,
`test_register_feed_identical_urls_reverts`, `test_register_feed_spread_out_of_range_reverts`,
`test_register_feed_jump_out_of_range_reverts`, `test_submit_note_too_long_reverts`,
`test_non_positive_quote_reverts`, `test_set_keeper_invalid_address_reverts`,
`test_submit_unknown_feed_reverts`, `test_reset_unknown_feed_reverts`,
`test_get_round_unknown_reverts`.

## 6. Access control and griefing

- `submit_deterministic_quote` is closed to non-keepers, removing the griefing
  vector where any address could spam quotes or force a breaker trip. The owner is
  auto-registered as a keeper in `__init__`, and keepers can be granted or revoked
  at runtime.
- `register_feed`, `set_keeper`, `set_feed_active`, and `reset_circuit_breaker` are
  owner gated.

Tests: `test_submit_unauthorized_caller_reverts`, `test_set_keeper_grant_and_revoke`,
`test_set_keeper_non_owner_reverts`, `test_set_feed_active_non_owner_reverts`,
`test_reset_circuit_breaker_non_owner_reverts`, `test_register_feed_non_owner_reverts`.

## 7. Division by zero and integer coercion

- Spread denominator is `min(source_a, source_b)`. Both quotes are asserted
  positive before the division, so the denominator is at least 1. No zero division.
- Jump denominator is `prev_price`, used only inside `if prev_price > 0`. The first
  round skips the jump guard. No zero division.
- All arithmetic uses integer `u256`; there is no `float` on the deterministic path,
  so validators cannot diverge through rounding.
- `abs` and `min` are replaced with explicit sign handling and a conditional so the
  math is unambiguous on chain.

## 8. LLM, web, and price-range hardening

- `_coerce_scaled_price` rejects empty, malformed, double-decimal, and non-digit
  input, and folds a negative price to zero.
- In `fetch_and_verify_live_price` each extracted price must satisfy `0 < price <= 10^18`.
  A non-positive or out-of-range value raises a deterministic `[LLM_ERROR]` revert,
  so a hallucinated or overflowing number can never be written to state.
- The `attestation_note` is truncated to 256 chars to bound storage and cost, so a
  prompt-injected page cannot make the LLM emit an unbounded note.
- Extraction runs inside `gl.eq_principle.prompt_comparative` with a tight 0.5
  percent cross-validator tolerance, so a single malicious or hallucinating leader
  cannot push an outlier price through without validators agreeing independently.

Tests: `test_fetch_non_positive_hallucination_reverts`, `test_fetch_out_of_range_price_reverts`.

## 9. Findings ledger

| ID | Finding | Status and proof |
| --- | --- | --- |
| F1 | Poisoned midpoint could overwrite the trusted price on a divergent pair | FIXED: trip branch returns before writing `latest_price`; `test_spread_breach_trips_and_does_not_poison_price` |
| F2 | Large temporal gap accepted as a normal update | FIXED: jump guard trips; `test_temporal_jump_breach_trips` |
| F3 | Zero denominator in spread or jump math | FIXED: positive-quote check plus `prev_price > 0` guard |
| F4 | Negative, zero, or oversized LLM price poisoning state | FIXED: `_coerce_scaled_price`, `0 < price <= 10^18` bound, deterministic revert; `test_fetch_non_positive_hallucination_reverts`, `test_fetch_out_of_range_price_reverts` |
| F5 | Unbounded or injection-laden caller strings | FIXED: length caps on `symbol` (1..32), URLs (8..512, no whitespace or control bytes), and `note` (max 256, LLM note truncated); five bound tests in section 5 |
| F6 | Silent empty return on unknown ids | FIXED: explicit `[EXPECTED]` reverts on unknown feed and round |
| F7 | `active` was a dead field with no way to pause a feed | FIXED: `set_feed_active` wired with owner gating and unknown-feed revert; `test_set_feed_active_blocks_and_restores`, `test_set_feed_active_non_owner_reverts`, `test_set_feed_active_unknown_reverts` |
| F8 | `submit_deterministic_quote` was open to any caller (griefing vector) | FIXED: `authorized_keepers` map plus owner-only `set_keeper` grant/revoke; `test_submit_unauthorized_caller_reverts`, `test_set_keeper_grant_and_revoke`, `test_set_keeper_non_owner_reverts` |
| F9 | Keeper address argument accepted malformed input | FIXED: `_to_address` enforces a 42-char hex value and reverts otherwise; `test_set_keeper_invalid_address_reverts` |
| F10 | A feed or round could be created but never deactivated for operational pausing | FIXED: feeds are pausable via `set_feed_active` and recoverable via `reset_circuit_breaker`; rounds are append-only by design so consumption history stays auditable, which is a documented invariant, not a defect |

## 10. Coverage and scalability notes

- Direct mode runs only the leader. It confirms validation, access control, guard
  math, string bounds, and the not-poisoned invariant. It does not prove
  cross-validator agreement on the live fetch path; that is a consensus property
  covered by the studionet integration run in `tests/test_price_guard_integration.py`
  and the evidence in `evidence/studionet_deployment.json`.
- `get_guard_summary` and `get_latest_safe_price` iterate feeds and rounds, so read
  cost grows with history. This is a deliberate audit-first tradeoff that is not a
  fund or griefing risk: the maps are bounded by owner `register_feed` calls and
  keeper submissions, both now access-controlled. A counter index can be added later
  without changing the public interface if throughput requires it.
