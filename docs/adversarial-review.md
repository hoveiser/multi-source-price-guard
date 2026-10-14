# Adversarial Review: MultiSourcePriceGuard

Reviewer posture: every caller is adversarial. The owner, any keeper that can
call `submit_deterministic_quote`, the validators' inputs, the external pages
fetched by `fetch_and_verify_live_price`, and the LLM that extracts prices from
those pages are all treated as hostile or unreliable. This note records what was
checked against the actual code in `contracts/multi_source_price_guard.py` and
the test that proves each result.

## 1. Feed state machine

| Status | Who may act | Allowed transitions | Effect on consumed price (`latest_price`) |
| --- | --- | --- | --- |
| Active, healthy | keeper (`submit_deterministic_quote`), owner (`fetch_and_verify_live_price`) | ACCEPTED stays healthy; spread or jump breach trips circuit | only an ACCEPTED round writes `latest_price` |
| Active, healthy, first round | keeper, owner | ACCEPTED, or CIRCUIT_BROKEN_SPREAD | `latest_price` written only on ACCEPTED; a trip leaves it at zero |
| Circuit broken (spread or jump) | owner only, via `reset_circuit_breaker` | reset returns to healthy | frozen; every update path reverts while broken |
| Inactive (`active = false`) | none | unreachable | not settable today, see finding F7 |

`register_feed` and `reset_circuit_breaker` are owner gated. `submit_deterministic_quote`
and `fetch_and_verify_live_price` are open to any caller by design (a permissioned
keeper allowlist is a documented extension point, see F8).

## 2. Liveness and stuck state

The circuit breaker intentionally freezes consumption when sources disagree, so a
stuck state is a feature. The recovery path is `reset_circuit_breaker`, owner gated,
callable at any time, so a tripped feed is never permanently unusable. There is no
funds lockup because the primitive holds no funds; the "asset" it protects is the
price consumers read.

- Proven by `test_reset_circuit_breaker_by_owner_recovers`.
- Proven by `test_update_blocked_while_circuit_broken` (a broken feed rejects updates
  until reset, so it cannot be silently overwritten).

## 3. Races between competing transitions

Writes serialize on chain; there is no interleaving inside one transaction. The
relevant ordering guard is that `reset_circuit_breaker` (owner) and an update
(keeper) both mutate only the same feed's fields, and `_apply_quotes` re-reads
`feed.circuit_broken` immediately before writing, so a reset followed by an update
in the same block still runs the full guard. A stale read cannot bypass the guard
because the guard re-evaluates spread and jump from the raw quotes every time.

- Proven by `test_reset_circuit_breaker_by_owner_recovers` (reset then a new guarded
  submit that can itself trip again).

## 4. Accounting and safety invariants

Invariant: `latest_price[feed_id]` only ever equals the midpoint of a quote pair that
passed both the spread and the jump guard while the feed was healthy. A tripped round
must never move `latest_price`.

Enforced in `_apply_quotes`: `latest_price[feed_id] = mid` runs only on the ACCEPTED
branch, after both guards pass. Every trip branch records the round and returns before
touching `latest_price`.

- Proven by `test_spread_breach_trips_and_does_not_poison_price` (seed 2500, then a
  15000 bps divergent pair; `latest_price` stays at 2500, not the poisoned midpoint 1750).
- Proven by `test_temporal_jump_breach_trips` (`latest_price` stays at the seed).
- Proven by `test_fetch_outlier_trips_spread_guard` (live path leaves `latest_price` at 0).

## 5. Input validation at every entry point

- `register_feed`: non-empty symbol, absolute http(s) URLs only, primary must differ
  from secondary, and both thresholds bounded to `1..5000` (floor and ceiling, reject
  rather than clamp). Proven by `test_register_feed_empty_symbol_reverts`,
  `test_register_feed_bad_url_scheme_reverts`, `test_register_feed_identical_urls_reverts`,
  `test_register_feed_spread_out_of_range_reverts`,
  `test_register_feed_jump_out_of_range_reverts`.
