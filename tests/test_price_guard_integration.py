"""Studionet integration test for MultiSourcePriceGuard.

Run against the live network with:

    gltest tests/test_price_guard_integration.py --network studionet

This exercises full consensus (leader plus validators) on real transactions. The
deterministic guard engine is driven on chain; the live web/LLM fetch method is
covered by the mocked leader path in the direct suite and by consensus on the
deterministic path here.
"""

import json

from gltest import get_contract_factory
from gltest.assertions import tx_execution_succeeded

CONTRACT_FILE = "multi_source_price_guard.py"
SCALE = 10 ** 8
ETH_2500 = 2500 * SCALE
ETH_1000 = 1000 * SCALE
PRIMARY = "https://source-a.example/eth-usd"
SECONDARY = "https://source-b.example/eth-usd"


def _decoded_return(receipt):
    """Unwrap the readable return value: ids to int, JSON-quoted strings unwrapped."""
    leader = receipt["consensus_data"]["leader_receipt"][0]
    readable = ((leader.get("result") or {}).get("payload") or {}).get("readable")
    if readable is None:
        return None
    try:
        return json.loads(readable)
    except (ValueError, TypeError):
        return readable


def test_full_guard_scenario_on_studionet():
    factory = get_contract_factory(contract_file_path=CONTRACT_FILE)
    contract = factory.deploy(args=[])

    register_receipt = contract.register_feed(
        args=["ETH/USD", PRIMARY, SECONDARY, 100, 200]
    ).transact()
    assert tx_execution_succeeded(register_receipt)
    feed_id = int(_decoded_return(register_receipt))
    assert feed_id == 1

    accepted = contract.submit_deterministic_quote(
        args=[feed_id, ETH_2500, ETH_2500, "keeper seed"]
    ).transact()
    assert tx_execution_succeeded(accepted)
    assert _decoded_return(accepted) == "ACCEPTED"

    safe = contract.get_latest_safe_price(args=[feed_id]).call()
    assert int(safe["latest_price"]) == ETH_2500
    assert safe["is_safe_to_consume"] is True

    trip = contract.submit_deterministic_quote(
        args=[feed_id, ETH_2500, ETH_1000, "divergent attack"]
    ).transact()
    assert tx_execution_succeeded(trip)
    assert _decoded_return(trip) == "CIRCUIT_BROKEN_SPREAD"

    # The poisoned midpoint must never reach the consumed price.
    after_trip = contract.get_latest_safe_price(args=[feed_id]).call()
    assert int(after_trip["latest_price"]) == ETH_2500
    assert after_trip["circuit_broken"] is True
    assert after_trip["is_safe_to_consume"] is False

    # A write while the breaker is tripped reverts: execution fails and state is
    # unchanged, which is the expected rollback behavior on a live network.
    rollback = contract.submit_deterministic_quote(
        args=[feed_id, ETH_2500, ETH_2500, "blocked"]
    ).transact()
    assert not tx_execution_succeeded(rollback)

    reset = contract.reset_circuit_breaker(args=[feed_id]).transact()
    assert tx_execution_succeeded(reset)

    recovered = contract.get_latest_safe_price(args=[feed_id]).call()
    assert recovered["circuit_broken"] is False

    summary = contract.get_guard_summary().call()
    assert int(summary["total_feeds"]) == 1
    assert int(summary["circuit_broken_feeds"]) == 0
    assert int(summary["total_rounds"]) == 2
    assert int(summary["accepted_rounds"]) == 1
    assert int(summary["tripped_rounds"]) == 1
