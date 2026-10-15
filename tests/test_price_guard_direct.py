"""Direct-mode tests for MultiSourcePriceGuard.

These exercise business logic, validation, access control and the deterministic
guard engine. Direct mode runs the leader path only, so validator consensus for
fetch_and_verify_live_price is covered by the studionet integration run.
"""

import json

SCALE = 10 ** 8
ETH_2500 = 2500 * SCALE  # 250000000000
ETH_2510 = 2510 * SCALE
ETH_2700 = 2700 * SCALE
ETH_1000 = 1000 * SCALE

CONTRACT_PATH = "contracts/multi_source_price_guard.py"
PRIMARY = "https://source-a.test/eth-usd"
SECONDARY = "https://source-b.test/eth-usd"


def addr_hex(addr) -> str:
    """Render a test fixture address (raw bytes or Address) as a 0x hex string."""
    if isinstance(addr, (bytes, bytearray)):
        return "0x" + bytes(addr).hex()
    return addr.as_hex


def deploy_guard(direct_deploy):
    """Deploy with the default sender, which becomes the owner."""
    return direct_deploy(CONTRACT_PATH)


def register(direct_vm, contract, owner, spread=100, jump=200):
    direct_vm.sender = owner
    return contract.register_feed("ETH/USD", PRIMARY, SECONDARY, spread, jump)


# --------------------------------------------------------------------------
# Registration and access control
# --------------------------------------------------------------------------


def test_register_feed_returns_id_one(direct_vm, direct_deploy, direct_owner):
    contract = deploy_guard(direct_deploy)
    feed_id = register(direct_vm, contract, direct_owner)
    assert int(feed_id) == 1
    feed = contract.get_feed(feed_id)
    assert feed["symbol"] == "ETH/USD"
    assert feed["primary_url"] == PRIMARY
    assert feed["secondary_url"] == SECONDARY
    assert int(feed["max_spread_bps"]) == 100
    assert int(feed["max_jump_bps"]) == 200
    assert feed["circuit_broken"] is False
    assert feed["active"] is True


def test_register_feed_ids_increment(direct_vm, direct_deploy, direct_owner):
    contract = deploy_guard(direct_deploy)
    first = register(direct_vm, contract, direct_owner)
    second = contract.register_feed("BTC/USD", "https://a.test/btc", "https://b.test/btc", 50, 100)
    assert int(first) == 1
    assert int(second) == 2


def test_register_feed_non_owner_reverts(direct_vm, direct_deploy, direct_owner, direct_bob):
    contract = deploy_guard(direct_deploy)
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("Only owner"):
        contract.register_feed("ETH/USD", PRIMARY, SECONDARY, 100, 200)


def test_register_feed_identical_urls_reverts(direct_vm, direct_deploy, direct_owner):
    contract = deploy_guard(direct_deploy)
    direct_vm.sender = direct_owner
    with direct_vm.expect_revert("distinct"):
        contract.register_feed("ETH/USD", PRIMARY, PRIMARY, 100, 200)


def test_register_feed_empty_symbol_reverts(direct_vm, direct_deploy, direct_owner):
    contract = deploy_guard(direct_deploy)
    direct_vm.sender = direct_owner
    with direct_vm.expect_revert("symbol"):
        contract.register_feed("   ", PRIMARY, SECONDARY, 100, 200)


def test_register_feed_bad_url_scheme_reverts(direct_vm, direct_deploy, direct_owner):
    contract = deploy_guard(direct_deploy)
    direct_vm.sender = direct_owner
    with direct_vm.expect_revert("http"):
        contract.register_feed("ETH/USD", "ftp://a.test", SECONDARY, 100, 200)


def test_register_feed_spread_out_of_range_reverts(direct_vm, direct_deploy, direct_owner):
    contract = deploy_guard(direct_deploy)
    direct_vm.sender = direct_owner
    with direct_vm.expect_revert("max_spread_bps"):
        contract.register_feed("ETH/USD", PRIMARY, SECONDARY, 0, 200)
    with direct_vm.expect_revert("max_spread_bps"):
        contract.register_feed("ETH/USD", PRIMARY, SECONDARY, 5001, 200)


def test_register_feed_jump_out_of_range_reverts(direct_vm, direct_deploy, direct_owner):
    contract = deploy_guard(direct_deploy)
    direct_vm.sender = direct_owner
    with direct_vm.expect_revert("max_jump_bps"):
        contract.register_feed("ETH/USD", PRIMARY, SECONDARY, 100, 5001)