- Access control on the two owner-only methods. Proven by
  `test_register_feed_non_owner_reverts`, `test_reset_circuit_breaker_non_owner_reverts`.
- Non-positive quotes rejected. Proven by `test_non_positive_quote_reverts`.
- Unknown feed and unknown round rejected rather than returning empty objects. Proven by
  `test_submit_unknown_feed_reverts`, `test_reset_unknown_feed_reverts`,
  `test_get_round_unknown_reverts`.

## 6. Division by zero and integer coercion

- Spread denominator is `min(source_a, source_b)`. Both quotes are asserted positive
  before the division, so the denominator is at least 1. No zero division.
- Jump denominator is `prev_price`, only used inside `if prev_price > 0`. The very
  first round skips the jump guard entirely. No zero division.
- All arithmetic uses integer `u256`; there is no `float` on the deterministic path,
  so no rounding nondeterminism between validators.
- `abs` and `min` were replaced with explicit sign handling and a conditional so the
  arithmetic is unambiguous on chain.

## 7. LLM and web hallucination hardening

- `_coerce_scaled_price` rejects empty, malformed, double-decimal, and non-digit
  input, and folds a negative price to zero. A zero then fails the positive check in
  `fetch_task`, which raises `[EXTERNAL]`, so a hallucinated negative or missing price
  can never be written to `latest_price`.
- The extraction happens inside `gl.eq_principle.prompt_comparative` with a tight 0.5
  percent cross-validator tolerance, so a single malicious or hallucinating leader
  cannot push an outlier price through without validators agreeing independently.
- Direct-mode proves only the leader extraction path. Cross-validator agreement on
  `fetch_and_verify_live_price` is exercised on studionet, not in direct mode. This is
  stated honestly and covered by the integration run.

## 8. Findings

| ID | Finding | Status |
| --- | --- | --- |
| F1 | Poisoned midpoint could overwrite the trusted price on a divergent pair | FIXED: trip branch returns before writing `latest_price`; proven by `test_spread_breach_trips_and_does_not_poison_price` |
| F2 | Large temporal gap accepted as a normal update | FIXED: jump guard trips; proven by `test_temporal_jump_breach_trips` |
| F3 | Zero denominator in spread or jump math | FIXED: positive-quote check plus `prev_price > 0` guard |
| F4 | Negative or missing LLM price poisoning state | FIXED: `_coerce_scaled_price` plus `[EXTERNAL]` raise; proven by `test_fetch_non_positive_hallucination_reverts` |
| F5 | Non-owner tripping recovery or forging feeds | FIXED: owner gating; proven by the two non-owner revert tests |
| F6 | Silent empty return on unknown ids | FIXED: explicit `[EXPECTED]` reverts |
| F7 | `active = false` is never reachable, so a feed cannot be paused without tripping the breaker | OPEN by design: there is no `deactivate_feed` method in the mandated interface; use `reset_circuit_breaker` and the breaker for pausing, or add a dedicated pause method if required |
| F8 | `submit_deterministic_quote` is callable by anyone | OPEN by design: the primitive models an open keeper set; production deployments should add a keeper allowlist. The guard math is identical for every caller, so an adversarial keeper can trip the breaker but cannot forge an ACCEPTED price beyond the two invariants |
| F9 | `get_guard_summary` and `get_round` iterate all rounds and all feeds, so cost grows with history | OPEN: acceptable for a primitive; documented as a limitation. A counter index can be added without changing the interface if throughput demands it |
| F10 | A feed can only ever be registered, never deleted | OPEN by design: history and auditability require permanent rounds; deletion is intentionally unsupported |

## 9. What direct mode does NOT prove

Direct mode runs only the leader. It confirms validation, access control, guard math,
and the not-poisoned invariant. It does not prove that validators reach the same
`ACCEPTED` or trip decision on live fetched data. That consensus behavior is covered by
the studionet integration run in `tests/test_price_guard_integration.py` and the
evidence in `evidence/studionet_deployment.json`.
