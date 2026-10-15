# MultiSourcePriceGuard

A production-grade, standalone GenLayer Intelligent Contract primitive: a
manipulation-resistant, multi-source price and index oracle with automated
dispersion (spread) analysis and an on-chain circuit breaker for DeFi protocols.

The contract is backend-only. There is no frontend. Consumers read a price that
has already been forced to agree across two independent sources and to respect a
bounded move against the last accepted value. When either condition fails, the
feed trips an on-chain circuit breaker and the consumed price is frozen at the
last value that passed both guards.

## Why this needs GenLayer

The consensus-critical role is settling a single authoritative price from two
external sources whose raw values cannot be trusted on their own. A keeper, or an
LLM-assisted fetch, supplies candidate prices; GenLayer validators must
independently re-fetch the same sources and agree on the extracted numbers before
any on-chain state that consumers rely on is written. The spread guard and the
temporal jump guard then decide, deterministically and reproducibly, whether that
agreed value is accepted or whether the breaker trips.

## Architecture boundary

- Off-chain clients and keepers own: scheduling, caching, UI, and non-authoritative previews.
- The contract owns: the minimum state transition that needs agreement, which is recording a round, updating `latest_price`, and flipping `circuit_broken`.
- External sources own: raw facts. They are treated as untrusted until validators re-fetch, normalize to a scaled integer, and compare them.

Every transition that determines what price is consumed runs the same guard code,
whether the numbers arrive through the deterministic keeper path or the live fetch
path.

## Two-invariant circuit breaker

Prices are scaled by 10^8. For example, `$2500.00` is `250000000000`. Both guards
use integer basis-point math on `u256`; there is no floating point on the
deterministic path, so validators cannot diverge through rounding.

Given two positive quotes `source_a_scaled` and `source_b_scaled`:

```
mid  = (source_a_scaled + source_b_scaled) // 2
diff = abs(source_a_scaled - source_b_scaled)
lo   = min(source_a_scaled, source_b_scaled)
```

### Invariant 1: Cross-Source Spread Guard

```
spread_bps = (diff * 10000) // lo
```

If `spread_bps > max_spread_bps`, the two sources disagree by more than the
configured tolerance. The breaker trips, a `PriceRound` with status
`CIRCUIT_BROKEN_SPREAD` is recorded, and `latest_price` is NOT overwritten. The
denominator `lo` is always at least 1 because both quotes are validated as
positive before the division, so the guard can never divide by zero.

### Invariant 2: Temporal Jump Guard

```
prev_price = latest_price.get(feed_id, 0)
jump_bps   = (abs(mid - prev_price) * 10000) // prev_price   # only when prev_price > 0
```

If `jump_bps > max_jump_bps`, the move against the last accepted price is too
large. The breaker trips, a `PriceRound` with status `CIRCUIT_BROKEN_JUMP` is
recorded, and `latest_price` is NOT overwritten. The first accepted round on a
fresh feed skips this guard because there is no prior price yet.

Only when both guards pass does the contract write `latest_price = mid` and record
a round with status `ACCEPTED`. A tripped feed rejects every further update until
the owner calls `reset_circuit_breaker`.

## `prompt_comparative` consensus design

`fetch_and_verify_live_price` snapshots the feed configuration into plain Python
variables before entering the non-deterministic block. Inside `gl.eq_principle.prompt_comparative`:

1. It fetches both source URLs with `gl.nondet.web.render(..., mode="text")`.
2. It extracts the 10^8-scaled integers with `gl.nondet.exec_prompt(..., response_format="json")`.
3. The comparative principle requires each validator to independently fetch both
   URLs and agree that the extracted `source_a_scaled` and `source_b_scaled`
   values match the leader within a tight 0.5 percent tolerance.

The agreed integers are then fed through the exact same deterministic spread and
jump guard as the keeper path. Extraction is hardened against LLM hallucination:
malformed, empty, double-decimal, and negative values are coerced to zero, which
fails the positive check and raises an `[EXTERNAL]` error rather than poisoning
state.

Note on coverage: direct-mode tests exercise only the leader extraction path.
Cross-validator agreement on the live fetch path is a consensus property that is
proven by running the method on a real network, not by unit tests. The committed
studionet evidence drives the deterministic guard path through full consensus.

