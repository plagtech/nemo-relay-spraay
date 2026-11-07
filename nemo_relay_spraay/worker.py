# SPDX-License-Identifier: Apache-2.0
"""NeMo Relay gRPC worker that builds unsigned batch transactions through the Spraay gateway.

The plugin builds an unsigned batch transaction; the app signs and broadcasts it. The
worker holds no key and has no signing dependency.

A host runs the managed tool ``spraay_build_batch``. The middleware runs in this order:

1. The guardrail refuses malformed batches before any network call.
2. The request intercept converts human amounts to raw base units, once.
3. The sanitizers keep addresses and calldata out of emitted events.
4. The execution intercept calls the gateway and returns the result without running the
   app's callback, unless the gateway asks for x402 payment. The app's callback is then
   called with ``{"spraay_payment_challenge": ...}`` and returns ``{"headers": ...}``. That
   makes it the payment hook, in any language, and the key stays with the app.

All logic lives in this file because the manifest digest covers only this file.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, cast

import httpx
from nemo_relay_plugin import (
    ConfigDiagnostic,
    DiagnosticLevel,
    Json,
    PluginContext,
    ToolExecutionContext,
    ToolExecutionInterceptOutcome,
    WorkerPlugin,
    serve_plugin,
)

PLUGIN_ID = "plagtech.spraay_batch"
TOOL_NAME = "spraay_build_batch"
CHALLENGE_ARG = "spraay_payment_challenge"
METADATA_PREFIX = "spraay."
BUILT_MARK = "spraay.batch.built"
REDACTED = "[REDACTED]"

DEFAULT_GATEWAY_URL = "https://gateway.spraay.app"
HTTP_TIMEOUT_SECONDS = 60
MAX_RECIPIENTS = 200

# Used only to check the returned transaction targets the requested chain. Whether a
# chain can be used at all comes from the gateway's estimate.supported.
CHAIN_IDS = {"base": 8453, "peaq": 3338}

DEFAULT_CONFIG: dict[str, Json] = {
    "chains": sorted(CHAIN_IDS),
    "max_recipients": MAX_RECIPIENTS,
    "redact_recipients": True,
    "priority": 10,
    "max_access_fee_raw": 20000,  # $0.02 in 6-decimal stablecoin base units.
}
OPTIONAL_FIELDS = {"gateway_url"}

_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_RAW_AMOUNT = re.compile(r"^[1-9][0-9]*$")
_PRECISION = 120


class SpraayError(Exception):
    """Base class for errors raised by this worker."""


class AmountError(SpraayError, ValueError):
    """An amount cannot be represented exactly in the token's base units."""


class GatewayResponseError(SpraayError):
    """The gateway answered with an error status or a non-JSON body."""


class AccessFeeTooHighError(SpraayError):
    """Every payment option in the challenge costs more than the configured cap."""


class UnsupportedChainError(SpraayError):
    """The gateway's estimate reports that this chain cannot be used for a batch."""


class BatchValidationError(SpraayError):
    """The gateway's free validation rejected the batch, so no access fee was paid."""


class ResponseMismatchError(SpraayError):
    """The returned transaction does not match the batch that was requested."""


# Amounts


def _parse_amount(value: object) -> Decimal:
    # Floats are refused: 0.1 + 0.2 style error has no place in a payment amount.
    if isinstance(value, (bool, float)) or not isinstance(value, (str, int, Decimal)):
        raise AmountError(f"amount must be a decimal string or integer, not {type(value).__name__}")
    try:
        parsed = Decimal(value.strip() if isinstance(value, str) else value)
    except InvalidOperation:
        raise AmountError(f"amount {value!r} is not a decimal number") from None
    if not parsed.is_finite():
        raise AmountError(f"amount {value!r} is not finite")
    return parsed


def to_raw_units(value: object, decimals: int, *, allow_zero: bool = False) -> int:
    """Convert a human-decimal amount to raw base units, refusing any rounding."""
    parsed = _parse_amount(value)
    with localcontext(prec=_PRECISION):
        scaled = parsed.scaleb(decimals)
        if scaled != scaled.to_integral_value():
            raise AmountError(f"amount {value!r} has more than {decimals} decimal places")
        raw = int(scaled)
    if raw < 0 or (raw == 0 and not allow_zero):
        raise AmountError(f"amount {value!r} must be greater than zero")
    return raw


