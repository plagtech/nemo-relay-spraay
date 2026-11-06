# SPDX-License-Identifier: Apache-2.0
"""Runnable demo against the live gateway. Spends nothing and needs no wallet.

Shows the guardrail refusing bad batches, then a valid batch passing the free
preflight checks and stopping at the x402 challenge, because no payment hook is set.
Pass a payment hook to SpraayGateway to receive the unsigned transaction instead.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile

from nemo_relay import plugin, subscribers

from nemo_relay_spraay import (
    PLUGIN_KIND,
    PaymentRequiredError,
    SpraayGateway,
    SpraayPaymentPlugin,
    build_batch_transaction,
    component,
)

SENDER = "0x000000000000000000000000000000000000dEaD"
ALICE = "0x1111111111111111111111111111111111111111"
BOB = "0x2222222222222222222222222222222222222222"
GOOD = [{"to": ALICE, "amount": "1.50"}, {"to": BOB, "amount": "2.25"}]


async def attempt(gateway: SpraayGateway, label: str, **kwargs: object) -> None:
    arguments = {"chain": "base", "token": "USDC", "sender": SENDER, "payments": GOOD, "decimals": 6, **kwargs}
    try:
        outcome = await build_batch_transaction(gateway, **arguments)
    except PaymentRequiredError as error:
        option = error.challenge.accepts[0]
        print(f"[{label}] preflight passed, x402 fee required: {option['amount']} raw on {option['network']}")
    except RuntimeError as error:
        print(f"[{label}] blocked: {error}")
    else:
        print(f"[{label}] unsigned transaction:", json.dumps(outcome.result["transaction"]))


async def main() -> None:
    os.chdir(tempfile.mkdtemp())
    os.environ.setdefault("XDG_CONFIG_HOME", os.getcwd())

    implementation = SpraayPaymentPlugin()
    plugin.register(PLUGIN_KIND, implementation)
    activation = await plugin.initialize(plugin.PluginConfig(components=[component()]))
    try:
        async with SpraayGateway() as gateway:
            print("gateway:", gateway.url)
            await attempt(gateway, "chain not enabled", chain="solana")
            await attempt(gateway, "bad address", payments=[{"to": "0xnope", "amount": "1"}])
            await attempt(gateway, "too many", payments=[{"to": ALICE, "amount": "1"}] * 201)
            await attempt(gateway, "base", chain="base")
            await attempt(gateway, "peaq", chain="peaq")
        await subscribers.flush_async()
    finally:
        await activation.close()
        plugin.deregister(PLUGIN_KIND)


if __name__ == "__main__":
    asyncio.run(main())