## Storage layout

All persistent structures are `@allow_storage` dataclasses. Field order is the
storage layout and must only ever be extended at the end.

```
FeedConfig:
  feed_id: u256
  symbol: str
  primary_url: str
  secondary_url: str
  max_spread_bps: u256   # 1..5000
  max_jump_bps: u256     # 1..5000
  circuit_broken: bool
  active: bool

PriceRound:
  round_id: u256
  feed_id: u256
  median_price_scaled: u256   # scaled by 10^8
  observed_spread_bps: u256
  status: str                 # ACCEPTED | CIRCUIT_BROKEN_SPREAD | CIRCUIT_BROKEN_JUMP
  reporter: Address
  attestation_note: str
```

Contract state:

```
owner: Address
next_feed_id: u256
next_round_id: u256
feeds: TreeMap[u256, FeedConfig]
latest_price: TreeMap[u256, u256]
rounds: TreeMap[u256, PriceRound]
feed_rounds: TreeMap[u256, DynArray[u256]]
authorized_keepers: TreeMap[Address, bool]
```

## Public interface

| Method | Kind | Access | Purpose |
| --- | --- | --- | --- |
| `register_feed(symbol, primary_url, secondary_url, max_spread_bps, max_jump_bps) -> u256` | write | owner | Create a feed, returns its id. |
| `submit_deterministic_quote(feed_id, source_a_scaled, source_b_scaled, note) -> str` | write | owner or authorized keeper | Run the guard engine on supplied quotes. |
| `fetch_and_verify_live_price(feed_id) -> str` | write | any caller | Fetch both URLs, reach comparative consensus, run the guard engine. |
| `set_keeper(keeper, allowed) -> None` | write | owner | Grant or revoke an address as an authorized keeper. |
| `set_feed_active(feed_id, active) -> None` | write | owner | Pause or resume a feed without tripping the breaker. |
| `reset_circuit_breaker(feed_id) -> None` | write | owner | Clear `circuit_broken` for a feed. |
| `get_feed(feed_id) -> dict` | view | read | Full feed configuration. |
| `get_round(round_id) -> dict` | view | read | One recorded round with its status and reporter. |
| `get_latest_safe_price(feed_id) -> dict` | view | read | `latest_price`, `circuit_broken`, `is_safe_to_consume`, round count. |
| `get_guard_summary() -> dict` | view | read | Feed and round counts split by health. |

`get_latest_safe_price` reports `is_safe_to_consume` as true only when the feed is
active, not circuit broken, and has a non-zero price. Consumers should gate on this
flag, not on the raw number.

## Limitations

- `submit_deterministic_quote` is gated to the owner or an address in
  `authorized_keepers`, managed by the owner-only `set_keeper`. An unauthorized
  caller reverts before any state write, so the griefing vector of spamming quotes
  or forcing a breaker trip is closed.
- A feed can be paused with owner-only `set_feed_active(feed_id, false)` and
  resumed with `true`. While inactive, both the deterministic and live update paths
  revert and the consumed price is frozen.
- Input strings are length-bounded at every write entry point: `symbol` is 1..32
  chars, URLs are 8..512 chars with no whitespace or control bytes, and notes are
  capped at 256 chars (LLM output truncated to fit). Fetched prices must satisfy
  `0 < price <= 10^18` or the update reverts with a deterministic error.
- Feeds and rounds are permanent; there is no delete path. This preserves audit
  history at the cost of unbounded round growth.
- `get_guard_summary` and `get_latest_safe_price` iterate feeds and rounds, so
  their cost grows with history. A counter index can be added without changing the
  public interface if throughput requires it.
- The live fetch method depends on external URL availability. Fetch failures raise
  classified errors and never write state.

## Deployed on studionet (verified evidence)

The contract was deployed and driven through real transactions on GenLayer
studionet. Every transaction reached `FINALIZED` with consensus `MAJORITY_AGREE`,
and the deployed source was confirmed byte-for-byte against this repository file
through `gen_getContractCode` (sha256 identity true). The line endings are pinned
to LF via `.gitattributes`, so the on-chain sha256 also equals the file as served
by the remote repository. The full machine-readable proof is in
`evidence/studionet_deployment.json`.

