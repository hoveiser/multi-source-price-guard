#!/usr/bin/env python3
"""Deploy MultiSourcePriceGuard to GenLayer studionet and record on-chain evidence.

The script deploys the contract, drives a deterministic scenario through real
transactions, proves the not-poisoned invariant, waits for FINALIZED on every
transaction, verifies byte-for-byte sha256 identity between the local source and
the on-chain code returned by gen_getContractCode, and writes the full evidence
to evidence/studionet_deployment.json.

It reads GENLAYER_PRIVATE_KEY from this project's own .env and never prints it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import dotenv_values
from eth_account import Account
from genlayer_py import create_client, studionet
from genlayer_py.types import TransactionStatus

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "contracts" / "multi_source_price_guard.py"
EVIDENCE_PATH = ROOT / "evidence" / "studionet_deployment.json"
EXPLORER_BASE = "https://genlayer-explorer.vercel.app"

SCALE = 10 ** 8
ETH_2500 = 2500 * SCALE
ETH_1000 = 1000 * SCALE

PRIMARY_URL = "https://source-a.example/eth-usd"
SECONDARY_URL = "https://source-b.example/eth-usd"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _load_private_key() -> str:
    cfg = dotenv_values(ROOT / ".env")
    pk = cfg.get("GENLAYER_PRIVATE_KEY")
    if not pk:
        raise SystemExit("GENLAYER_PRIVATE_KEY is missing from .env")
    return pk


def _leader_receipt(receipt: dict) -> dict:
    return receipt["consensus_data"]["leader_receipt"][0]


def _unwrap(raw):
    """Coerce a readable return value: chained ids to int, JSON-quoted strings unwrapped."""
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return raw


def _readable_result(receipt: dict):
    result = _leader_receipt(receipt).get("result")
    if not isinstance(result, dict):
        return None
    payload = result.get("payload")
    if isinstance(payload, dict):
        return payload.get("readable")
    # On a revert or panic the payload is a plain message string rather than a dict.
    if isinstance(payload, str):
        return payload
    return None


def _execution_ok(receipt: dict) -> bool:
    return _leader_receipt(receipt).get("execution_result") == "SUCCESS"


def _onchain_code(client, address: str) -> bytes:
    response = client.provider.make_request("gen_getContractCode", [address])
    raw = response["result"] if isinstance(response, dict) else response
    if isinstance(raw, bytes):
        return raw
    if isinstance(raw, str):
        text = raw[2:] if raw.startswith("0x") else raw
        if raw.startswith("0x"):
            return bytes.fromhex(text)
        return base64.b64decode(text)
    raise RuntimeError(f"unexpected gen_getContractCode type: {type(raw)}")


def main() -> int:
    pk = _load_private_key()
    account = Account.from_key(pk)
    client = create_client(chain=studionet, account=account)

    source = CONTRACT_PATH.read_bytes()
    local_sha256 = hashlib.sha256(source).hexdigest()

    evidence = {
        "network": "studionet",
        "rpc": studionet.rpc_urls["default"]["http"][0],
        "explorer_base": EXPLORER_BASE,
        "deployer_address": account.address,
        "contract_path": str(CONTRACT_PATH.relative_to(ROOT)),
        "local_sha256": local_sha256,
        "generated_at": _utcnow(),
        "transactions": [],
    }

    def record(label: str, tx_hash: str, expect_success: bool):
        receipt = client.wait_for_transaction_receipt(
            tx_hash, status=TransactionStatus.FINALIZED, retries=60, interval=3000
        )
        readable = _readable_result(receipt)
        unwrapped = _unwrap(readable)
        ok = _execution_ok(receipt)
        status_name = receipt.get("status_name")
        result_name = receipt.get("result_name")
        entry = {
            "label": label,
            "tx_hash": tx_hash,
            "status_name": status_name,
            "consensus_result": result_name,
            "execution_result": _leader_receipt(receipt).get("execution_result"),
            "readable_return": readable,
            "decoded_return": unwrapped,
            "explorer_url": f"{EXPLORER_BASE}/#/tx/{tx_hash}",
            "verified_finalized": status_name == "FINALIZED",
            "expected_success": expect_success,
            "matches_expectation": ok == expect_success,
        }
        evidence["transactions"].append(entry)
        print(
            f"[{label}] status={status_name} consensus={result_name} "
            f"exec={entry['execution_result']} return={unwrapped!r}"
        )
        if status_name != "FINALIZED":
            raise SystemExit(f"{label} did not finalize: {status_name}")
        if ok != expect_success:
            raise SystemExit(f"{label} execution mismatch: got {ok}, expected {expect_success}")
        return entry

    print("Deploying contract to studionet...")
    deploy_hash = client.deploy_contract(code=source.decode("utf-8"), args=[])
    print(f"deploy tx: {deploy_hash}")
    deploy_entry = record("deploy", deploy_hash, expect_success=True)

    deploy_receipt = client.wait_for_transaction_receipt(
        deploy_hash, status=TransactionStatus.FINALIZED, retries=60, interval=3000
    )
    contract_address = deploy_receipt.get("to_address")
    if not contract_address:
        raise SystemExit("could not resolve deployed contract address")
    evidence["contract_address"] = contract_address
    print(f"contract address: {contract_address}")

    # Byte-for-byte source identity between local file and on-chain code.
    onchain = _onchain_code(client, contract_address)
    onchain_sha256 = hashlib.sha256(onchain).hexdigest()
    evidence["onchain_sha256"] = onchain_sha256
    evidence["sha256_identity"] = onchain_sha256 == local_sha256
    print(f"sha256 local={local_sha256}")
    print(f"sha256 onchain={onchain_sha256} identity={onchain_sha256 == local_sha256}")
    if onchain_sha256 != local_sha256:
        raise SystemExit("deployed code does not match local source")

    # Scenario: register a feed.
    reg_hash = client.write_contract(
        contract_address,
        "register_feed",
        args=["ETH/USD", PRIMARY_URL, SECONDARY_URL, 100, 200],
        account=account,
    )
    reg_entry = record("register_feed", reg_hash, expect_success=True)
    feed_id = int(reg_entry["decoded_return"])

    # Accepted deterministic quote seeds the trusted price.
    acc_hash = client.write_contract(
        contract_address,
        "submit_deterministic_quote",
        args=[feed_id, ETH_2500, ETH_2500, "keeper seed"],
        account=account,
    )
    acc_entry = record("submit_accepted", acc_hash, expect_success=True)
    if acc_entry["decoded_return"] != "ACCEPTED":
        raise SystemExit("seed quote was not accepted")

    # Spread breach trips the breaker and must not poison latest_price.
    trip_hash = client.write_contract(
        contract_address,
        "submit_deterministic_quote",
        args=[feed_id, ETH_2500, ETH_1000, "divergent attack"],
        account=account,
    )
    trip_entry = record("submit_spread_trip", trip_hash, expect_success=True)
    if trip_entry["decoded_return"] != "CIRCUIT_BROKEN_SPREAD":
        raise SystemExit("spread breach did not trip the breaker")

    # Prove latest_price is still the trusted seed and the breaker is tripped.
    safe = client.read_contract(contract_address, "get_latest_safe_price", args=[feed_id], account=account)
    print(f"after trip, latest_safe_price={safe}")
    if int(safe["latest_price"]) != ETH_2500 or safe["circuit_broken"] is not True:
        raise SystemExit("invariant broken: latest_price was poisoned or breaker not tripped")
    if safe["is_safe_to_consume"] is not False:
        raise SystemExit("invariant broken: a tripped feed reported itself as safe")

    # Expected rollback: any update while the breaker is tripped must revert.
    rb_hash = client.write_contract(
        contract_address,
        "submit_deterministic_quote",
        args=[feed_id, ETH_2500, ETH_2500, "blocked while broken"],
        account=account,
    )
    record("rollback_while_broken", rb_hash, expect_success=False)

    # Owner resets the breaker and the feed becomes usable again.
    reset_hash = client.write_contract(
        contract_address, "reset_circuit_breaker", args=[feed_id], account=account
    )
    record("reset_circuit_breaker", reset_hash, expect_success=True)

    # Final on-chain state snapshot.
    evidence["final_state"] = {
        "get_feed": client.read_contract(contract_address, "get_feed", args=[feed_id], account=account),
        "get_latest_safe_price": client.read_contract(
            contract_address, "get_latest_safe_price", args=[feed_id], account=account
        ),
        "get_guard_summary": client.read_contract(
            contract_address, "get_guard_summary", account=account
        ),
    }
    print(f"final summary: {evidence['final_state']['get_guard_summary']}")

    evidence["all_transactions_finalized"] = all(
        t["verified_finalized"] for t in evidence["transactions"]
    )
    EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE_PATH.write_text(json.dumps(evidence, indent=2, default=str), encoding="utf-8")
    print(f"evidence written to {EVIDENCE_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
