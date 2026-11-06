# SPDX-License-Identifier: Apache-2.0
"""The managed tool: builds an unsigned batch transaction. The app signs and broadcasts it."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import nemo_relay
from nemo_relay import tools

from .amounts import to_human_units, to_raw_units
from .gateway import SpraayError, SpraayGateway

TOOL_NAME = "spraay_build_batch"

# Used only to check the returned transaction targets the requested chain. Whether a
# chain can be used at all comes from the gateway's estimate.supported.
CHAIN_IDS = {"base": 8453, "peaq": 3338}


class UnsupportedChainError(SpraayError):
    """The gateway's estimate reports that this chain cannot be used for a batch."""


class BatchValidationError(SpraayError):
    """The gateway's free validation rejected the batch, so no access fee was paid."""


class ResponseMismatchError(SpraayError):
    """The returned transaction does not match the batch that was requested."""


async def _resolve_decimals(gateway: SpraayGateway, token: str, decimals: int | None) -> int:
    if decimals is not None:
        return decimals
    catalog = (await gateway.tokens()).get("popularTokens", {})
    entry = catalog.get(token.upper()) if isinstance(catalog, dict) else None
    if not isinstance(entry, dict) or not isinstance(entry.get("decimals"), int):
        raise SpraayError(f"decimals for token {token!r} are not in the gateway catalog; pass decimals explicitly")
    return entry["decimals"]


def _word(value: int) -> str:
    return format(value, "x").rjust(64, "0")


def _verify(body: dict[str, Any], args: Mapping[str, Any], raw_amounts: list[int]) -> dict[str, int]:
    """Check the response matches the request before an app can sign it."""
    decimals = args["decimals"]
    transaction = body.get("transaction")
    batch = body.get("batch")
    token = body.get("token")
    if body.get("success") is not True or not all(isinstance(item, dict) for item in (transaction, batch, token)):
        raise ResponseMismatchError("gateway response is missing transaction, batch, or token")

    if token.get("decimals") != decimals:
        raise ResponseMismatchError(
            f"amounts were converted with {decimals} decimals, gateway used {token.get('decimals')}"
        )
    if transaction.get("chainId") != CHAIN_IDS.get(args["chain"]):
        raise ResponseMismatchError(f"transaction is for chain id {transaction.get('chainId')}, not {args['chain']}")
    if batch.get("recipientCount") != len(raw_amounts):
        raise ResponseMismatchError("gateway recipient count differs from the request")

    try:
        total_raw = to_raw_units(batch["totalAmount"], decimals)
        fee_raw = to_raw_units(batch["fee"], decimals, allow_zero=True)
        total_with_fee_raw = to_raw_units(batch["totalWithFee"], decimals)
    except (KeyError, ValueError) as error:
        raise ResponseMismatchError(f"gateway batch totals are unreadable: {error}") from None
    if total_raw != sum(raw_amounts):
        raise ResponseMismatchError("gateway total differs from the sum of requested amounts")

    data = str(transaction.get("data", "")).lower()
    for address, raw in zip(args["recipients"], raw_amounts, strict=True):
        if address[2:].lower().rjust(64, "0") not in data or _word(raw) not in data:
            raise ResponseMismatchError("transaction calldata does not encode every requested recipient and amount")

    approval = body.get("approvalRequired")
    if token.get("isETH") is False:
        if not isinstance(approval, dict) or str(approval.get("amount")) != str(total_with_fee_raw):
            raise ResponseMismatchError("gateway approval amount differs from the batch total with fee")
    return {"total_raw": total_raw, "fee_raw": fee_raw, "total_with_fee_raw": total_with_fee_raw}


async def build_batch_transaction(
    gateway: SpraayGateway,
    *,
    chain: str,
    token: str,
    sender: str,
    payments: Sequence[Mapping[str, Any]],
    decimals: int | None = None,
    tool_call_id: str | None = None,
) -> nemo_relay.ToolExecutionResult:
    """Build an unsigned batch transaction through Relay's managed tool pipeline.

    Args:
        gateway: The gateway client, holding the app's optional payment hook.
        chain: ``"base"`` or ``"peaq"``.
        token: Token symbol or contract address.
        sender: Address that will sign and broadcast the returned transaction.
        payments: ``[{"to": address, "amount": human_amount}, ...]``. Amounts are human
            decimals, for example ``"1.50"`` for 1.50 USDC, and are converted here, once.
        decimals: Token decimals. Looked up in the gateway catalog when omitted, which
            only covers its popular symbols.
        tool_call_id: Optional provider tool call id for the emitted events.

    Returns:
        ToolExecutionResult whose ``result`` holds ``transaction`` and, for ERC-20 tokens,
        ``approval_required``. Nothing has been signed or broadcast.
    """
    resolved_decimals = await _resolve_decimals(gateway, token, decimals)
    raw_amounts = [to_raw_units(payment["amount"], resolved_decimals) for payment in payments]
    args: dict[str, Any] = {
        "chain": chain,
        "token": token,
        "sender": sender,
        "decimals": resolved_decimals,
        "recipients": [payment["to"] for payment in payments],
        "amounts": [str(raw) for raw in raw_amounts],
        "amounts_human": [to_human_units(raw, resolved_decimals) for raw in raw_amounts],
    }

    async def call_gateway(tool_args: Any) -> nemo_relay.ToolExecutionResult:
        raw = [int(amount) for amount in tool_args["amounts"]]
        count = len(raw)
        total_human = to_human_units(sum(raw), tool_args["decimals"])
        estimate = await gateway.estimate(
            chain=tool_args["chain"], recipients=count, total_human=total_human, token=tool_args["token"]
        )
        if estimate.get("supported") is not True:
            raise UnsupportedChainError(f"gateway reports chain {tool_args['chain']!r} is not supported for batches")

        native = [
            {"to": to, "amount": amount}
            for to, amount in zip(tool_args["recipients"], tool_args["amounts_human"], strict=True)
        ]
        check = await gateway.validate(
            chain=tool_args["chain"], token=tool_args["token"], sender=tool_args["sender"], payments=native
        )
        if check.get("valid") is not True:
            raise BatchValidationError(f"gateway validation failed: {check.get('errors')}")

        response = await gateway.execute(
            token=tool_args["token"],
            chain=tool_args["chain"],
            sender=tool_args["sender"],
            recipients=tool_args["recipients"],
            amounts_raw=tool_args["amounts"],
        )
        totals = _verify(response.body, tool_args, raw)
        result: dict[str, Any] = {
            "status": "unsigned",
            "chain": tool_args["chain"],
            "contract": response.body.get("contract"),
            "token": response.body["token"],
            "batch": {**response.body["batch"], **{key: str(value) for key, value in totals.items()}},
            "transaction": response.body["transaction"],
            "estimated_gas_usd": estimate.get("estimatedGasUSD"),
        }
        if "approvalRequired" in response.body:
            result["approval_required"] = response.body["approvalRequired"]
        if response.access_fee is not None:
            result["access_fee"] = response.access_fee
        return nemo_relay.ToolExecutionResult(result, None)

    return await tools.execute(TOOL_NAME, args, call_gateway, tool_call_id=tool_call_id)