```
Contract address: 0x7a4eD4eA7d5a2A4c8b3727A74e3f5625e789Bc55
Deploy tx:        0xd38eda48dc8d4640b15e58ba35ee501a73484f5ce6ea29891dd2e51a518029f2
Source sha256:    7793dfac5091042baf0026f430706eb33d035b317a60e8cb81fb5eddffad69ed
```

Scenario transactions, all `FINALIZED`:

| Step | Result | Tx hash |
| --- | --- | --- |
| register_feed | `1` | `0xec32985246ff1c21d9d4216b654b7dcc2db2bfd35581613d4eb76544a85ed10f` |
| set_keeper grant (owner) | success | `0xc43804b803248f8a7cabb2010638ff28f04a4e01f5d420726f6f8868e6a880cf` |
| authorized keeper submit accepted | `ACCEPTED` | `0xd806f3f89682505310c2315ac74c670483972b9f55e06fe76c22c0eba74715a4` |
| unauthorized submit (expected rollback) | reverted: `[EXPECTED] caller is not an authorized keeper` | `0x3d0bb76212405eb5a3a9f5d65914f526298f3a5e34e4bfe9e8bdc9571ff1ee16` |
| submit spread breach (2500/1000) | `CIRCUIT_BROKEN_SPREAD` | `0x41ec560501607005d3d51bb93c8c0724e9eaae6016c069b46fa31733598a9f04` |
| reset_circuit_breaker (owner) | success | `0x09af32b36a671bf6efadf3121bf39eacc872271f467e101c164864adc22498e9` |
| set_feed_active false (owner) | success | `0xb7dc2efd1e4c522677c0d528d367db82171cc890adb9b716bfa05dbe08028b69` |
| submit while inactive (expected rollback) | reverted: `[EXPECTED] feed is inactive` | `0xd898f0ebdd78fe0cabc31411e0a7ab2dd8dfe94dbac249c5fc13c0c8de5e1dc4` |
| set_feed_active true (owner) | success | `0x08077c16136fb775a39bdc7269d39b2808e8fce8fb4d07571ad091fae2a8a82a` |

After the spread breach, `get_latest_safe_price` on chain still reported
`latest_price = 250000000000`, `circuit_broken = true`, and
`is_safe_to_consume = false`, which proves the poisoned midpoint never reached the
consumed price. The two expected rollbacks finalized with consensus agreement yet
reverted execution, leaving state unchanged. The final `get_guard_summary` reported
1 feed, 2 rounds, 1 accepted, and 1 tripped.

## Toolchain and runner pin

The contract's first line pins the concrete GenVM runner:

```
# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
```

This pinned hash is what the network accepts. Local development aliases such as
`test` and `latest` are never used.

## Getting started

Prerequisites: a Python environment with `genlayer-py`, `genlayer-test`, and
`genvm-linter`, plus the Node `genlayer` CLI. Copy `.env.example` to `.env` and
fill in `GENLAYER_PRIVATE_KEY` and `GITHUB_TOKEN`. The `.env` file is gitignored
and must never be committed.

```
# Validate the contract with the GenVM linter (expect 0 errors)
genvm-lint check contracts/multi_source_price_guard.py

# Fast in-memory logic tests
pytest tests/test_price_guard_direct.py -v

# Deploy to studionet and write evidence/studionet_deployment.json
python scripts/deploy_studionet.py

# Full-consensus integration run against studionet
gltest tests/test_price_guard_integration.py --network studionet -v -s
```

## Repository layout

```
contracts/multi_source_price_guard.py   the primitive
tests/test_price_guard_direct.py        39 direct-mode tests
tests/test_price_guard_integration.py   full-consensus studionet run
tests/conftest.py                        direct-mode VM shim for Windows
scripts/deploy_studionet.py             deploy, drive, and record evidence
docs/adversarial-review.md              threat model and findings
evidence/studionet_deployment.json      on-chain proof
gltest.config.yaml                      studionet network config (key from .env)
```

## Security

Credentials are read only from this project's own `.env`. Neither the private key
nor the GitHub token is ever committed or printed. The repository ships
`.env.example` with placeholders and a `.gitignore` that excludes `.env` and all
`.env.*` files except the example.