# --------------------------------------------------------------------------
# Deterministic quote path
# --------------------------------------------------------------------------


def test_identical_quotes_accepted(direct_vm, direct_deploy, direct_owner):
    contract = deploy_guard(direct_deploy)
    feed_id = register(direct_vm, contract, direct_owner)
    status = contract.submit_deterministic_quote(feed_id, ETH_2500, ETH_2500, "twin quotes")
    assert status == "ACCEPTED"
    safe = contract.get_latest_safe_price(feed_id)
    assert int(safe["latest_price"]) == ETH_2500
    assert safe["is_safe_to_consume"] is True


def test_small_spread_within_limit_accepted(direct_vm, direct_deploy, direct_owner):
    contract = deploy_guard(direct_deploy)
    feed_id = register(direct_vm, contract, direct_owner, spread=100)
    # spread = (2510-2500)/2500 * 10000 = 40 bps, under 100 bps limit.
    status = contract.submit_deterministic_quote(feed_id, ETH_2510, ETH_2500, "tight")
    assert status == "ACCEPTED"
    assert int(contract.get_latest_safe_price(feed_id)["latest_price"]) == (ETH_2510 + ETH_2500) // 2


def test_spread_breach_trips_and_does_not_poison_price(direct_vm, direct_deploy, direct_owner):
    contract = deploy_guard(direct_deploy)
    feed_id = register(direct_vm, contract, direct_owner, spread=100)
    # Seed a valid price first.
    contract.submit_deterministic_quote(feed_id, ETH_2500, ETH_2500, "seed")
    # Now divergent sources: spread = (2500-1000)/1000 * 10000 = 15000 bps.
    status = contract.submit_deterministic_quote(feed_id, ETH_2500, ETH_1000, "attack")
    assert status == "CIRCUIT_BROKEN_SPREAD"
    feed = contract.get_feed(feed_id)
    assert feed["circuit_broken"] is True
    safe = contract.get_latest_safe_price(feed_id)
    # latest_price must remain the trusted seed value, not the poisoned midpoint.
    assert int(safe["latest_price"]) == ETH_2500
    assert safe["is_safe_to_consume"] is False


def test_temporal_jump_breach_trips(direct_vm, direct_deploy, direct_owner):
    contract = deploy_guard(direct_deploy)
    feed_id = register(direct_vm, contract, direct_owner, spread=100, jump=200)
    contract.submit_deterministic_quote(feed_id, ETH_2500, ETH_2500, "seed")
    # Sources agree (spread 0) but midpoint jumped 800 bps above 2500.
    status = contract.submit_deterministic_quote(feed_id, ETH_2700, ETH_2700, "gap")
    assert status == "CIRCUIT_BROKEN_JUMP"
    safe = contract.get_latest_safe_price(feed_id)
    assert int(safe["latest_price"]) == ETH_2500
    assert contract.get_feed(feed_id)["circuit_broken"] is True


def test_update_blocked_while_circuit_broken(direct_vm, direct_deploy, direct_owner):
    contract = deploy_guard(direct_deploy)
    feed_id = register(direct_vm, contract, direct_owner, spread=100)
    contract.submit_deterministic_quote(feed_id, ETH_2500, ETH_2500, "seed")
    contract.submit_deterministic_quote(feed_id, ETH_2500, ETH_1000, "trip")
    with direct_vm.expect_revert("circuit breaker is tripped"):
        contract.submit_deterministic_quote(feed_id, ETH_2500, ETH_2500, "blocked")


def test_non_positive_quote_reverts(direct_vm, direct_deploy, direct_owner):
    contract = deploy_guard(direct_deploy)
    feed_id = register(direct_vm, contract, direct_owner)
    with direct_vm.expect_revert("must be positive"):
        contract.submit_deterministic_quote(feed_id, 0, ETH_2500, "zero")
    with direct_vm.expect_revert("must be positive"):
        contract.submit_deterministic_quote(feed_id, ETH_2500, 0, "zero")


def test_submit_unknown_feed_reverts(direct_vm, direct_deploy, direct_owner):
    contract = deploy_guard(direct_deploy)
    register(direct_vm, contract, direct_owner)
    with direct_vm.expect_revert("feed not found"):
        contract.submit_deterministic_quote(999, ETH_2500, ETH_2500, "nope")


