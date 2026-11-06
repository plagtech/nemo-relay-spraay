# SPDX-License-Identifier: Apache-2.0
"""Relay middleware for the batch tool: pre-execution checks, privacy, and spend metadata."""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

import nemo_relay
from nemo_relay import plugin

from .amounts import AmountError, to_raw_units
from .tool import CHAIN_IDS, TOOL_NAME

PLUGIN_KIND = "spraay-payment"
MAX_RECIPIENTS = 200
REDACTED = "[REDACTED]"
METADATA_PREFIX = "spraay."

DEFAULT_CONFIG: dict[str, Any] = {
    "chains": sorted(CHAIN_IDS),
    "max_recipients": MAX_RECIPIENTS,
    "redact_recipients": True,
    "priority": 10,
}

_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_RAW_AMOUNT = re.compile(r"^[1-9][0-9]*$")


def _diagnostic(level: str, code: str, field: str | None, message: str) -> dict[str, str]:
    item = {"level": level, "code": f"{PLUGIN_KIND}.{code}", "component": PLUGIN_KIND, "message": message}
    if field is not None:
        item["field"] = field
    return item


def validate_config(config: dict[str, Any]) -> list[dict[str, str]]:
    """Return diagnostics for a plugin configuration."""
    diagnostics = [
        _diagnostic("warning", "unknown_field", key, f"unknown field '{key}' is not supported")
        for key in sorted(config.keys() - DEFAULT_CONFIG.keys())
    ]
    settings = {**DEFAULT_CONFIG, **config}
    chains = settings["chains"]
    if not isinstance(chains, list) or not chains or not all(chain in CHAIN_IDS for chain in chains):
        message = f"chains must be a non-empty list drawn from {sorted(CHAIN_IDS)}"
        diagnostics.append(_diagnostic("error", "invalid_chains", "chains", message))
    limit = settings["max_recipients"]
    if type(limit) is not int or not 1 <= limit <= MAX_RECIPIENTS:
        message = f"max_recipients must be an integer from 1 to {MAX_RECIPIENTS}"
        diagnostics.append(_diagnostic("error", "invalid_max_recipients", "max_recipients", message))
    if type(settings["redact_recipients"]) is not bool:
        message = "redact_recipients must be a boolean"
        diagnostics.append(_diagnostic("error", "invalid_config", "redact_recipients", message))
    if type(settings["priority"]) is not int:
        diagnostics.append(_diagnostic("error", "invalid_config", "priority", "priority must be an integer"))
    return diagnostics


def batch_rejection(args: Any, *, chains: list[str], max_recipients: int) -> str | None:
    """Return why a batch must not run, or None. Pure, so it runs before any network call."""
    if not isinstance(args, dict):
        return "batch arguments must be an object"
    if args.get("chain") not in chains:
        return f"chain {args.get('chain')!r} is not enabled; enabled chains: {chains}"
    if not isinstance(args.get("token"), str) or not args["token"]:
        return "token is required"
    if not isinstance(args.get("sender"), str) or not _ADDRESS.match(args["sender"]):
        return "sender must be a 0x address"

    recipients = args.get("recipients")
    amounts = args.get("amounts")
    if not isinstance(recipients, list) or not recipients:
        return "recipients must be a non-empty list"
    if len(recipients) > max_recipients:
        return f"{len(recipients)} recipients exceeds the limit of {max_recipients}"
    if not isinstance(amounts, list) or len(amounts) != len(recipients):
        return "amounts must have one entry per recipient"
    for index, recipient in enumerate(recipients):
        if not isinstance(recipient, str) or not _ADDRESS.match(recipient):
            return f"recipients[{index}] is not a 0x address"
    for index, amount in enumerate(amounts):
        if not isinstance(amount, str) or not _RAW_AMOUNT.match(amount):
            return f"amounts[{index}] must be a positive integer string in raw base units"

    # A human amount passed where raw is expected would move vastly less or more than intended.
    human = args.get("amounts_human")
    decimals = args.get("decimals")
    if not isinstance(human, list) or len(human) != len(amounts) or type(decimals) is not int:
        return "amounts_human and decimals are required alongside raw amounts"
    try:
        if any(to_raw_units(value, decimals) != int(raw) for value, raw in zip(human, amounts, strict=True)):
            return "amounts_human and raw amounts disagree"
    except AmountError as error:
        return str(error)
    return None


def _redact_request(args: Any) -> Any:
    return {**args, "recipients": REDACTED, "sender": REDACTED} if isinstance(args, dict) else args


def _redact_response(result: Any) -> Any:
    # Calldata encodes every recipient address, so it must not reach exporters either.
    if isinstance(result, dict) and isinstance(result.get("transaction"), dict):
        return {**result, "transaction": {**result["transaction"], "data": REDACTED}}
    return result


def _request_metadata(data: dict[str, Any]) -> dict[str, Any]:
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


def _response_metadata(data: dict[str, Any]) -> dict[str, Any]:
    batch = data.get("batch") if isinstance(data.get("batch"), dict) else {}
    metadata = {
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


class SpraayPaymentPlugin:
    """Guards and observes ``spraay_build_batch`` calls. Holds no key and calls no gateway."""

    def __init__(self) -> None:
        self.built_batches: list[dict[str, Any]] = []
        """Spend metadata for each unsigned batch transaction built while active."""

    def validate(self, plugin_config: dict[str, Any]) -> list[dict[str, str]]:
        return validate_config(plugin_config)

    def register(self, plugin_config: dict[str, Any], context: plugin.PluginContext) -> None:
        settings = {**deepcopy(DEFAULT_CONFIG), **plugin_config}
        priority = settings["priority"]

        def guardrail(name: str, args: nemo_relay.Json) -> str | None:
            if name != TOOL_NAME:
                return None
            return batch_rejection(args, chains=settings["chains"], max_recipients=settings["max_recipients"])

        context.register_tool_conditional_execution_guardrail("batch-policy", priority, guardrail)

        if settings["redact_recipients"]:
            context.register_tool_sanitize_request_guardrail(
                "redact-recipients", priority, lambda name, args: _redact_request(args) if name == TOOL_NAME else args
            )
            context.register_tool_sanitize_response_guardrail(
                "redact-calldata",
                priority,
                lambda name, result: _redact_response(result) if name == TOOL_NAME else result,
            )

        def inject_metadata(event: nemo_relay.Event) -> dict[str, Any]:
            if event.name != TOOL_NAME or event.category != "tool" or not isinstance(event.data, dict):
                return {}
            if event.scope_category == "start":
                return _request_metadata(event.data)
            return _response_metadata(event.data) if event.data.get("status") == "unsigned" else {}

        context.register_event_metadata_injector("batch-metadata", priority, inject_metadata)

        def observe(event: nemo_relay.Event) -> None:
            if event.name == TOOL_NAME and event.category == "tool" and event.scope_category == "end":
                metadata = event.metadata if isinstance(event.metadata, dict) else {}
                if metadata.get(f"{METADATA_PREFIX}status") == "unsigned":
                    self.built_batches.append(
                        {key: value for key, value in metadata.items() if key.startswith(METADATA_PREFIX)}
                    )

        context.register_subscriber("batch-spend", observe)


def component(**config: Any) -> plugin.ComponentSpec:
    """Build the component spec for ``plugin.initialize``."""
    return plugin.ComponentSpec(kind=PLUGIN_KIND, config=config)