def to_human_units(raw: int, decimals: int) -> str:
    """Format raw base units as a plain decimal string with no trailing zeros."""
    with localcontext(prec=_PRECISION):
        text = format(Decimal(raw).scaleb(-decimals), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


# Gateway


def _json_body(response: httpx.Response) -> dict[str, Any]:
    # A wrong base URL can answer 200 with an HTML page, so status alone proves nothing.
    if "json" not in response.headers.get("content-type", ""):
        raise GatewayResponseError(
            f"expected JSON from {response.request.url}, is the gateway URL correct? (HTTP {response.status_code})"
        )
    body = response.json()
    if not isinstance(body, dict):
        raise GatewayResponseError(f"expected a JSON object (HTTP {response.status_code})")
    return body


def _checked_body(response: httpx.Response) -> dict[str, Any]:
    if response.status_code != 200:
        raise GatewayResponseError(
            f"gateway rejected {response.request.url.path} (HTTP {response.status_code}): {response.text[:500]}"
        )
    return _json_body(response)


def _decode_receipt(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    try:
        decoded = json.loads(base64.b64decode(value + "=" * (-len(value) % 4)))
    except ValueError:
        return None
    return decoded if isinstance(decoded, dict) else None


def _within_cap(item: Any, cap: int) -> bool:
    try:
        return int(item["amount"]) <= cap
    except (KeyError, TypeError, ValueError):
        return False


class SpraayGateway:
    """Calls the live gateway endpoints this plugin depends on. Signs nothing."""

    def __init__(self, url: str, *, max_access_fee_raw: int, http: httpx.AsyncClient) -> None:
        self.url = url.rstrip("/")
        self._max_access_fee_raw = max_access_fee_raw
        self._http = http

    async def tokens(self) -> dict[str, Any]:
        """``GET /api/v1/tokens`` (free)."""
        return _checked_body(await self._http.get(f"{self.url}/api/v1/tokens"))

    async def estimate(self, *, chain: str, recipients: int, total_human: str, token: str) -> dict[str, Any]:
        """``GET /free/estimate-batch`` (free). ``amount`` is in human units on this endpoint."""
        params = {"chain": chain, "recipients": recipients, "amount": total_human, "token": token}
        body = _checked_body(await self._http.get(f"{self.url}/free/estimate-batch", params=params))
        estimate = body.get("estimate")
        if not isinstance(estimate, dict):
            raise GatewayResponseError("estimate response has no estimate object")
        return estimate

    async def validate(self, *, chain: str, token: str, sender: str, payments: list[dict[str, str]]) -> dict[str, Any]:
        """``POST /free/validate-batch`` (free). Takes ``recipients: [{to, amount}]``."""
        payload = {"chain": chain, "token": token, "sender": sender, "recipients": payments}
        return _checked_body(await self._http.post(f"{self.url}/free/validate-batch", json=payload))

    async def execute_unpaid(self, payload: dict[str, Any]) -> httpx.Response:
        """``POST /api/v1/batch/execute`` with no payment attached."""
        return await self._http.post(f"{self.url}/api/v1/batch/execute", json=payload)

    async def execute_paid(self, payload: dict[str, Any], headers: dict[str, str]) -> httpx.Response:
        """``POST /api/v1/batch/execute`` once, with the app's payment headers."""
        return await self._http.post(f"{self.url}/api/v1/batch/execute", json=payload, headers=headers)

    def challenge(self, response: httpx.Response) -> dict[str, Any]:
        """Parse a 402 and keep only the payment options within the fee cap."""
        body = _json_body(response)
        offered = body.get("accepts")
        offered = offered if isinstance(offered, list) else []
        accepts = [item for item in offered if _within_cap(item, self._max_access_fee_raw)]
        if not accepts:
            raise AccessFeeTooHighError(f"no payment option at or below {self._max_access_fee_raw} raw units")
        return {"url": f"{self.url}/api/v1/batch/execute", "accepts": accepts, "body": body}


def _access_fee(response: httpx.Response, accepts: list[dict[str, Any]]) -> dict[str, Any] | None:
    receipt = _decode_receipt(response.headers.get("payment-response") or response.headers.get("x-payment-response"))
    if receipt is None:
        return None
    network = receipt.get("network")
    match = next((item for item in accepts if item.get("network") == network), {})
    return {"tx_hash": receipt.get("transaction"), "network": network, "amount_raw": match.get("amount")}


# Batch checks


def _word(value: int) -> str:
    return format(value, "x").rjust(64, "0")


def verify_response(body: dict[str, Any], args: dict[str, Any], raw_amounts: list[int]) -> dict[str, int]:
    """Check the response matches the request before an app can sign it."""
    decimals = args["decimals"]
    transaction = body.get("transaction")
    batch = body.get("batch")
    token = body.get("token")
    if (
        body.get("success") is not True
        or not isinstance(transaction, dict)
        or not isinstance(batch, dict)
        or not isinstance(token, dict)
    ):
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


def batch_rejection(args: Json, *, chains: list[str], max_recipients: int) -> str | None:
    """Return why a batch request must not run, or None. Pure, so it runs before any network call."""
    if not isinstance(args, dict):
        return "batch arguments must be an object"
    if args.get("chain") not in chains:
        return f"chain {args.get('chain')!r} is not enabled; enabled chains: {chains}"
    if not isinstance(args.get("token"), str) or not args["token"]:
        return "token is required"
    if not isinstance(args.get("sender"), str) or not _ADDRESS.match(args["sender"]):
        return "sender must be a 0x address"

    decimals = args.get("decimals")
    if decimals is not None and (type(decimals) is not int or decimals < 0):
        return "decimals must be a non-negative integer"
    payments = args.get("payments")
    if not isinstance(payments, list) or not payments:
        return "payments must be a non-empty list"
    if len(payments) > max_recipients:
        return f"{len(payments)} recipients exceeds the limit of {max_recipients}"
    for index, payment in enumerate(payments):
        if not isinstance(payment, dict):
            return f"payments[{index}] must be an object with to and amount"
        recipient = payment.get("to")
        if not isinstance(recipient, str) or not _ADDRESS.match(recipient):
            return f"payments[{index}].to is not a 0x address"
        try:
            if decimals is not None:
                to_raw_units(payment.get("amount"), decimals)
            elif _parse_amount(payment.get("amount")) <= 0:
                return f"payments[{index}].amount must be greater than zero"
        except AmountError as error:
            return f"payments[{index}].amount: {error}"
    return None


def converted_args_rejection(args: Json) -> str | None:
    """Check the arguments the execution intercept received still agree with each other.

    Another plugin's request intercept can run between conversion and execution.
    """
    if not isinstance(args, dict):
        return "batch arguments must be an object"
    recipients, amounts, human = args.get("recipients"), args.get("amounts"), args.get("amounts_human")
    if not isinstance(recipients, list) or not isinstance(amounts, list) or not isinstance(human, list):
        return "recipients, amounts, and amounts_human are required"
    if not recipients or not len(recipients) == len(amounts) == len(human):
        return "recipients, amounts, and amounts_human must have one entry per recipient"
    if type(args.get("decimals")) is not int:
        return "decimals is required"
    for index, recipient in enumerate(recipients):
        if not isinstance(recipient, str) or not _ADDRESS.match(recipient):
            return f"recipients[{index}] is not a 0x address"
    for index, raw in enumerate(amounts):
        if not isinstance(raw, str) or not _RAW_AMOUNT.match(raw):
            return f"amounts[{index}] must be a positive integer string in raw base units"
    try:
        if any(to_raw_units(value, args["decimals"]) != int(raw) for value, raw in zip(human, amounts, strict=True)):
            return "amounts_human and raw amounts disagree"
    except AmountError as error:
        return str(error)
    return None


# Event helpers


def redact_request(args: Json) -> Json:
    if not isinstance(args, dict):
        return args
    hidden = {key: REDACTED for key in ("recipients", "sender", "payments") if key in args}
    return {**args, **hidden}


def redact_response(result: Json) -> Json:
    # Calldata encodes every recipient address, so it must not reach exporters either.
    if isinstance(result, dict) and isinstance(result.get("transaction"), dict):
        return {**result, "transaction": {**result["transaction"], "data": REDACTED}}
    return result


def request_metadata(data: dict[str, Any]) -> dict[str, Json]:
    raw = [str(value) for value in data.get("amounts", [])]
    human = [str(value) for value in data.get("amounts_human", [])]
    return {
        f"{METADATA_PREFIX}chain": str(data.get("chain")),
        f"{METADATA_PREFIX}token": str(data.get("token")),
        f"{METADATA_PREFIX}recipient_count": len(raw),
        f"{METADATA_PREFIX}amounts_human": human,
        f"{METADATA_PREFIX}amounts_raw": raw,
        f"{METADATA_PREFIX}total_raw": str(sum(int(value) for value in raw)),
    }


def response_metadata(data: dict[str, Any]) -> dict[str, Json]:
    batch = data.get("batch") if isinstance(data.get("batch"), dict) else {}
    metadata: dict[str, Json] = {
        f"{METADATA_PREFIX}status": str(data.get("status")),
        f"{METADATA_PREFIX}chain": str(data.get("chain")),
        f"{METADATA_PREFIX}recipient_count": batch.get("recipientCount", 0),
        f"{METADATA_PREFIX}total_human": str(batch.get("totalAmount")),
        f"{METADATA_PREFIX}total_raw": str(batch.get("total_raw")),
        f"{METADATA_PREFIX}protocol_fee_human": str(batch.get("fee")),
        f"{METADATA_PREFIX}protocol_fee_raw": str(batch.get("fee_raw")),
    }
    fee = data.get("access_fee")
    if isinstance(fee, dict):
        for key in ("tx_hash", "network", "amount_raw"):
            if fee.get(key) is not None:
                metadata[f"{METADATA_PREFIX}access_fee_{key}"] = str(fee[key])
    return metadata


# Plugin


class SpraayBatchPlugin(WorkerPlugin):
    """Guards, runs, and observes ``spraay_build_batch`` calls."""

    plugin_id = PLUGIN_ID
    allows_multiple_components = False

    def __init__(self) -> None:
        self.built_batches: list[dict[str, Json]] = []
        """Spend metadata for each unsigned batch built. Hosts read the ``spraay.batch.built`` mark."""

    def validate(self, config: Json) -> list[ConfigDiagnostic | dict[str, Any]]:
        return list(validate_config(config))

    def register(self, ctx: PluginContext, config: Json) -> None:
        errors = [item for item in validate_config(config) if item.level == DiagnosticLevel.ERROR]
        if errors:
            raise ValueError(errors[0].message)
        settings = normalized_config(config)
        priority = cast(int, settings["priority"])
        chains = cast(list[str], settings["chains"])
        max_recipients = cast(int, settings["max_recipients"])
        url = cast(str, settings.get("gateway_url") or os.environ.get("SPRAAY_GATEWAY_URL") or DEFAULT_GATEWAY_URL)
        max_access_fee_raw = cast(int, settings["max_access_fee_raw"])

        def gateway(http: httpx.AsyncClient) -> SpraayGateway:
            return SpraayGateway(url, max_access_fee_raw=max_access_fee_raw, http=http)

        def guardrail(name: str, args: Json) -> str | None:
            if name != TOOL_NAME:
                return None
            return batch_rejection(args, chains=chains, max_recipients=max_recipients)

        async def convert_amounts(name: str, args: Json) -> Json:
            if name != TOOL_NAME:
                return args
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as http:
                return await _converted_args(gateway(http), cast(dict[str, Any], args))

        async def execute(context: ToolExecutionContext, next_call: Any) -> ToolExecutionInterceptOutcome:
            if context.tool_name != TOOL_NAME:
                downstream = await next_call.call(context.args)
                return ToolExecutionInterceptOutcome(result=downstream.result, annotation=downstream.annotation)
            rejection = converted_args_rejection(context.args)
            if rejection is not None:
                raise BatchValidationError(rejection)
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as http:
                result = await _build_batch(gateway(http), cast(dict[str, Any], context.args), next_call)
            return ToolExecutionInterceptOutcome(result=result)

        ctx.register_tool_conditional_execution_guardrail("spraay_batch_policy", guardrail, priority=priority)
        ctx.register_tool_request_intercept("spraay_convert_amounts", convert_amounts, priority=priority)
        ctx.register_tool_execution_intercept("spraay_build_batch", execute, priority=priority)

        if settings["redact_recipients"]:

            def sanitize_request(name: str, args: Json) -> Json:
                return redact_request(args) if name == TOOL_NAME else args

            def sanitize_response(name: str, result: Json) -> Json:
                return redact_response(result) if name == TOOL_NAME else result

            ctx.register_tool_sanitize_request_guardrail(
                "spraay_redact_recipients", sanitize_request, priority=priority
            )
            ctx.register_tool_sanitize_response_guardrail(
                "spraay_redact_calldata", sanitize_response, priority=priority
            )

        def inject_metadata(event: dict[str, Any]) -> dict[str, Json]:
            data = event.get("data")
            if event.get("name") != TOOL_NAME or event.get("category") != "tool" or not isinstance(data, dict):
                return {}
            if event.get("scope_category") == "start":
                return request_metadata(data)
            return response_metadata(data) if data.get("status") == "unsigned" else {}

        async def observe(event: dict[str, Any]) -> None:
            if event.get("name") != TOOL_NAME or event.get("scope_category") != "end":
                return
            metadata = event.get("metadata")
            if isinstance(metadata, dict) and metadata.get(f"{METADATA_PREFIX}status") == "unsigned":
                record = {key: value for key, value in metadata.items() if key.startswith(METADATA_PREFIX)}
                self.built_batches.append(record)
                await ctx.runtime.emit_mark(BUILT_MARK, record)

        ctx.register_event_metadata_injector("spraay_batch_metadata", inject_metadata, priority=priority)
        ctx.register_subscriber("spraay_batch_spend", observe)


async def _converted_args(gateway: SpraayGateway, args: dict[str, Any]) -> dict[str, Any]:
    """Convert human amounts to raw base units. This is the only conversion."""
    decimals = args.get("decimals")
    if decimals is None:
        catalog = (await gateway.tokens()).get("popularTokens", {})
        entry = catalog.get(args["token"].upper()) if isinstance(catalog, dict) else None
        if not isinstance(entry, dict) or not isinstance(entry.get("decimals"), int):
            raise SpraayError(
                f"decimals for token {args['token']!r} are not in the gateway catalog; pass decimals explicitly"
            )
        decimals = entry["decimals"]
    raw_amounts = [to_raw_units(payment["amount"], decimals) for payment in args["payments"]]
    return {
        "chain": args["chain"],
        "token": args["token"],
        "sender": args["sender"],
        "decimals": decimals,
        "recipients": [payment["to"] for payment in args["payments"]],
        "amounts": [str(raw) for raw in raw_amounts],
        "amounts_human": [to_human_units(raw, decimals) for raw in raw_amounts],
    }


async def _build_batch(gateway: SpraayGateway, args: dict[str, Any], next_call: Any) -> dict[str, Any]:
    raw = [int(amount) for amount in args["amounts"]]
    total_human = to_human_units(sum(raw), args["decimals"])
    estimate = await gateway.estimate(
        chain=args["chain"], recipients=len(raw), total_human=total_human, token=args["token"]
    )
    if estimate.get("supported") is not True:
        raise UnsupportedChainError(f"gateway reports chain {args['chain']!r} is not supported for batches")

    native = [
        {"to": to, "amount": amount} for to, amount in zip(args["recipients"], args["amounts_human"], strict=True)
    ]
    check = await gateway.validate(chain=args["chain"], token=args["token"], sender=args["sender"], payments=native)
    if check.get("valid") is not True:
        raise BatchValidationError(f"gateway validation failed: {check.get('errors')}")

    payload = {
        "token": args["token"],
        "chain": args["chain"],
        "sender": args["sender"],
        "recipients": args["recipients"],
        "amounts": args["amounts"],
    }
    response = await gateway.execute_unpaid(payload)
    accepts: list[dict[str, Any]] = []
    if response.status_code == 402:
        challenge = gateway.challenge(response)
        accepts = challenge["accepts"]
        paid = await next_call.call({CHALLENGE_ARG: challenge})
        headers = paid.result.get("headers") if isinstance(paid.result, dict) else None
        if not isinstance(headers, dict) or not headers:
            return {"status": "payment_required", "chain": args["chain"], "challenge": challenge}
        response = await gateway.execute_paid(payload, {str(key): str(value) for key, value in headers.items()})

    body = _checked_body(response)
    totals = verify_response(body, args, raw)
    result: dict[str, Any] = {
        "status": "unsigned",
        "chain": args["chain"],
        "contract": body.get("contract"),
        "token": body["token"],
        "batch": {**body["batch"], **{key: str(value) for key, value in totals.items()}},
        "transaction": body["transaction"],
        "estimated_gas_usd": estimate.get("estimatedGasUSD"),
    }
    if "approvalRequired" in body:
        result["approval_required"] = body["approvalRequired"]
    access_fee = _access_fee(response, accepts)
    if access_fee is not None:
        result["access_fee"] = access_fee
    return result


# Configuration


def normalized_config(config: Json) -> dict[str, Json]:
    settings = dict(DEFAULT_CONFIG)
    if isinstance(config, dict):
        settings.update(config)
    return settings


def validate_config(config: Json) -> list[ConfigDiagnostic]:
    if not isinstance(config, dict):
        return [_diagnostic("invalid_config", None, "plugin config must be a JSON object")]
    diagnostics = [
        _diagnostic("unknown_field", key, f"unknown field '{key}'")
        for key in sorted(config.keys() - DEFAULT_CONFIG.keys() - OPTIONAL_FIELDS)
    ]
    settings = normalized_config(config)
    chains = settings["chains"]
    if not isinstance(chains, list) or not chains or not all(chain in CHAIN_IDS for chain in chains):
        diagnostics.append(
            _diagnostic("invalid_chains", "chains", f"chains must be a non-empty list drawn from {sorted(CHAIN_IDS)}")
        )
    limit = settings["max_recipients"]
    if type(limit) is not int or not 1 <= limit <= MAX_RECIPIENTS:
        message = f"max_recipients must be an integer from 1 to {MAX_RECIPIENTS}"
        diagnostics.append(_diagnostic("invalid_max_recipients", "max_recipients", message))
    if type(settings["redact_recipients"]) is not bool:
        diagnostics.append(_diagnostic("invalid_config", "redact_recipients", "redact_recipients must be a boolean"))
    if type(settings["priority"]) is not int:
        diagnostics.append(_diagnostic("invalid_config", "priority", "priority must be an integer"))
    fee_cap = settings["max_access_fee_raw"]
    if type(fee_cap) is not int or fee_cap < 0:
        message = "max_access_fee_raw must be a non-negative integer"
        diagnostics.append(_diagnostic("invalid_config", "max_access_fee_raw", message))
    url = settings.get("gateway_url")
    if url is not None and (not isinstance(url, str) or not url.startswith(("https://", "http://"))):
        diagnostics.append(_diagnostic("invalid_config", "gateway_url", "gateway_url must be an http(s) URL"))
    return diagnostics


def _diagnostic(suffix: str, field: str | None, message: str) -> ConfigDiagnostic:
    return ConfigDiagnostic(
        level=DiagnosticLevel.ERROR,
        code=f"{PLUGIN_ID}.{suffix}",
        component=PLUGIN_ID,
        field=field,
        message=message,
    )


async def main() -> None:
    """Serve the worker entrypoint referenced by relay-plugin.toml."""
    await serve_plugin(SpraayBatchPlugin())


if __name__ == "__main__":
    asyncio.run(main())
