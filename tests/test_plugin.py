# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

import nemo_relay
import pytest
from conftest import PAYMENTS, PROOF, RECIPIENTS, SENDER, pay_hook
from nemo_relay import plugin, subscribers, tools

from nemo_relay_spraay import (
    PLUGIN_KIND,
    TOOL_NAME,
    BatchValidationError,
    ResponseMismatchError,
    SpraayPaymentPlugin,
    UnsupportedChainError,
    build_batch_transaction,
    component,
)


async def build(gateway: Any, **overrides: Any) -> Any:
    kwargs = {"chain": "base", "token": "USDC", "sender": SENDER, "payments": PAYMENTS, **overrides}
    return await build_batch_transaction(gateway, **kwargs)


class TestBuild:
    async def test_replays_live_response_into_an_unsigned_transaction(
        self, active_plugin: Any, make_gateway: Any
    ) -> None:
        outcome = await build(make_gateway(payment_hook=pay_hook))

        assert outcome.result["status"] == "unsigned"
        assert outcome.result["transaction"] == PROOF["response"]["json"]["transaction"]
        assert outcome.result["approval_required"]["amount"] == "30090"
        assert outcome.result["batch"]["fee_raw"] == "90"

    async def test_human_amounts_are_converted_once_to_the_recorded_request(
        self, active_plugin: Any, fake: Any, make_gateway: Any
    ) -> None:
        await build(make_gateway(payment_hook=pay_hook))

        assert json.loads(fake.execute_calls()[0].content) == PROOF["request"]["json"]

    async def test_preflights_run_before_any_paid_call(self, active_plugin: Any, fake: Any, make_gateway: Any) -> None:
        await build(make_gateway(payment_hook=pay_hook))

        assert fake.paths()[:3] == ["/api/v1/tokens", "/free/estimate-batch", "/free/validate-batch"]

    async def test_explicit_decimals_skip_the_catalog(self, active_plugin: Any, fake: Any, make_gateway: Any) -> None:
        await build(make_gateway(payment_hook=pay_hook), decimals=6)

        assert "/api/v1/tokens" not in fake.paths()

    async def test_unsupported_estimate_stops_before_payment(
        self, active_plugin: Any, fake: Any, make_gateway: Any
    ) -> None:
        fake.estimate = {"estimate": {**fake.estimate["estimate"], "supported": False}}

        with pytest.raises(UnsupportedChainError):
            await build(make_gateway(payment_hook=pay_hook))

        assert fake.execute_calls() == []

    async def test_failed_validation_stops_before_payment(
        self, active_plugin: Any, fake: Any, make_gateway: Any
    ) -> None:
        fake.validation = {"valid": False, "errors": ["bad"]}

        with pytest.raises(BatchValidationError, match="bad"):
            await build(make_gateway(payment_hook=pay_hook))

        assert fake.execute_calls() == []


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
    async def test_mismatched_response_is_refused(
        self, active_plugin: Any, fake: Any, make_gateway: Any, path: tuple[str, ...], value: Any
    ) -> None:
        body = deepcopy(fake.paid_body)
        target = body
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        fake.paid_body = body

        with pytest.raises(ResponseMismatchError):
            await build(make_gateway(payment_hook=pay_hook))

    async def test_plural_transactions_shape_is_refused(self, active_plugin: Any, fake: Any, make_gateway: Any) -> None:
        body = deepcopy(fake.paid_body)
        body["transactions"] = [body.pop("transaction")]
        fake.paid_body = body

        with pytest.raises(ResponseMismatchError):
            await build(make_gateway(payment_hook=pay_hook))


def batch_args(**overrides: Any) -> dict[str, Any]:
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


class TestGuardrail:
    @pytest.mark.parametrize(
        ("overrides", "reason"),
        [
            ({"chain": "solana"}, "not enabled"),
            ({"chain": "unichain"}, "not enabled"),
            ({"sender": "0xnope"}, "sender"),
            ({"recipients": []}, "non-empty"),
            ({"recipients": ["0xnope", RECIPIENTS[1]]}, "recipients\\[0\\]"),
            ({"recipients": [RECIPIENTS[0]] * 201, "amounts": ["1"] * 201, "amounts_human": ["0.000001"] * 201}, "201"),
            ({"amounts": ["10000"]}, "one entry per recipient"),
            ({"amounts": ["0", "20000"]}, "amounts\\[0\\]"),
            ({"amounts": ["1.5", "20000"]}, "amounts\\[0\\]"),
            ({"amounts": ["10001", "20000"]}, "disagree"),
            ({"amounts_human": ["0.01"]}, "amounts_human"),
        ],
    )
    async def test_rejected_batch_never_reaches_the_callback(
        self, active_plugin: Any, overrides: dict[str, Any], reason: str
    ) -> None:
        with pytest.raises(RuntimeError, match=f"guardrail rejected.*{reason}"):
            await tools.execute(TOOL_NAME, batch_args(**overrides), lambda _args: pytest.fail("must not run"))

    async def test_two_hundred_recipients_is_allowed(self, active_plugin: Any) -> None:
        args = batch_args(recipients=[RECIPIENTS[0]] * 200, amounts=["1"] * 200, amounts_human=["0.000001"] * 200)

        outcome = await tools.execute(TOOL_NAME, args, lambda _args: nemo_relay.ToolExecutionResult({"ok": True}, None))

        assert outcome.result == {"ok": True}

    async def test_bad_batch_costs_no_network_call(self, active_plugin: Any, fake: Any, make_gateway: Any) -> None:
        with pytest.raises(RuntimeError, match="guardrail rejected"):
            await build(make_gateway(payment_hook=pay_hook), chain="solana", decimals=6)

        assert fake.requests == []

    async def test_other_tools_are_untouched(self, active_plugin: Any) -> None:
        outcome = await tools.execute(
            "other_tool", {"recipients": "x"}, lambda args: nemo_relay.ToolExecutionResult(args, None)
        )

        assert outcome.result == {"recipients": "x"}