# --------------------------------------------------------------------------
# Circuit breaker reset
# --------------------------------------------------------------------------


def test_reset_circuit_breaker_by_owner_recovers(direct_vm, direct_deploy, direct_owner):
    contract = deploy_guard(direct_deploy)
    feed_id = register(direct_vm, contract, direct_owner, spread=100)
    contract.submit_deterministic_quote(feed_id, ETH_2500, ETH_1000, "trip")
    assert contract.get_feed(feed_id)["circuit_broken"] is True
    contract.reset_circuit_breaker(feed_id)
    assert contract.get_feed(feed_id)["circuit_broken"] is False
    status = contract.submit_deterministic_quote(feed_id, ETH_2500, ETH_2500, "recovered")
    assert status == "ACCEPTED"


def test_reset_circuit_breaker_non_owner_reverts(direct_vm, direct_deploy, direct_owner, direct_bob):
    contract = deploy_guard(direct_deploy)
    feed_id = register(direct_vm, contract, direct_owner, spread=100)
    contract.submit_deterministic_quote(feed_id, ETH_2500, ETH_1000, "trip")
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("Only owner"):
        contract.reset_circuit_breaker(feed_id)


def test_reset_unknown_feed_reverts(direct_vm, direct_deploy, direct_owner):
    contract = deploy_guard(direct_deploy)
    register(direct_vm, contract, direct_owner)
    with direct_vm.expect_revert("feed not found"):
        contract.reset_circuit_breaker(42)


# --------------------------------------------------------------------------
# Views
# --------------------------------------------------------------------------


def test_get_round_returns_status_and_reporter(direct_vm, direct_deploy, direct_owner):
    contract = deploy_guard(direct_deploy)
    feed_id = register(direct_vm, contract, direct_owner)
    contract.submit_deterministic_quote(feed_id, ETH_2500, ETH_2500, "seed")
    rnd = contract.get_round(1)
    assert int(rnd["round_id"]) == 1
    assert int(rnd["feed_id"]) == int(feed_id)
    assert rnd["status"] == "ACCEPTED"
    assert int(rnd["median_price_scaled"]) == ETH_2500
    assert rnd["reporter"].startswith("0x")


def test_get_round_unknown_reverts(direct_vm, direct_deploy, direct_owner):
    contract = deploy_guard(direct_deploy)
    register(direct_vm, contract, direct_owner)
    with direct_vm.expect_revert("round not found"):
        contract.get_round(77)


def test_get_guard_summary_counts(direct_vm, direct_deploy, direct_owner):
    contract = deploy_guard(direct_deploy)
    feed_a = register(direct_vm, contract, direct_owner, spread=100)
    feed_b = contract.register_feed("BTC/USD", "https://a.test/b", "https://b.test/b", 100, 200)
    contract.submit_deterministic_quote(feed_a, ETH_2500, ETH_2500, "ok")
    contract.submit_deterministic_quote(feed_a, ETH_2500, ETH_1000, "trip")
    contract.submit_deterministic_quote(feed_b, 20 * SCALE, 20 * SCALE, "ok")
    summary = contract.get_guard_summary()
    assert int(summary["total_feeds"]) == 2
    assert int(summary["active_feeds"]) == 2
    assert int(summary["circuit_broken_feeds"]) == 1
    assert int(summary["total_rounds"]) == 3
    assert int(summary["accepted_rounds"]) == 2
    assert int(summary["tripped_rounds"]) == 1


def test_latest_safe_price_false_when_no_price(direct_vm, direct_deploy, direct_owner):
    contract = deploy_guard(direct_deploy)
    feed_id = register(direct_vm, contract, direct_owner)
    safe = contract.get_latest_safe_price(feed_id)
    assert int(safe["latest_price"]) == 0
    assert safe["is_safe_to_consume"] is False
    assert int(safe["rounds_count"]) == 0


# --------------------------------------------------------------------------
# Mocked live fetch comparative flow
# --------------------------------------------------------------------------


def _mock_fetch(direct_vm, source_a, source_b, note="live"):
    direct_vm.mock_web(r".*source-a\.test.*", {"status": 200, "body": "ETH/USD spot 2500.00"})
    direct_vm.mock_web(r".*source-b\.test.*", {"status": 200, "body": "ETH/USD spot 2500.00"})
    payload = {
        "source_a_scaled": source_a,
        "source_b_scaled": source_b,
        "attestation_note": note,
    }
    direct_vm.mock_llm(r".*spot-price oracle.*", json.dumps(payload))


