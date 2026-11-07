# SPDX-License-Identifier: Apache-2.0
"""Offline contract tests. The HTTP layer replays the recorded live execute response."""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from copy import deepcopy
from decimal import Decimal
from importlib import metadata
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from nemo_relay_plugin import PluginContext, PluginRuntime, ToolExecutionContext, ToolExecutionResult

from nemo_relay_spraay import worker
from nemo_relay_spraay.worker import (
    BUILT_MARK,
    CHALLENGE_ARG,
    PLUGIN_ID,
    TOOL_NAME,
    AccessFeeTooHighError,
    AmountError,
    BatchValidationError,
    GatewayResponseError,
    ResponseMismatchError,
    SpraayBatchPlugin,
    UnsupportedChainError,
    to_human_units,
    to_raw_units,
)

pytestmark = pytest.mark.httpx_mock(assert_all_requests_were_expected=False, can_send_already_matched_responses=True)

ROOT = Path(__file__).parents[1]
PROOF = json.loads((ROOT / "docs" / "live-execute-proof.json").read_text(encoding="utf-8"))
SENDER = PROOF["request"]["json"]["sender"]
RECIPIENTS = PROOF["request"]["json"]["recipients"]
PAYMENTS = [{"to": RECIPIENTS[0], "amount": "0.01"}, {"to": RECIPIENTS[1], "amount": "0.02"}]
GATEWAY = "https://gateway.test"
CHALLENGE = {
    "x402Version": 2,
    "error": "Payment required",
    "accepts": [PROOF["challenge_paid"], {**PROOF["challenge_paid"], "network": "eip155:4663", "amount": "99999"}],
    # The stale transactions[] example really is in the live 402 body and must be ignored.
    "extensions": {"bazaar": {"info": {"output": {"example": {"transactions": []}}}}},
}
ESTIMATE = {"estimate": {"chain": "base", "recipients": 2, "estimatedGasUSD": 0.046, "supported": True}}
TOKENS = {"popularTokens": {"USDC": {"decimals": 6}}}