class TestEvents:
    @staticmethod
    async def capture(gateway: Any) -> list[Any]:
        events: list[Any] = []
        subscribers.register("capture", events.append)
        try:
            await build(gateway)
            await subscribers.flush_async()
        finally:
            subscribers.deregister("capture")
        return [event for event in events if event.name == TOOL_NAME and event.category == "tool"]

    async def test_metadata_surfaces_human_and_raw_amounts(self, active_plugin: Any, make_gateway: Any) -> None:
        start, _end = await self.capture(make_gateway(payment_hook=pay_hook))

        assert start.metadata["spraay.amounts_human"] == ["0.01", "0.02"]
        assert start.metadata["spraay.amounts_raw"] == ["10000", "20000"]
        assert start.metadata["spraay.recipient_count"] == 2

    async def test_end_event_carries_spend(self, active_plugin: Any, make_gateway: Any) -> None:
        _start, end = await self.capture(make_gateway(payment_hook=pay_hook))

        assert end.metadata["spraay.status"] == "unsigned"
        assert end.metadata["spraay.protocol_fee_raw"] == "90"
        assert end.metadata["spraay.access_fee_amount_raw"] == "20000"
        assert end.metadata["spraay.access_fee_tx_hash"] == PROOF["response"]["payment_response_decoded"]["transaction"]

    async def test_addresses_and_calldata_stay_out_of_events(self, active_plugin: Any, make_gateway: Any) -> None:
        start, end = await self.capture(make_gateway(payment_hook=pay_hook))
        emitted = json.dumps([start.data, start.metadata, end.data, end.metadata]).lower()

        for address in [SENDER, *RECIPIENTS]:
            assert address[2:].lower() not in emitted

    async def test_callback_still_receives_real_addresses(
        self, active_plugin: Any, fake: Any, make_gateway: Any
    ) -> None:
        await self.capture(make_gateway(payment_hook=pay_hook))

        assert json.loads(fake.execute_calls()[0].content)["recipients"] == RECIPIENTS

    async def test_subscriber_records_each_built_batch(self, active_plugin: Any, make_gateway: Any) -> None:
        await self.capture(make_gateway(payment_hook=pay_hook))

        assert [item["spraay.total_raw"] for item in active_plugin.built_batches] == ["30000"]

    async def test_failed_build_records_no_spend(self, active_plugin: Any, fake: Any, make_gateway: Any) -> None:
        fake.validation = {"valid": False, "errors": ["bad"]}

        with pytest.raises(BatchValidationError):
            await build(make_gateway(payment_hook=pay_hook))
        await subscribers.flush_async()

        assert active_plugin.built_batches == []


class TestConfig:
    def test_defaults_are_valid(self) -> None:
        assert SpraayPaymentPlugin().validate({}) == []

    @pytest.mark.parametrize(
        ("config", "code"),
        [
            ({"chains": ["solana"]}, "invalid_chains"),
            ({"chains": []}, "invalid_chains"),
            ({"max_recipients": 201}, "invalid_max_recipients"),
            ({"max_recipients": True}, "invalid_max_recipients"),
            ({"redact_recipients": "yes"}, "invalid_config"),
            ({"priority": "high"}, "invalid_config"),
        ],
    )
    def test_invalid_values_are_errors(self, config: dict[str, Any], code: str) -> None:
        diagnostics = SpraayPaymentPlugin().validate(config)

        assert diagnostics[0]["level"] == "error"
        assert diagnostics[0]["code"] == f"spraay-payment.{code}"

    def test_unknown_field_is_a_warning(self) -> None:
        assert SpraayPaymentPlugin().validate({"nope": 1})[0]["level"] == "warning"

    async def test_chain_can_be_narrowed_to_base(self, relay_home: None) -> None:
        plugin.register(PLUGIN_KIND, SpraayPaymentPlugin())
        activation = None
        try:
            activation = await plugin.initialize(plugin.PluginConfig(components=[component(chains=["base"])]))
            with pytest.raises(RuntimeError, match="guardrail rejected"):
                await tools.execute(TOOL_NAME, batch_args(chain="peaq"), lambda _args: pytest.fail("must not run"))
        finally:
            if activation is not None:
                await activation.close()
            plugin.deregister(PLUGIN_KIND)

    async def test_teardown_removes_the_guardrail(self, relay_home: None) -> None:
        plugin.register(PLUGIN_KIND, SpraayPaymentPlugin())
        activation = await plugin.initialize(plugin.PluginConfig(components=[component()]))
        await activation.close()
        assert plugin.deregister(PLUGIN_KIND) is True

        outcome = await tools.execute(
            TOOL_NAME, batch_args(chain="solana"), lambda _args: nemo_relay.ToolExecutionResult({"ran": True}, None)
        )

        assert outcome.result == {"ran": True}
        assert PLUGIN_KIND not in plugin.list_kinds()
