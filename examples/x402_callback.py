# SPDX-License-Identifier: Apache-2.0
"""Runnable host for the worker, with an app-side x402 payment callback.

The callback and its key belong to the application, not the plugin. The worker only
hands the callback a challenge and attaches the headers it returns.

Setup, from the repository root:

    uv sync --group example
    uv run nemo-relay plugins add ./relay-plugin.toml
    uv run nemo-relay plugins enable plagtech.spraay_batch

Relay refuses unsigned plugins by default, so see the README for the attestation setting.

By default this declines the challenge and spends nothing, and it needs no wallet:

    uv run python examples/x402_callback.py

``--pay`` pays the $0.02 access fee once, reading SPRAAY_WALLET_PRIVATE_KEY from the
process environment, then ./.env. The returned batch transaction is never signed or
broadcast by this example, so no batch funds move.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import nemo_relay
from eth_account import Account
from eth_account.signers.local import LocalAccount
from nemo_relay import plugin, subscribers, tools
from x402 import x402Client
from x402.http.x402_http_client import x402HTTPClient
from x402.mechanisms.evm.exact import register_exact_evm_client
from x402.mechanisms.evm.signers import EthAccountSigner
from x402.schemas import PaymentRequired

TOOL = "spraay_build_batch"
CHALLENGE_ARG = "spraay_payment_challenge"
BASE_RPC = "https://mainnet.base.org"

# The only access fee this callback will pay. Anything else is declined before signing.
PAY_NETWORK = "eip155:8453"
PAY_ASSET = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
PAY_TO = "0xAd62f03C7514bb8c51f1eA70C2b75C37404695c8"
MAX_FEE_RAW = 20000

# Without --pay no address is used for anything but the free checks.
NO_WALLET_SENDER = "0x000000000000000000000000000000000000dEaD"
SECOND_RECIPIENT = "0x000000000000000000000000000000000000dEaD"
FIRST_RECIPIENT_NO_WALLET = "0x1111111111111111111111111111111111111111"


class X402Callback:
    """The managed tool's callback, used as the payment hook. Pays at most once."""

    def __init__(self, account: LocalAccount | None) -> None:
        self._account = account
        self.paid_headers: dict[str, str] = {}
        self.declined_reason: str | None = None

    async def __call__(self, args: dict[str, Any]) -> nemo_relay.ToolExecutionResult:
        challenge = args.get(CHALLENGE_ARG)
        if not isinstance(challenge, dict):
            raise RuntimeError("the worker runs the tool; this callback is only called with a payment challenge")
        headers = await self._pay(challenge)
        return nemo_relay.ToolExecutionResult({"headers": headers} if headers else {}, None)

    async def _pay(self, challenge: dict[str, Any]) -> dict[str, str] | None:
        if self._account is None:
            self.declined_reason = "no --pay"
            return None
        if self.paid_headers:
            self.declined_reason = "already paid once"
            return None
        option = next((item for item in challenge["accepts"] if item.get("network") == PAY_NETWORK), None)
        if option is None or not _is_expected_fee(option):
            self.declined_reason = "no matching payment option"
            return None

        body = challenge["body"]
        required = PaymentRequired.model_validate(
            {
                "x402Version": body["x402Version"],
                "error": body.get("error"),
                "resource": body.get("resource"),
                "accepts": [option],
                "extensions": body.get("extensions"),
            }
        )
        client = x402Client()
        register_exact_evm_client(client, EthAccountSigner(self._account))
        payload = await client.create_payment_payload(required)
        self.paid_headers = x402HTTPClient(client).encode_payment_signature_header(payload)
        return self.paid_headers


def _is_expected_fee(option: dict[str, Any]) -> bool:
    try:
        amount = int(option["amount"])
    except (KeyError, TypeError, ValueError):
        return False
    return (
        option.get("scheme") == "exact"
        and option.get("asset") == PAY_ASSET
        and option.get("payTo") == PAY_TO
        and 0 < amount <= MAX_FEE_RAW
    )


def load_account() -> LocalAccount:
    key = os.environ.get("SPRAAY_WALLET_PRIVATE_KEY", "")
    env_file = Path(".env")
    if not key and env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            name, sep, value = line.strip().removeprefix("export ").partition("=")
            if sep and name.strip() == "SPRAAY_WALLET_PRIVATE_KEY":
                key = value.strip().strip("'\"")
    if not key:
        raise SystemExit("SPRAAY_WALLET_PRIVATE_KEY is not set.")
    try:
        return Account.from_key(key if key.startswith("0x") else f"0x{key}")
    except Exception:
        # Do not chain or echo the original error: it could reference key material.
        pass
    raise SystemExit("SPRAAY_WALLET_PRIVATE_KEY is not a valid EVM private key.")


