# SPDX-License-Identifier: Apache-2.0
"""Offline tests for the example callback's spending limits. Signs with throwaway keys only."""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("x402", reason="needs the example dependency group")
pytest.importorskip("eth_account", reason="needs the example dependency group")
pytest.importorskip("nemo_relay", reason="needs the example dependency group")

from eth_account import Account  # noqa: E402

ROOT = Path(__file__).parents[1]
PROOF = json.loads((ROOT / "docs" / "live-execute-proof.json").read_text(encoding="utf-8"))
SPEC = importlib.util.spec_from_file_location("x402_callback", ROOT / "examples" / "x402_callback.py")
assert SPEC is not None and SPEC.loader is not None
example = importlib.util.module_from_spec(SPEC)
sys.modules["x402_callback"] = example
SPEC.loader.exec_module(example)


def challenge_args(**option_overrides: Any) -> dict[str, Any]:
    option = {**PROOF["challenge_paid"], **option_overrides}
    body = {"x402Version": 2, "error": "Payment required", "accepts": [option]}
    return {example.CHALLENGE_ARG: {"url": "https://gateway.test", "accepts": [option], "body": body}}


async def test_declines_without_a_wallet() -> None:
    callback = example.X402Callback(None)

    assert (await callback(challenge_args())).result == {}
    assert callback.declined_reason == "no --pay"


async def test_signs_the_expected_fee_from_the_given_account() -> None:
    account = Account.create()
    outcome = await example.X402Callback(account)(challenge_args())

    value = outcome.result["headers"]["PAYMENT-SIGNATURE"]
    decoded = json.loads(base64.b64decode(value + "=" * (-len(value) % 4)))
    authorization = decoded["payload"]["authorization"]
    assert authorization["from"] == account.address
    assert authorization["to"] == example.PAY_TO
    assert authorization["value"] == "20000"


@pytest.mark.parametrize(
    "overrides",
    [
        {"amount": "20001"},
        {"amount": "0"},
        {"amount": "nope"},
        {"payTo": "0x0000000000000000000000000000000000000001"},
        {"asset": "0x0000000000000000000000000000000000000002"},
        {"scheme": "upto"},
        {"network": "eip155:4663"},
    ],
)
async def test_declines_anything_but_the_expected_fee(overrides: dict[str, Any]) -> None:
    callback = example.X402Callback(Account.create())

    assert (await callback(challenge_args(**overrides))).result == {}
    assert callback.paid_headers == {}


async def test_pays_at_most_once() -> None:
    callback = example.X402Callback(Account.create())
    await callback(challenge_args())

    assert (await callback(challenge_args())).result == {}
    assert callback.declined_reason == "already paid once"


async def test_refuses_calls_without_a_challenge() -> None:
    with pytest.raises(RuntimeError, match="payment challenge"):
        await example.X402Callback(None)({"chain": "base"})
