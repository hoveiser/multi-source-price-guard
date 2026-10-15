# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

from genlayer import *

import json
from dataclasses import dataclass


# Canonical price scale: 1 USD -> 10^8 scaled units.
PRICE_SCALE: int = 100000000

# Hard ceiling for both guard thresholds, in basis points.
MAX_BPS_LIMIT: int = 5000

# Bounds for caller-supplied text and fetched prices.
MIN_URL_LEN: int = 8
MAX_URL_LEN: int = 512
MAX_SYMBOL_LEN: int = 32
MAX_NOTE_LEN: int = 256
# Upper bound for a single scaled price accepted from the oracle path.
MAX_SCALED_PRICE: int = 10 ** 18

# Error classification prefixes so validators can compare failure paths.
ERROR_EXPECTED = "[EXPECTED]"
ERROR_EXTERNAL = "[EXTERNAL]"
ERROR_LLM = "[LLM_ERROR]"


@allow_storage
@dataclass
class FeedConfig:
    feed_id: u256
    symbol: str
    primary_url: str
    secondary_url: str
    max_spread_bps: u256
    max_jump_bps: u256
    circuit_broken: bool
    active: bool


@allow_storage
@dataclass
class PriceRound:
    round_id: u256
    feed_id: u256
    median_price_scaled: u256
    observed_spread_bps: u256
    status: str
    reporter: Address
    attestation_note: str


def _is_http_url(url: str) -> bool:
    """Absolute http(s) URL with bounded length and no whitespace or control bytes."""
    if not isinstance(url, str):
        return False
    if len(url) < MIN_URL_LEN or len(url) > MAX_URL_LEN:
        return False
    if not (url.startswith("http://") or url.startswith("https://")):
        return False
    for ch in url:
        code = ord(ch)
        # Reject control bytes, space, and DEL or higher to avoid injection vectors.
        if code <= 32 or code >= 127:
            return False
    return True


def _to_address(value: str) -> Address:
    """Parse a 0x-prefixed 20-byte hex address string into an Address."""
    if not isinstance(value, str):
        raise gl.vm.UserError(f"{ERROR_EXPECTED} keeper address must be a string")
    if not value.startswith("0x") or len(value) != 42:
        raise gl.vm.UserError(f"{ERROR_EXPECTED} keeper address must be 42 hex chars")
    for ch in value[2:]:
        if ch not in "0123456789abcdefABCDEF":
            raise gl.vm.UserError(f"{ERROR_EXPECTED} keeper address has non-hex chars")
    return Address(value)


def _as_text(resp) -> str:
    """Normalize a GenLayer web render result into plain text."""
    if isinstance(resp, str):
        return resp
    if isinstance(resp, bytes):
        return resp.decode("utf-8", "ignore")
    body = getattr(resp, "body", None)
    if isinstance(body, bytes):
        return body.decode("utf-8", "ignore")
    if isinstance(body, str):
        return body
    text = getattr(resp, "text", None)
    if isinstance(text, str):
        return text
    return str(resp)


def _parse_json_object(raw) -> dict:
    """Extract a JSON object from an LLM response that may be wrapped in prose."""
    if isinstance(raw, dict):
        return raw
    text = raw if isinstance(raw, str) else str(raw)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise gl.vm.UserError(f"{ERROR_LLM} LLM returned no JSON object")
    return json.loads(text[start : end + 1])


def _coerce_scaled_price(value) -> int:
    """Coerce a hallucination-prone LLM price field into a non-negative integer."""
    text = str(value).strip()
    if text == "":
        raise gl.vm.UserError(f"{ERROR_LLM} empty price field")
    negative = False
    if text.startswith("-"):
        negative = True
        text = text[1:].strip()
    if text.startswith("+"):
        text = text[1:].strip()
    if text == "":
        raise gl.vm.UserError(f"{ERROR_LLM} non-numeric price field")
    n = 0
    scale = 0
    seen_dot = False
    for ch in text:
        if ch == ".":
            if seen_dot:
                raise gl.vm.UserError(f"{ERROR_LLM} malformed price field")
            seen_dot = True
            continue
        if ch < "0" or ch > "9":
            raise gl.vm.UserError(f"{ERROR_LLM} non-numeric price field")
        n = n * 10 + (ord(ch) - ord("0"))
        if seen_dot:
            scale += 1
    if scale > 0:
        n = n // (10**scale)
    if negative:
        # A negative spot price is never valid input; collapse it to zero so the
        # downstream positive check rejects it rather than poisoning state.
        return 0
    return n


