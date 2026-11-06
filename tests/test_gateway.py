# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
from importlib import metadata
from typing import Any

import httpx
import pytest
from conftest import PROOF, RECIPIENTS, SENDER, pay_hook

from nemo_relay_spraay import (
    AccessFeeTooHighError,
    GatewayResponseError,
    PaymentChallenge,
    PaymentRequiredError,
    SpraayGateway,
)

EXECUTE = {
    "token": "USDC",
    "chain": "base",
    "sender": SENDER,
    "recipients": RECIPIENTS,
    "amounts_raw": ["10000", "20000"],
}


async def test_execute_body_is_flat_raw_units_with_sender(fake: Any, make_gateway: Any) -> None:
    await make_gateway(payment_hook=pay_hook).execute(**EXECUTE)

    assert json.loads(fake.execute_calls()[0].content) == PROOF["request"]["json"]


async def test_execute_without_hook_surfaces_the_challenge(fake: Any, make_gateway: Any) -> None:
    with pytest.raises(PaymentRequiredError) as caught:
        await make_gateway().execute(**EXECUTE)

    assert caught.value.challenge.accepts[0]["amount"] == "20000"
    assert len(fake.execute_calls()) == 1


async def test_hook_pays_once_and_only_sees_options_within_the_cap(fake: Any, make_gateway: Any) -> None:
    seen: list[PaymentChallenge] = []

    def hook(challenge: PaymentChallenge) -> dict[str, str]:
        seen.append(challenge)
        return {"payment-signature": "signed"}

    response = await make_gateway(payment_hook=hook).execute(**EXECUTE)

    assert [item["network"] for item in seen[0].accepts] == ["eip155:8453"]
    assert fake.execute_calls()[1].headers["payment-signature"] == "signed"
    assert response.body["transaction"]["chainId"] == 8453


async def test_async_hook_is_awaited(fake: Any, make_gateway: Any) -> None:
    async def hook(_challenge: PaymentChallenge) -> dict[str, str]:
        return {"payment-signature": "signed"}

    assert (await make_gateway(payment_hook=hook).execute(**EXECUTE)).body["success"] is True


async def test_fee_above_cap_never_reaches_the_hook(fake: Any, make_gateway: Any) -> None:
    def hook(_challenge: PaymentChallenge) -> dict[str, str]:
        pytest.fail("hook must not be asked to pay above the cap")

    with pytest.raises(AccessFeeTooHighError):
        await make_gateway(payment_hook=hook, max_access_fee_raw=19999).execute(**EXECUTE)


async def test_rejected_payment_is_not_retried(fake: Any, make_gateway: Any) -> None:
    fake.paid_status = 402
    calls = 0

    def hook(_challenge: PaymentChallenge) -> dict[str, str]:
        nonlocal calls
        calls += 1
        return {"payment-signature": "signed"}

    with pytest.raises(GatewayResponseError):
        await make_gateway(payment_hook=hook).execute(**EXECUTE)

    assert calls == 1
    assert len(fake.execute_calls()) == 2


async def test_access_fee_receipt_comes_from_the_payment_response_header(make_gateway: Any) -> None:
    response = await make_gateway(payment_hook=pay_hook).execute(**EXECUTE)

    assert response.access_fee == {
        "tx_hash": PROOF["response"]["payment_response_decoded"]["transaction"],
        "network": "eip155:8453",
        "amount_raw": "20000",
    }


async def test_html_200_from_a_wrong_base_url_is_an_error() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, html="<!DOCTYPE html>"))
    async with httpx.AsyncClient(transport=transport) as http:
        with pytest.raises(GatewayResponseError, match="gateway URL"):
            await SpraayGateway("https://spraay.app", http=http).tokens()


async def test_estimate_is_a_get_with_human_amount(fake: Any, make_gateway: Any) -> None:
    await make_gateway().estimate(chain="base", recipients=2, total_human="0.03", token="USDC")

    request = fake.requests[0]
    assert request.method == "GET"
    assert dict(request.url.params) == {"chain": "base", "recipients": "2", "amount": "0.03", "token": "USDC"}


async def test_validate_posts_the_native_recipient_objects(fake: Any, make_gateway: Any) -> None:
    payments = [{"to": RECIPIENTS[0], "amount": "0.01"}]
    await make_gateway().validate(chain="base", token="USDC", sender=SENDER, payments=payments)

    assert json.loads(fake.requests[0].content) == {
        "chain": "base",
        "token": "USDC",
        "sender": SENDER,
        "recipients": payments,
    }


def test_url_falls_back_to_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPRAAY_GATEWAY_URL", "https://example.test/")

    assert SpraayGateway().url == "https://example.test"


def test_package_declares_no_signing_dependency() -> None:
    required = " ".join(metadata.requires("nemo-relay-spraay") or []).lower()

    assert not any(name in required for name in ("eth-account", "eth_account", "web3", "x402"))