def test_fetch_healthy_accepted(direct_vm, direct_deploy, direct_owner):
    contract = deploy_guard(direct_deploy)
    feed_id = register(direct_vm, contract, direct_owner, spread=100)
    _mock_fetch(direct_vm, ETH_2500, ETH_2500, "both sources agree")
    status = contract.fetch_and_verify_live_price(feed_id)
    assert status == "ACCEPTED"
    safe = contract.get_latest_safe_price(feed_id)
    assert int(safe["latest_price"]) == ETH_2500
    assert safe["is_safe_to_consume"] is True
    assert int(contract.get_round(1)["observed_spread_bps"]) == 0


def test_fetch_outlier_trips_spread_guard(direct_vm, direct_deploy, direct_owner):
    contract = deploy_guard(direct_deploy)
    feed_id = register(direct_vm, contract, direct_owner, spread=100)
    _mock_fetch(direct_vm, ETH_2500, ETH_1000, "one source far off")
    status = contract.fetch_and_verify_live_price(feed_id)
    assert status == "CIRCUIT_BROKEN_SPREAD"
    feed = contract.get_feed(feed_id)
    assert feed["circuit_broken"] is True
    # Never-poisoned price: no prior accepted round, so latest stays zero.
    assert int(contract.get_latest_safe_price(feed_id)["latest_price"]) == 0


def test_fetch_non_positive_hallucination_reverts(direct_vm, direct_deploy, direct_owner):
    contract = deploy_guard(direct_deploy)
    feed_id = register(direct_vm, contract, direct_owner, spread=100)
    _mock_fetch(direct_vm, 0, ETH_2500, "bad extraction")
    with direct_vm.expect_revert("not positive"):
        contract.fetch_and_verify_live_price(feed_id)


def test_fetch_blocked_while_broken(direct_vm, direct_deploy, direct_owner):
    contract = deploy_guard(direct_deploy)
    feed_id = register(direct_vm, contract, direct_owner, spread=100)
    contract.submit_deterministic_quote(feed_id, ETH_2500, ETH_1000, "trip")
    _mock_fetch(direct_vm, ETH_2500, ETH_2500, "ignored")
    with direct_vm.expect_revert("circuit breaker is tripped"):
        contract.fetch_and_verify_live_price(feed_id)


# --------------------------------------------------------------------------
# Keeper authorization (griefing gate)
# --------------------------------------------------------------------------


def test_submit_unauthorized_caller_reverts(direct_vm, direct_deploy, direct_owner, direct_bob):
    contract = deploy_guard(direct_deploy)
    feed_id = register(direct_vm, contract, direct_owner)
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("authorized keeper"):
        contract.submit_deterministic_quote(feed_id, ETH_2500, ETH_2500, "as bob")


def test_set_keeper_grant_and_revoke(direct_vm, direct_deploy, direct_owner, direct_bob):
    contract = deploy_guard(direct_deploy)
    feed_id = register(direct_vm, contract, direct_owner)
    # Bob is not a keeper yet.
    with direct_vm.prank(direct_bob):
        with direct_vm.expect_revert("authorized keeper"):
            contract.submit_deterministic_quote(feed_id, ETH_2500, ETH_2500, "before grant")
    # Owner grants bob.
    contract.set_keeper(addr_hex(direct_bob), True)
    with direct_vm.prank(direct_bob):
        assert contract.submit_deterministic_quote(feed_id, ETH_2500, ETH_2500, "after grant") == "ACCEPTED"
    # Owner revokes bob.
    contract.set_keeper(addr_hex(direct_bob), False)
    with direct_vm.prank(direct_bob):
        with direct_vm.expect_revert("authorized keeper"):
            contract.submit_deterministic_quote(feed_id, ETH_2510, ETH_2510, "after revoke")


def test_set_keeper_non_owner_reverts(direct_vm, direct_deploy, direct_owner, direct_bob):
    contract = deploy_guard(direct_deploy)
    register(direct_vm, contract, direct_owner)
    with direct_vm.prank(direct_bob):
        with direct_vm.expect_revert("Only owner can manage keepers"):
            contract.set_keeper(addr_hex(direct_owner), True)