class MultiSourcePriceGuard(gl.Contract):
    owner: Address
    next_feed_id: u256
    next_round_id: u256
    feeds: TreeMap[u256, FeedConfig]
    latest_price: TreeMap[u256, u256]
    rounds: TreeMap[u256, PriceRound]
    feed_rounds: TreeMap[u256, DynArray[u256]]
    authorized_keepers: TreeMap[Address, bool]

    def __init__(self):
        self.owner = gl.message.sender_address
        self.next_feed_id = u256(1)
        self.next_round_id = u256(1)
        self.authorized_keepers[self.owner] = True

    @gl.public.write
    def register_feed(
        self,
        symbol: str,
        primary_url: str,
        secondary_url: str,
        max_spread_bps: u256,
        max_jump_bps: u256,
    ) -> u256:
        if gl.message.sender_address != self.owner:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Only owner can register a feed")
        if not isinstance(symbol, str) or symbol.strip() == "" or len(symbol) > MAX_SYMBOL_LEN:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} symbol must be 1..32 chars")
        if not _is_http_url(primary_url):
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} primary_url must be http(s), 8..512 chars, no whitespace"
            )
        if not _is_http_url(secondary_url):
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} secondary_url must be http(s), 8..512 chars, no whitespace"
            )
        if primary_url == secondary_url:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} URLs must be distinct")
        if max_spread_bps <= 0 or max_spread_bps > MAX_BPS_LIMIT:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} max_spread_bps out of range 1..5000")
        if max_jump_bps <= 0 or max_jump_bps > MAX_BPS_LIMIT:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} max_jump_bps out of range 1..5000")

        feed_id = u256(self.next_feed_id)
        self.next_feed_id = u256(feed_id + 1)
        self.feeds[feed_id] = FeedConfig(
            feed_id=feed_id,
            symbol=symbol,
            primary_url=primary_url,
            secondary_url=secondary_url,
            max_spread_bps=u256(max_spread_bps),
            max_jump_bps=u256(max_jump_bps),
            circuit_broken=False,
            active=True,
        )
        return feed_id

    @gl.public.write
    def submit_deterministic_quote(
        self,
        feed_id: u256,
        source_a_scaled: u256,
        source_b_scaled: u256,
        note: str,
    ) -> str:
        # Keeper griefing gate: only the owner or an explicitly authorized keeper may
        # push deterministic quotes. Anyone else reverts before touching state.
        sender = gl.message.sender_address
        if sender != self.owner and not self.authorized_keepers.get(sender, False):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} caller is not an authorized keeper")
        if not isinstance(note, str) or len(note) > MAX_NOTE_LEN:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} note must be at most 256 chars")
        # Deterministic keeper path: the exact same guard accounting runs as the
        # live fetch path, with no nondeterministic input to trust.
        return self._apply_quotes(
            feed_id,
            u256(source_a_scaled),
            u256(source_b_scaled),
            note,
        )

    @gl.public.write
    def fetch_and_verify_live_price(self, feed_id: u256) -> str:
        # Pre-flight deterministic checks before entering the nondeterministic block.
        if feed_id not in self.feeds:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} feed not found")
        feed = self.feeds[feed_id]
        if not feed.active:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} feed is inactive")
        if feed.circuit_broken:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} circuit breaker is tripped")

        # Snapshot plain Python values so the nondeterministic block never reads
        # mutable storage state mid-flight.
        symbol = feed.symbol
        primary_url = feed.primary_url
        secondary_url = feed.secondary_url

        def fetch_task() -> dict:
            page_a = gl.nondet.web.render(primary_url, mode="text")
            page_b = gl.nondet.web.render(secondary_url, mode="text")
            text_a = _as_text(page_a)
            text_b = _as_text(page_b)
            prompt = (
                "You are a financial spot-price oracle. Two independent sources publish the "
                f"spot price for {symbol}. Read each page, find the spot price quoted in US "
                "dollars, and express it scaled by 100000000 (10^8) as a plain integer. If a "
                "page has no clear numeric spot price, use 0 for that source. Respond with "
                "ONLY a JSON object shaped exactly like this: "
                '{"source_a_scaled": <int>, "source_b_scaled": <int>, '
                '"attestation_note": "<short summary>"}\n\n'
                f"SOURCE_A_PAGE:\n{text_a[:4000]}\n\nSOURCE_B_PAGE:\n{text_b[:4000]}"
            )
            raw = gl.nondet.exec_prompt(prompt, response_format="json")
            parsed = _parse_json_object(raw)
            sa = _coerce_scaled_price(parsed.get("source_a_scaled"))
            sb = _coerce_scaled_price(parsed.get("source_b_scaled"))
            attestation = str(parsed.get("attestation_note", ""))[:MAX_NOTE_LEN]
            if sa <= 0 or sb <= 0:
                raise gl.vm.UserError(f"{ERROR_LLM} scaled price is not positive")
            if sa > MAX_SCALED_PRICE or sb > MAX_SCALED_PRICE:
                raise gl.vm.UserError(f"{ERROR_LLM} scaled price is out of range")
            return {
                "source_a_scaled": sa,
                "source_b_scaled": sb,
                "attestation_note": attestation,
            }

        agreed = gl.eq_principle.prompt_comparative(
            fetch_task,
            principle=(
                "The leader and each validator independently fetched both source URLs and "
                "extracted the 10^8-scaled spot prices. source_a_scaled and source_b_scaled "
                "must each agree between leader and validator within a tight tolerance of 0.5 "
                "percent. The attestation_note may differ in wording but must describe the same "
                "prices. Any disagreement on the price fields must be rejected."
            ),
        )

        a = u256(int(agreed["source_a_scaled"]))
        b = u256(int(agreed["source_b_scaled"]))
        note = str(agreed["attestation_note"])[:MAX_NOTE_LEN]
        # Feed the agreed values through the identical deterministic guard engine.
        return self._apply_quotes(feed_id, a, b, note)

    @gl.public.write
    def reset_circuit_breaker(self, feed_id: u256) -> None:
        if gl.message.sender_address != self.owner:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Only owner can reset the circuit breaker")
        if feed_id not in self.feeds:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} feed not found")
        feed = self.feeds[feed_id]
        feed.circuit_broken = False
        self.feeds[feed_id] = feed

    @gl.public.write
    def set_keeper(self, keeper: str, allowed: bool) -> None:
        if gl.message.sender_address != self.owner:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Only owner can manage keepers")
        addr = _to_address(keeper)
        self.authorized_keepers[addr] = allowed

    @gl.public.write
    def set_feed_active(self, feed_id: u256, active: bool) -> None:
        if gl.message.sender_address != self.owner:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Only owner can change feed activity")
        if feed_id not in self.feeds:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} feed not found")
        feed = self.feeds[feed_id]
        feed.active = active
        self.feeds[feed_id] = feed

    def _apply_quotes(
        self,
        feed_id: u256,
        source_a_scaled: u256,
        source_b_scaled: u256,
        note: str,
    ) -> str:
        if feed_id not in self.feeds:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} feed not found")
        feed = self.feeds[feed_id]
        if not feed.active:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} feed is inactive")
        if feed.circuit_broken:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} circuit breaker is tripped")
        if source_a_scaled <= 0 or source_b_scaled <= 0:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} quotes must be positive")

        mid = u256((source_a_scaled + source_b_scaled) // 2)

        # Cross-source spread. Denominator is the smaller positive quote, so it is
        # never zero because both quotes were validated as positive above.
        diff = source_a_scaled - source_b_scaled
        if diff < 0:
            diff = -diff
        lo = source_a_scaled if source_a_scaled < source_b_scaled else source_b_scaled
        spread_bps = u256((diff * 10000) // lo)

        round_id = u256(self.next_round_id)
        self.next_round_id = u256(round_id + 1)
        reporter = gl.message.sender_address

        # Invariant 1: Cross-Source Spread Guard.
        if spread_bps > feed.max_spread_bps:
            feed.circuit_broken = True
            self.feeds[feed_id] = feed
            self._record_round(
                round_id, feed_id, mid, spread_bps, "CIRCUIT_BROKEN_SPREAD", reporter, note
            )
            return "CIRCUIT_BROKEN_SPREAD"

        # Invariant 2: Temporal Jump Guard.
        prev_price = u256(self.latest_price.get(feed_id, u256(0)))
        if prev_price > 0:
            jdiff = mid - prev_price
            if jdiff < 0:
                jdiff = -jdiff
            jump_bps = u256((jdiff * 10000) // prev_price)
            if jump_bps > feed.max_jump_bps:
                feed.circuit_broken = True
                self.feeds[feed_id] = feed
                self._record_round(
                    round_id, feed_id, mid, spread_bps, "CIRCUIT_BROKEN_JUMP", reporter, note
                )
                return "CIRCUIT_BROKEN_JUMP"

        # Accepted: only now do we overwrite the consumed price.
        self.latest_price[feed_id] = mid
        self._record_round(
            round_id, feed_id, mid, spread_bps, "ACCEPTED", reporter, note
        )
        return "ACCEPTED"

    def _record_round(
        self,
        round_id: u256,
        feed_id: u256,
        median_scaled: u256,
        spread_bps: u256,
        status: str,
        reporter: Address,
        note: str,
    ) -> None:
        self.rounds[round_id] = PriceRound(
            round_id=round_id,
            feed_id=feed_id,
            median_price_scaled=median_scaled,
            observed_spread_bps=spread_bps,
            status=status,
            reporter=reporter,
            attestation_note=note,
        )
        arr = self.feed_rounds.get(feed_id)
        if arr is None:
            # Seed a brand new feed with its first round id. The TreeMap value
            # type DynArray[u256] serializes a plain Python list on assignment.
            self.feed_rounds[feed_id] = [round_id]
        else:
            arr.append(round_id)

    @gl.public.view
    def get_feed(self, feed_id: u256) -> dict:
        if feed_id not in self.feeds:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} feed not found")
        f = self.feeds[feed_id]
        return {
            "feed_id": u256(f.feed_id),
            "symbol": f.symbol,
            "primary_url": f.primary_url,
            "secondary_url": f.secondary_url,
            "max_spread_bps": u256(f.max_spread_bps),
            "max_jump_bps": u256(f.max_jump_bps),
            "circuit_broken": f.circuit_broken,
            "active": f.active,
        }

    @gl.public.view
    def get_round(self, round_id: u256) -> dict:
        if round_id not in self.rounds:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} round not found")
        r = self.rounds[round_id]
        return {
            "round_id": u256(r.round_id),
            "feed_id": u256(r.feed_id),
            "median_price_scaled": u256(r.median_price_scaled),
            "observed_spread_bps": u256(r.observed_spread_bps),
            "status": r.status,
            "reporter": r.reporter.as_hex,
            "attestation_note": r.attestation_note,
        }

    @gl.public.view
    def get_latest_safe_price(self, feed_id: u256) -> dict:
        if feed_id not in self.feeds:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} feed not found")
        f = self.feeds[feed_id]
        price = u256(self.latest_price.get(feed_id, u256(0)))
        is_safe = f.active and (not f.circuit_broken) and price > 0
        rounds_count = 0
        if feed_id in self.feed_rounds:
            rounds_count = len(self.feed_rounds[feed_id])
        return {
            "feed_id": u256(feed_id),
            "latest_price": price,
            "circuit_broken": f.circuit_broken,
            "is_safe_to_consume": is_safe,
            "rounds_count": u256(rounds_count),
        }

    @gl.public.view
    def get_guard_summary(self) -> dict:
        total_feeds = 0
        active_feeds = 0
        broken_feeds = 0
        for fid in self.feeds:
            total_feeds += 1
            f = self.feeds[fid]
            if f.active:
                active_feeds += 1
            if f.circuit_broken:
                broken_feeds += 1

        total_rounds = 0
        accepted_rounds = 0
        tripped_rounds = 0
        for rid in self.rounds:
            total_rounds += 1
            if self.rounds[rid].status == "ACCEPTED":
                accepted_rounds += 1
            else:
                tripped_rounds += 1

        return {
            "total_feeds": u256(total_feeds),
            "active_feeds": u256(active_feeds),
            "circuit_broken_feeds": u256(broken_feeds),
            "total_rounds": u256(total_rounds),
            "accepted_rounds": u256(accepted_rounds),
            "tripped_rounds": u256(tripped_rounds),
        }