class FakeGateway:
    """Replays recorded gateway bytes and records every request it receives."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.estimate: dict[str, Any] = ESTIMATE
        self.validation: dict[str, Any] = {"valid": True, "errors": [], "warnings": []}
        self.paid_status = PROOF["response"]["status"]
        self.paid_body: Any = PROOF["response"]["json"]
        self.challenge: dict[str, Any] = CHALLENGE

    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]

    def execute_calls(self) -> list[httpx.Request]:
        return [request for request in self.requests if request.url.path == "/api/v1/batch/execute"]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/api/v1/tokens":
            return httpx.Response(200, json=TOKENS)
        if path == "/free/estimate-batch":
            return httpx.Response(200, json=self.estimate)
        if path == "/free/validate-batch":
            return httpx.Response(200, json=self.validation)
        if path == "/api/v1/batch/execute":
            if "payment-signature" not in request.headers:
                return httpx.Response(402, json=self.challenge)
            headers = {"payment-response": PROOF["response"]["headers"]["payment-response"]}
            return httpx.Response(self.paid_status, json=self.paid_body, headers=headers)
        return httpx.Response(404, text="not found")


@pytest.fixture
def fake(httpx_mock: Any) -> FakeGateway:
    gateway = FakeGateway()
    httpx_mock.add_callback(gateway, is_reusable=True, is_optional=True)
    return gateway


def registered(config: dict[str, Any] | None = None) -> tuple[SpraayBatchPlugin, MagicMock]:
    context = MagicMock(spec=PluginContext)
    context.runtime = MagicMock(spec=PluginRuntime)
    context.runtime.emit_mark = AsyncMock()
    implementation = SpraayBatchPlugin()
    implementation.register(context, {"gateway_url": GATEWAY, **(config or {})})
    return implementation, context


def callback(context: MagicMock, method: str) -> Any:
    calls = getattr(context, method).call_args_list
    assert len(calls) == 1
    return calls[0].args[1]


def paying_next() -> MagicMock:
    next_call = MagicMock()
    next_call.call = AsyncMock(return_value=ToolExecutionResult({"headers": {"payment-signature": "test-sig-token"}}))
    return next_call


def declining_next() -> MagicMock:
    next_call = MagicMock()
    next_call.call = AsyncMock(return_value=ToolExecutionResult({"unrelated": True}))
    return next_call


def request_args(**overrides: Any) -> dict[str, Any]:
    return {"chain": "base", "token": "USDC", "sender": SENDER, "payments": PAYMENTS, **overrides}


def converted_args(**overrides: Any) -> dict[str, Any]:
    return {
        "chain": "base",
        "token": "USDC",
        "sender": SENDER,
        "decimals": 6,
        "recipients": RECIPIENTS,
        "amounts": ["10000", "20000"],
        "amounts_human": ["0.01", "0.02"],
        **overrides,
    }


async def run_tool(context: MagicMock, next_call: MagicMock, args: dict[str, Any] | None = None) -> Any:
    execute = callback(context, "register_tool_execution_intercept")
    return await execute(ToolExecutionContext(tool_name=TOOL_NAME, args=args or converted_args()), next_call)


class TestManifest:
    @staticmethod
    def manifest() -> dict[str, Any]:
        return tomllib.loads((ROOT / "relay-plugin.toml").read_text(encoding="utf-8"))

    def test_digest_matches_worker_source(self) -> None:
        manifest = self.manifest()
        actual = hashlib.sha256((ROOT / manifest["source"]["artifact"]).read_bytes()).hexdigest()

        assert manifest["integrity"]["sha256"] == f"sha256:{actual}"

    def test_declares_the_published_worker_protocol(self) -> None:
        assert self.manifest()["compat"] == {"relay": ">=0.11.0,<1.0", "worker_protocol": "grpc-v1"}

    def test_identity_matches_the_plugin(self) -> None:
        manifest = self.manifest()

        assert manifest["plugin"]["id"] == SpraayBatchPlugin.plugin_id == PLUGIN_ID
        assert manifest["load"]["entrypoint"] == "nemo_relay_spraay.worker:main"
        assert manifest["defaults"]["enabled"] is False

    def test_schema_covers_exactly_the_supported_fields(self) -> None:
        schema = json.loads((ROOT / self.manifest()["config_schema"]["path"]).read_text(encoding="utf-8"))

        assert schema["additionalProperties"] is False
        assert set(schema["properties"]) == set(worker.DEFAULT_CONFIG) | worker.OPTIONAL_FIELDS

    def test_package_declares_no_signing_dependency(self) -> None:
        required = " ".join(metadata.requires("nemo-relay-spraay") or []).lower()

        assert not any(name in required for name in ("eth-account", "eth_account", "web3", "x402"))


class TestAmounts:
    @pytest.mark.parametrize(
        ("value", "decimals", "raw"),
        [
            ("1.5", 6, 1_500_000),
            ("0.000001", 6, 1),
            (2, 6, 2_000_000),
            (Decimal("0.01"), 6, 10_000),
            (" 3 ", 6, 3_000_000),
            ("123456789012345678901234567890.123456789012345678", 18, 123456789012345678901234567890123456789012345678),
        ],
    )
    def test_converts_exactly(self, value: object, decimals: int, raw: int) -> None:
        assert to_raw_units(value, decimals) == raw

    @pytest.mark.parametrize("value", [0.1, True, None, "abc", "NaN", "Infinity", "0", "-1", "0.0000001"])
    def test_rejects_inexact_or_non_positive(self, value: object) -> None:
        with pytest.raises(AmountError):
            to_raw_units(value, 6)

    def test_zero_is_allowed_only_when_requested(self) -> None:
        assert to_raw_units("0", 6, allow_zero=True) == 0

    @pytest.mark.parametrize(("raw", "human"), [(1_500_000, "1.5"), (2_000_000, "2"), (1, "0.000001"), (10**7, "10")])
    def test_formats_without_trailing_zeros(self, raw: int, human: str) -> None:
        assert to_human_units(raw, 6) == human


class TestConfig:
    def test_defaults_are_valid(self) -> None:
        assert SpraayBatchPlugin().validate({}) == []

    @pytest.mark.parametrize(
        ("config", "suffix"),
        [
            ("nope", "invalid_config"),
            ({"chains": ["solana"]}, "invalid_chains"),
            ({"chains": []}, "invalid_chains"),
            ({"max_recipients": 201}, "invalid_max_recipients"),
            ({"max_recipients": True}, "invalid_max_recipients"),
            ({"redact_recipients": "yes"}, "invalid_config"),
            ({"priority": "high"}, "invalid_config"),
            ({"max_access_fee_raw": -1}, "invalid_config"),
            ({"gateway_url": "gateway.spraay.app"}, "invalid_config"),
            ({"nope": 1}, "unknown_field"),
        ],
    )
    def test_invalid_values_are_errors(self, config: Any, suffix: str) -> None:
        assert SpraayBatchPlugin().validate(config)[0].code == f"{PLUGIN_ID}.{suffix}"

    def test_register_refuses_invalid_config(self) -> None:
        with pytest.raises(ValueError, match="chains"):
            SpraayBatchPlugin().register(MagicMock(spec=PluginContext), {"chains": ["solana"]})

    def test_registers_every_surface(self) -> None:
        _implementation, context = registered()

        for method in (
            "register_tool_conditional_execution_guardrail",
            "register_tool_request_intercept",
            "register_tool_execution_intercept",
            "register_tool_sanitize_request_guardrail",
            "register_tool_sanitize_response_guardrail",
            "register_event_metadata_injector",
            "register_subscriber",
        ):
            assert getattr(context, method).call_count == 1, method

    def test_redaction_can_be_turned_off(self) -> None:
        _implementation, context = registered({"redact_recipients": False})

        assert context.register_tool_sanitize_request_guardrail.call_count == 0


class TestGuardrail:
    @pytest.mark.parametrize(
        ("overrides", "reason"),
        [
            ({"chain": "solana"}, "not enabled"),
            ({"chain": "unichain"}, "not enabled"),
            ({"token": ""}, "token"),
            ({"sender": "0xnope"}, "sender"),
            ({"sender": None}, "sender"),
            ({"payments": []}, "non-empty"),
            ({"payments": ["x"]}, "payments\\[0\\]"),
            ({"payments": [{"to": "0xnope", "amount": "1"}]}, "payments\\[0\\].to"),
            ({"payments": [{"to": RECIPIENTS[0], "amount": "1"}] * 201}, "201"),
            ({"payments": [{"to": RECIPIENTS[0], "amount": "0"}]}, "payments\\[0\\].amount"),
            ({"payments": [{"to": RECIPIENTS[0], "amount": 1.5}]}, "payments\\[0\\].amount"),
            ({"payments": [{"to": RECIPIENTS[0], "amount": "0.0000001"}], "decimals": 6}, "decimal places"),
            ({"decimals": -1}, "decimals"),
        ],
    )
    def test_rejects_malformed_batches(self, overrides: dict[str, Any], reason: str) -> None:
        _implementation, context = registered()
        rejection = callback(context, "register_tool_conditional_execution_guardrail")(
            TOOL_NAME, request_args(**overrides)
        )

        assert rejection is not None
        assert re.search(reason, rejection)

    def test_two_hundred_recipients_is_allowed(self) -> None:
        _implementation, context = registered()
        guardrail = callback(context, "register_tool_conditional_execution_guardrail")

        assert guardrail(TOOL_NAME, request_args(payments=[{"to": RECIPIENTS[0], "amount": "1"}] * 200)) is None

    def test_other_tools_are_untouched(self) -> None:
        _implementation, context = registered()

        assert callback(context, "register_tool_conditional_execution_guardrail")("other", {"chain": "solana"}) is None

    def test_chain_can_be_narrowed_to_base(self) -> None:
        _implementation, context = registered({"chains": ["base"]})
        rejection = callback(context, "register_tool_conditional_execution_guardrail")(
            TOOL_NAME, request_args(chain="peaq")
        )

        assert "not enabled" in rejection


class TestConversion:
    async def test_human_amounts_become_raw_once(self, fake: FakeGateway) -> None:
        _implementation, context = registered()
        converted = await callback(context, "register_tool_request_intercept")(TOOL_NAME, request_args())

        assert converted == converted_args()

    async def test_catalog_supplies_missing_decimals(self, fake: FakeGateway) -> None:
        _implementation, context = registered()
        await callback(context, "register_tool_request_intercept")(TOOL_NAME, request_args())

        assert fake.paths() == ["/api/v1/tokens"]

    async def test_explicit_decimals_skip_the_catalog(self, fake: FakeGateway) -> None:
        _implementation, context = registered()
        converted = await callback(context, "register_tool_request_intercept")(TOOL_NAME, request_args(decimals=6))

        assert converted["amounts"] == ["10000", "20000"]
        assert fake.requests == []

    async def test_unknown_token_needs_explicit_decimals(self, fake: FakeGateway) -> None:
        _implementation, context = registered()

        with pytest.raises(worker.SpraayError, match="pass decimals"):
            await callback(context, "register_tool_request_intercept")(TOOL_NAME, request_args(token="0xabc"))

    async def test_excess_precision_is_refused_after_catalog_lookup(self, fake: FakeGateway) -> None:
        _implementation, context = registered()
        args = request_args(payments=[{"to": RECIPIENTS[0], "amount": "0.0000001"}])

        with pytest.raises(AmountError):
            await callback(context, "register_tool_request_intercept")(TOOL_NAME, args)

    async def test_other_tools_are_untouched(self, fake: FakeGateway) -> None:
        _implementation, context = registered()

        assert await callback(context, "register_tool_request_intercept")("other", {"a": 1}) == {"a": 1}


class TestBuild:
    async def test_replays_live_response_into_an_unsigned_transaction(self, fake: FakeGateway) -> None:
        _implementation, context = registered()
        outcome = await run_tool(context, paying_next())

        assert outcome.result["status"] == "unsigned"
        assert outcome.result["transaction"] == PROOF["response"]["json"]["transaction"]
        assert outcome.result["approval_required"]["amount"] == "30090"
        assert outcome.result["batch"]["fee_raw"] == "90"

    async def test_execute_body_is_flat_raw_units_with_sender(self, fake: FakeGateway) -> None:
        _implementation, context = registered()
        await run_tool(context, paying_next())

        assert json.loads(fake.execute_calls()[0].content) == PROOF["request"]["json"]

    async def test_preflights_run_before_any_paid_call(self, fake: FakeGateway) -> None:
        _implementation, context = registered()
        await run_tool(context, paying_next())

        assert fake.paths()[:2] == ["/free/estimate-batch", "/free/validate-batch"]

    async def test_estimate_is_a_get_with_human_amount(self, fake: FakeGateway) -> None:
        _implementation, context = registered()
        await run_tool(context, paying_next())

        request = fake.requests[0]
        assert request.method == "GET"
        assert dict(request.url.params) == {"chain": "base", "recipients": "2", "amount": "0.03", "token": "USDC"}

    async def test_validate_posts_the_native_recipient_objects(self, fake: FakeGateway) -> None:
        _implementation, context = registered()
        await run_tool(context, paying_next())

        assert json.loads(fake.requests[1].content) == {
            "chain": "base",
            "token": "USDC",
            "sender": SENDER,
            "recipients": PAYMENTS,
        }

    async def test_unsupported_estimate_stops_before_payment(self, fake: FakeGateway) -> None:
        fake.estimate = {"estimate": {**ESTIMATE["estimate"], "supported": False}}
        _implementation, context = registered()

        with pytest.raises(UnsupportedChainError):
            await run_tool(context, paying_next())

        assert fake.execute_calls() == []

    async def test_failed_validation_stops_before_payment(self, fake: FakeGateway) -> None:
        fake.validation = {"valid": False, "errors": ["bad"]}
        _implementation, context = registered()

        with pytest.raises(BatchValidationError, match="bad"):
            await run_tool(context, paying_next())

        assert fake.execute_calls() == []

    @pytest.mark.parametrize(
        ("overrides", "reason"),
        [
            ({"amounts": ["10001", "20000"]}, "disagree"),
            ({"amounts": ["0", "20000"]}, "amounts\\[0\\]"),
            ({"amounts": ["1.5", "20000"]}, "amounts\\[0\\]"),
            ({"amounts": ["10000"]}, "one entry"),
            ({"recipients": ["0xnope", RECIPIENTS[1]]}, "recipients\\[0\\]"),
        ],
    )
    async def test_tampered_arguments_are_refused_before_any_call(
        self, fake: FakeGateway, overrides: dict[str, Any], reason: str
    ) -> None:
        _implementation, context = registered()

        with pytest.raises(BatchValidationError, match=reason):
            await run_tool(context, paying_next(), converted_args(**overrides))

        assert fake.requests == []

    async def test_other_tools_pass_through_to_the_app(self, fake: FakeGateway) -> None:
        _implementation, context = registered()
        next_call = MagicMock()
        next_call.call = AsyncMock(return_value=ToolExecutionResult({"ran": True}, {"note": "kept"}))
        execute = callback(context, "register_tool_execution_intercept")

        outcome = await execute(ToolExecutionContext(tool_name="other", args={"a": 1}), next_call)

        assert (outcome.result, outcome.annotation) == ({"ran": True}, {"note": "kept"})
        assert fake.requests == []


class TestPayment:
    async def test_app_callback_receives_only_options_within_the_cap(self, fake: FakeGateway) -> None:
        _implementation, context = registered()
        next_call = paying_next()
        await run_tool(context, next_call)

        challenge = next_call.call.call_args.args[0][CHALLENGE_ARG]
        assert [item["network"] for item in challenge["accepts"]] == ["eip155:8453"]
        assert challenge["url"] == f"{GATEWAY}/api/v1/batch/execute"

    async def test_app_headers_are_attached_to_the_one_retry(self, fake: FakeGateway) -> None:
        _implementation, context = registered()
        await run_tool(context, paying_next())

        assert len(fake.execute_calls()) == 2
        assert fake.execute_calls()[1].headers["payment-signature"] == "test-sig-token"

    async def test_payment_headers_never_reach_the_result(self, fake: FakeGateway) -> None:
        _implementation, context = registered()
        outcome = await run_tool(context, paying_next())

        assert "test-sig-token" not in json.dumps(outcome.result)

    async def test_no_payment_returns_the_challenge(self, fake: FakeGateway) -> None:
        _implementation, context = registered()
        outcome = await run_tool(context, declining_next())

        assert outcome.result["status"] == "payment_required"
        assert outcome.result["challenge"]["accepts"][0]["amount"] == "20000"
        assert len(fake.execute_calls()) == 1

    async def test_fee_above_cap_never_reaches_the_app(self, fake: FakeGateway) -> None:
        _implementation, context = registered({"max_access_fee_raw": 19999})
        next_call = paying_next()

        with pytest.raises(AccessFeeTooHighError):
            await run_tool(context, next_call)

        next_call.call.assert_not_awaited()

    async def test_rejected_payment_is_not_retried(self, fake: FakeGateway) -> None:
        fake.paid_status = 402
        _implementation, context = registered()
        next_call = paying_next()

        with pytest.raises(GatewayResponseError):
            await run_tool(context, next_call)

        assert next_call.call.await_count == 1
        assert len(fake.execute_calls()) == 2

    async def test_access_fee_receipt_comes_from_the_payment_response_header(self, fake: FakeGateway) -> None:
        _implementation, context = registered()
        outcome = await run_tool(context, paying_next())

        assert outcome.result["access_fee"] == {
            "tx_hash": PROOF["response"]["payment_response_decoded"]["transaction"],
            "network": "eip155:8453",
            "amount_raw": "20000",
        }

    async def test_html_200_from_a_wrong_base_url_is_an_error(self, httpx_mock: Any) -> None:
        httpx_mock.add_response(status_code=200, html="<!DOCTYPE html>", is_reusable=True)
        _implementation, context = registered()

        with pytest.raises(GatewayResponseError, match="gateway URL"):
            await run_tool(context, paying_next())


class TestResponseVerification:
    @pytest.mark.parametrize(
        ("path", "value"),
        [
            (("token", "decimals"), 18),
            (("transaction", "chainId"), 3338),
            (("batch", "recipientCount"), 3),
            (("batch", "totalAmount"), "0.04"),
            (("approvalRequired", "amount"), "99999999"),
            (("transaction", "data"), "0xfb83b683"),
            (("success",), False),
        ],
    )
    async def test_mismatched_response_is_refused(self, fake: FakeGateway, path: tuple[str, ...], value: Any) -> None:
        body = deepcopy(fake.paid_body)
        target = body
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        fake.paid_body = body
        _implementation, context = registered()

        with pytest.raises(ResponseMismatchError):
            await run_tool(context, paying_next())

    async def test_plural_transactions_shape_is_refused(self, fake: FakeGateway) -> None:
        body = deepcopy(fake.paid_body)
        body["transactions"] = [body.pop("transaction")]
        fake.paid_body = body
        _implementation, context = registered()

        with pytest.raises(ResponseMismatchError):
            await run_tool(context, paying_next())


class TestEvents:
    def test_request_sanitizer_hides_addresses(self) -> None:
        _implementation, context = registered()
        sanitized = callback(context, "register_tool_sanitize_request_guardrail")(TOOL_NAME, converted_args())

        assert sanitized["recipients"] == sanitized["sender"] == "[REDACTED]"
        assert sanitized["amounts"] == ["10000", "20000"]

    def test_response_sanitizer_hides_calldata(self) -> None:
        _implementation, context = registered()
        sanitized = callback(context, "register_tool_sanitize_response_guardrail")(
            TOOL_NAME, {"status": "unsigned", "transaction": {"to": "0xc", "data": "0xfeed"}}
        )

        assert sanitized["transaction"] == {"to": "0xc", "data": "[REDACTED]"}

    def test_sanitizers_leave_other_tools_alone(self) -> None:
        _implementation, context = registered()

        assert callback(context, "register_tool_sanitize_request_guardrail")("other", {"sender": "x"}) == {
            "sender": "x"
        }

    async def test_addresses_and_calldata_stay_out_of_end_event_data(self, fake: FakeGateway) -> None:
        _implementation, context = registered()
        outcome = await run_tool(context, paying_next())
        sanitized = callback(context, "register_tool_sanitize_response_guardrail")(TOOL_NAME, outcome.result)
        emitted = json.dumps(sanitized).lower()

        for address in [SENDER, *RECIPIENTS]:
            assert address[2:].lower() not in emitted

    def test_start_metadata_surfaces_human_and_raw_amounts(self) -> None:
        _implementation, context = registered()
        injector = callback(context, "register_event_metadata_injector")
        metadata_added = injector(
            {"name": TOOL_NAME, "category": "tool", "scope_category": "start", "data": converted_args()}
        )

        assert metadata_added["spraay.amounts_human"] == ["0.01", "0.02"]
        assert metadata_added["spraay.amounts_raw"] == ["10000", "20000"]
        assert metadata_added["spraay.recipient_count"] == 2

    async def test_end_metadata_carries_spend(self, fake: FakeGateway) -> None:
        _implementation, context = registered()
        outcome = await run_tool(context, paying_next())
        injector = callback(context, "register_event_metadata_injector")
        added = injector({"name": TOOL_NAME, "category": "tool", "scope_category": "end", "data": outcome.result})

        assert added["spraay.status"] == "unsigned"
        assert added["spraay.protocol_fee_raw"] == "90"
        assert added["spraay.access_fee_amount_raw"] == "20000"

    def test_injector_ignores_other_events(self) -> None:
        _implementation, context = registered()
        injector = callback(context, "register_event_metadata_injector")

        assert injector({"name": "other", "category": "tool", "scope_category": "start", "data": {}}) == {}

    async def test_subscriber_records_and_marks_each_built_batch(self) -> None:
        implementation, context = registered()
        record = {"spraay.status": "unsigned", "spraay.total_raw": "30000"}
        event = {"name": TOOL_NAME, "scope_category": "end", "metadata": {**record, "otel.status_code": "OK"}}

        await callback(context, "register_subscriber")(event)

        assert implementation.built_batches == [record]
        context.runtime.emit_mark.assert_awaited_once_with(BUILT_MARK, record)

    async def test_subscriber_ignores_unbuilt_outcomes(self) -> None:
        implementation, context = registered()
        event = {"name": TOOL_NAME, "scope_category": "end", "metadata": {"otel.status_code": "ERROR"}}

        await callback(context, "register_subscriber")(event)

        assert implementation.built_batches == []
        context.runtime.emit_mark.assert_not_awaited()
