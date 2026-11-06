# SPDX-License-Identifier: Apache-2.0
"""Fixtures that replay the recorded paid execute response. No test touches the network."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from nemo_relay import plugin

from nemo_relay_spraay import PLUGIN_KIND, SpraayGateway, SpraayPaymentPlugin, component

PROOF = json.loads((Path(__file__).parents[1] / "docs" / "live-execute-proof.json").read_text(encoding="utf-8"))
SENDER = PROOF["request"]["json"]["sender"]
RECIPIENTS = PROOF["request"]["json"]["recipients"]
PAYMENTS = [{"to": RECIPIENTS[0], "amount": "0.01"}, {"to": RECIPIENTS[1], "amount": "0.02"}]

CHALLENGE = {
    "x402Version": 2,
    "error": "Payment required",
    "accepts": [PROOF["challenge_paid"], {**PROOF["challenge_paid"], "network": "eip155:4663", "amount": "99999"}],
    # The stale transactions[] example really is in the live 402 body and must be ignored.
    "extensions": {"bazaar": {"info": {"output": {"example": {"transactions": []}}}}},
}
ESTIMATE = {
    "estimate": {
        "chain": "base",
        "recipients": 2,
        "totalAmount": 0.03,
        "protocolFeeBps": 30,
        "estimatedGasUSD": 0.046,
        "supported": True,
        "supportedChains": ["base", "peaq"],
    }
}
TOKENS = {"popularTokens": {"USDC": {"address": PROOF["response"]["json"]["token"]["address"], "decimals": 6}}}


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


def pay_hook(_challenge: Any) -> dict[str, str]:
    return {"payment-signature": "test-signature"}


@pytest.fixture
def fake() -> FakeGateway:
    return FakeGateway()


@pytest_asyncio.fixture
async def make_gateway(fake: FakeGateway) -> AsyncIterator[Callable[..., SpraayGateway]]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake))

    def build(**kwargs: Any) -> SpraayGateway:
        return SpraayGateway("https://gateway.test", http=http, **kwargs)

    yield build
    await http.aclose()


@pytest.fixture
def relay_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))


@pytest_asyncio.fixture
async def active_plugin(relay_home: None) -> AsyncIterator[SpraayPaymentPlugin]:
    implementation = SpraayPaymentPlugin()
    plugin.register(PLUGIN_KIND, implementation)
    activation = None
    try:
        activation = await plugin.initialize(plugin.PluginConfig(components=[component()]))
        assert activation.report["config"]["diagnostics"] == []
        yield implementation
    finally:
        if activation is not None:
            await activation.close()
        plugin.deregister(PLUGIN_KIND)