def usdc_balance(address: str) -> int:
    call = {"to": PAY_ASSET, "data": "0x70a08231" + address[2:].lower().rjust(64, "0")}
    request = {"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": [call, "latest"]}
    reply = httpx.post(BASE_RPC, json=request, timeout=30)
    reply.raise_for_status()
    return int(reply.json()["result"], 16)


def event_summary(event: Any) -> dict[str, Any]:
    metadata = event.metadata if isinstance(event.metadata, dict) else {}
    return {
        "name": event.name,
        "scope_category": getattr(event, "scope_category", None),
        "data": event.data,
        "spraay_metadata": {key: value for key, value in metadata.items() if key.startswith("spraay.")},
    }


async def run(account: LocalAccount | None) -> tuple[dict[str, Any], list[dict[str, Any]], X402Callback, str]:
    sender = account.address if account else NO_WALLET_SENDER
    first = account.address if account else FIRST_RECIPIENT_NO_WALLET
    arguments = {
        "chain": "base",
        "token": "USDC",
        "sender": sender,
        "payments": [{"to": first, "amount": "0.01"}, {"to": SECOND_RECIPIENT, "amount": "0.02"}],
    }
    callback = X402Callback(account)
    events: list[Any] = []
    subscribers.register("example_capture", events.append)
    try:
        async with await plugin.initialize(plugin.PluginConfig(components=[])) as activation:
            selected = [item["plugin_id"] for item in activation.report["dynamic_plugins"] if item["selected"]]
            if "plagtech.spraay_batch" not in selected:
                raise SystemExit("The worker is not enabled. See the setup steps at the top of this file.")
            outcome = await tools.execute(TOOL, arguments, callback)
            await subscribers.flush_async()
            await asyncio.sleep(0.5)  # Let the worker's spraay.batch.built mark arrive.
            await subscribers.flush_async()
    finally:
        subscribers.deregister("example_capture")
    relevant = [
        summary
        for summary in (event_summary(event) for event in events)
        if summary["name"] == "spraay.batch.built"
        or (summary["name"] == TOOL and summary["scope_category"] in ("start", "end"))
    ]
    raw_events = json.dumps([event_summary(event) for event in events], default=str)
    return outcome.result, relevant, callback, raw_events


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the worker with an x402 payment callback.")
    parser.add_argument("--pay", action="store_true", help="pay the $0.02 access fee once")
    parser.add_argument("--record", type=Path, help="with --pay, write a proof record here; refuses to overwrite")
    args = parser.parse_args()
    if args.record and (not args.pay or args.record.exists()):
        raise SystemExit("--record needs --pay and a path that does not exist yet.")

    account = load_account() if args.pay else None
    before = None
    if account:
        print("payer address:", account.address)
        before = usdc_balance(account.address)
        print("USDC before (raw):", before)
        if before < MAX_FEE_RAW:
            raise SystemExit("Wallet holds less than the access fee.")

    result, events, callback, raw_events = asyncio.run(run(account))
    print("status:", result["status"])
    if result["status"] == "payment_required":
        print("declined:", callback.declined_reason, "| nothing spent")
        return 0

    # Nothing has been signed. A real app approves result["approval_required"], then signs
    # and broadcasts result["transaction"]. This example does neither.
    print(json.dumps(result, indent=2))
    secret_values = list(callback.paid_headers.values())
    leaked = any(value in raw_events for value in secret_values)
    addresses = [account.address] if account else []
    exposed = any(address[2:].lower() in raw_events.lower() for address in addresses)
    after = usdc_balance(account.address) if account else None
    print("USDC after (raw):", after, "| spent:", (before - after) if before is not None else None)
    print("payment header in events:", leaked, "| wallet address in events:", exposed)
    if leaked or exposed:
        print("Refusing to record: sensitive values reached events.", file=sys.stderr)
        return 2
    if args.record:
        record = {
            "captured_at": datetime.now(UTC).isoformat(),
            "path": "Relay Python SDK host -> gRPC -> worker -> gateway, with the host callback paying x402",
            "result": result,
            "events": events,
            "usdc_raw_before": before,
            "usdc_raw_after": after,
            "payment_header_in_events": leaked,
            "wallet_address_in_events": exposed,
            "signed_or_broadcast_batch_tx": False,
        }
        args.record.write_text(json.dumps(record, indent=2, default=str) + "\n", encoding="utf-8")
        print("recorded:", args.record)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
