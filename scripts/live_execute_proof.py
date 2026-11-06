# SPDX-License-Identifier: Apache-2.0
"""One-shot live proof of POST /api/v1/batch/execute on the Spraay gateway.

Dry run by default: loads the key, checks the wallet and the 402 challenge, and
signs nothing. ``--pay`` signs one x402 authorization for the $0.02 access fee.

The response is an unsigned batch transaction. This script never signs or
broadcasts it, so no batch funds move. The only spend is the access fee.

The key is read from SPRAAY_WALLET_PRIVATE_KEY (process env, then ./.env). It is
never printed, logged, or written to the proof record. Requires the ``live``
dependency group.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
from eth_account import Account
from x402 import x402Client
from x402.http.x402_http_client import x402HTTPClient
from x402.mechanisms.evm.exact import register_exact_evm_client
from x402.mechanisms.evm.signers import EthAccountSigner
from x402.schemas import PaymentRequired

GATEWAY = "https://gateway.spraay.app"
EXECUTE_URL = f"{GATEWAY}/api/v1/batch/execute"
BASE_RPC = "https://mainnet.base.org"
PROOF_PATH = Path("docs/live-execute-proof.json")
ATTEMPT_PATH = Path("docs/live-execute-attempt.json")

# The only challenge this script will pay. Anything else aborts before signing.
EXPECTED_NETWORK = "eip155:8453"
EXPECTED_AMOUNT = "20000"
EXPECTED_ASSET = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
EXPECTED_PAY_TO = "0xAd62f03C7514bb8c51f1eA70C2b75C37404695c8"

SECOND_RECIPIENT = "0x000000000000000000000000000000000000dEaD"
RECORDED_RESPONSE_HEADERS = ("content-type", "payment-response", "x-payment-response", "x-spraay-meta")


def load_key() -> str:
    key = os.environ.get("SPRAAY_WALLET_PRIVATE_KEY", "")
    env_file = Path(".env")
    if not key and env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            name, sep, value = line.strip().removeprefix("export ").partition("=")
            if sep and name.strip() == "SPRAAY_WALLET_PRIVATE_KEY":
                key = value.strip().strip("'\"")
    if not key:
        sys.exit("SPRAAY_WALLET_PRIVATE_KEY is not set.")
    return key if key.startswith("0x") else f"0x{key}"


def load_account():
    try:
        return Account.from_key(load_key())
    except Exception:
        # Do not chain or echo the original error: it could reference key material.
        pass
    raise SystemExit("SPRAAY_WALLET_PRIVATE_KEY is not a valid EVM private key.")


def usdc_balance(http: httpx.Client, address: str) -> int:
    call = {"to": EXPECTED_ASSET, "data": "0x70a08231" + address[2:].lower().rjust(64, "0")}
    reply = http.post(BASE_RPC, json={"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": [call, "latest"]})
    reply.raise_for_status()
    return int(reply.json()["result"], 16)


def expected_requirement(challenge: dict) -> dict:
    """Return the one acceptable requirement, or abort before anything is signed."""
    for item in challenge.get("accepts", []):
        if item.get("network") == EXPECTED_NETWORK:
            actual = (item.get("scheme"), item.get("amount"), item.get("asset"), item.get("payTo"))
            wanted = ("exact", EXPECTED_AMOUNT, EXPECTED_ASSET, EXPECTED_PAY_TO)
            if actual != wanted:
                sys.exit(f"Refusing to pay: unexpected Base requirement {actual}")
            return item
    sys.exit("Refusing to pay: no Base requirement in challenge.")


def decode_header(value: str | None):
    if not value:
        return None
    try:
        return json.loads(base64.b64decode(value + "=" * (-len(value) % 4)))
    except ValueError:
        return value


async def payment_headers(account, challenge: dict, requirement: dict) -> dict[str, str]:
    client = x402Client()
    register_exact_evm_client(client, EthAccountSigner(account))
    required = PaymentRequired.model_validate(
        {
            "x402Version": challenge["x402Version"],
            "error": challenge.get("error"),
            "resource": challenge.get("resource"),
            "accepts": [requirement],
            "extensions": challenge.get("extensions"),
        }
    )
    payload = await client.create_payment_payload(required)
    return x402HTTPClient(client).encode_payment_signature_header(payload)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pay", action="store_true", help="sign and pay the $0.02 access fee once")
    args = parser.parse_args()

    if args.pay and PROOF_PATH.exists():
        sys.exit(f"{PROOF_PATH} already exists: the one paid proof has been recorded.")

    account = load_account()
    print("payer address:", account.address)

    body = {
        "token": "USDC",
        "chain": "base",
        "sender": account.address,
        "recipients": [account.address, SECOND_RECIPIENT],
        "amounts": ["10000", "20000"],
    }

    with httpx.Client(timeout=60) as http:
        before = usdc_balance(http, account.address)
        print("USDC before (raw):", before)
        if before < int(EXPECTED_AMOUNT):
            sys.exit("Wallet holds less than the 20000 raw USDC access fee.")

        unpaid = http.post(EXECUTE_URL, json=body)
        if unpaid.status_code != 402:
            sys.exit(f"Expected 402 from unpaid call, got {unpaid.status_code}")
        challenge = unpaid.json()
        requirement = expected_requirement(challenge)
        print("challenge OK:", requirement["network"], requirement["amount"], requirement["payTo"])

        if not args.pay:
            print("Dry run complete. Nothing signed, nothing spent.")
            return 0

        headers = asyncio.run(payment_headers(account, challenge, requirement))
        paid = http.post(EXECUTE_URL, json=body, headers=headers)
        after = usdc_balance(http, account.address)

    record = {
        "captured_at": datetime.now(UTC).isoformat(),
        "gateway_version_endpoint": f"{GATEWAY}/health",
        "request": {"method": "POST", "url": EXECUTE_URL, "json": body},
        "challenge_paid": requirement,
        "response": {
            "status": paid.status_code,
            "headers": {name: paid.headers[name] for name in RECORDED_RESPONSE_HEADERS if name in paid.headers},
            "payment_response_decoded": decode_header(
                paid.headers.get("payment-response") or paid.headers.get("x-payment-response")
            ),
            "json": paid.json() if "json" in paid.headers.get("content-type", "") else None,
            "text": None if "json" in paid.headers.get("content-type", "") else paid.text[:2000],
        },
        "usdc_raw_before": before,
        "usdc_raw_after": after,
        "signed_or_broadcast_batch_tx": False,
    }
    target = PROOF_PATH if paid.status_code == 200 else ATTEMPT_PATH
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(record["response"], indent=2))
    print("USDC after (raw):", after, "| spent:", before - after)
    print("recorded:", target)
    return 0 if paid.status_code == 200 else 2


if __name__ == "__main__":
    raise SystemExit(main())
