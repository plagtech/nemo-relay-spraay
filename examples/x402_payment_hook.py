# SPDX-License-Identifier: Apache-2.0
"""Example app-side payment hook, using the x402 SDK (``uv sync --group live``).

This code belongs to the application, not the plugin: the app owns the key and decides
what it will pay. The plugin only passes the hook a challenge and attaches the headers.
"""

from __future__ import annotations

from eth_account.signers.local import LocalAccount
from x402 import x402Client
from x402.http.x402_http_client import x402HTTPClient
from x402.mechanisms.evm.exact import register_exact_evm_client
from x402.mechanisms.evm.signers import EthAccountSigner
from x402.schemas import PaymentRequired

from nemo_relay_spraay import PaymentChallenge


def make_x402_hook(account: LocalAccount, *, network: str = "eip155:8453"):
    """Return a hook that pays the access fee from ``account`` on one network."""
    client = x402Client()
    register_exact_evm_client(client, EthAccountSigner(account))

    async def pay(challenge: PaymentChallenge) -> dict[str, str]:
        options = [item for item in challenge.accepts if item.get("network") == network]
        if not options:
            raise RuntimeError(f"gateway offered no payment option on {network}")
        required = PaymentRequired.model_validate(
            {
                "x402Version": challenge.body["x402Version"],
                "error": challenge.body.get("error"),
                "resource": challenge.body.get("resource"),
                "accepts": options[:1],
                "extensions": challenge.body.get("extensions"),
            }
        )
        payload = await client.create_payment_payload(required)
        return x402HTTPClient(client).encode_payment_signature_header(payload)

    return pay