def test_set_keeper_invalid_address_reverts(direct_vm, direct_deploy, direct_owner):
    contract = deploy_guard(direct_deploy)
    register(direct_vm, contract, direct_owner)
    with direct_vm.expect_revert("keeper address"):
        contract.set_keeper("not-an-address", True)
    with direct_vm.expect_revert("keeper address"):
        contract.set_keeper("0x123", True)


# --------------------------------------------------------------------------
# Feed active lifecycle (no dead field)
# --------------------------------------------------------------------------


def test_set_feed_active_blocks_and_restores(direct_vm, direct_deploy, direct_owner):
    contract = deploy_guard(direct_deploy)
    feed_id = register(direct_vm, contract, direct_owner)
    contract.submit_deterministic_quote(feed_id, ETH_2500, ETH_2500, "seed")
    contract.set_feed_active(feed_id, False)
    assert contract.get_feed(feed_id)["active"] is False
    with direct_vm.expect_revert("feed is inactive"):
        contract.submit_deterministic_quote(feed_id, ETH_2500, ETH_2500, "blocked")
    assert contract.get_latest_safe_price(feed_id)["is_safe_to_consume"] is False
    contract.set_feed_active(feed_id, True)
    assert contract.submit_deterministic_quote(feed_id, ETH_2510, ETH_2510, "restored") == "ACCEPTED"


def test_set_feed_active_non_owner_reverts(direct_vm, direct_deploy, direct_owner, direct_bob):
    contract = deploy_guard(direct_deploy)
    feed_id = register(direct_vm, contract, direct_owner)
    with direct_vm.prank(direct_bob):
        with direct_vm.expect_revert("Only owner can change feed activity"):
            contract.set_feed_active(feed_id, False)


def test_set_feed_active_unknown_reverts(direct_vm, direct_deploy, direct_owner):
    contract = deploy_guard(direct_deploy)
    register(direct_vm, contract, direct_owner)
    with direct_vm.expect_revert("feed not found"):
        contract.set_feed_active(99, False)


# --------------------------------------------------------------------------
# String length and URL hygiene bounds
# --------------------------------------------------------------------------


def test_register_symbol_too_long_reverts(direct_vm, direct_deploy, direct_owner):
    contract = deploy_guard(direct_deploy)
    direct_vm.sender = direct_owner
    with direct_vm.expect_revert("symbol"):
        contract.register_feed("S" * 33, PRIMARY, SECONDARY, 100, 200)


def test_register_url_too_long_reverts(direct_vm, direct_deploy, direct_owner):
    contract = deploy_guard(direct_deploy)
    direct_vm.sender = direct_owner
    long_url = "https://a.test/" + "p" * 600
    with direct_vm.expect_revert("primary_url"):
        contract.register_feed("ETH/USD", long_url, SECONDARY, 100, 200)


def test_register_url_too_short_reverts(direct_vm, direct_deploy, direct_owner):
    contract = deploy_guard(direct_deploy)
    direct_vm.sender = direct_owner
    with direct_vm.expect_revert("primary_url"):
        contract.register_feed("ETH/USD", "http://", SECONDARY, 100, 200)


def test_register_url_with_whitespace_reverts(direct_vm, direct_deploy, direct_owner):
    contract = deploy_guard(direct_deploy)
    direct_vm.sender = direct_owner
    with direct_vm.expect_revert("primary_url"):
        contract.register_feed("ETH/USD", "https://a.test/eth usd", SECONDARY, 100, 200)
    with direct_vm.expect_revert("primary_url"):
        contract.register_feed("ETH/USD", "https://a.test/eth\nusd", SECONDARY, 100, 200)


def test_submit_note_too_long_reverts(direct_vm, direct_deploy, direct_owner):
    contract = deploy_guard(direct_deploy)
    feed_id = register(direct_vm, contract, direct_owner)
    with direct_vm.expect_revert("note"):
        contract.submit_deterministic_quote(feed_id, ETH_2500, ETH_2500, "n" * 257)


def test_fetch_out_of_range_price_reverts(direct_vm, direct_deploy, direct_owner):
    contract = deploy_guard(direct_deploy)
    feed_id = register(direct_vm, contract, direct_owner, spread=100)
    _mock_fetch(direct_vm, 2 * 10 ** 18, ETH_2500, "huge")
    with direct_vm.expect_revert("out of range"):
        contract.fetch_and_verify_live_price(feed_id)
