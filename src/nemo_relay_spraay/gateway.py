# SPDX-License-Identifier: Apache-2.0
"""HTTP client for the Spraay gateway. Holds no key and signs nothing."""

from __future__ import annotations

import base64
import inspect
import json
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

DEFAULT_GATEWAY_URL = "https://gateway.spraay.app"
DEFAULT_MAX_ACCESS_FEE_RAW = 20000  # $0.02 in 6-decimal stablecoin base units.


class SpraayError(Exception):
    """Base class for errors raised by this package."""


class GatewayResponseError(SpraayError):
    """The gateway answered with an error status or a non-JSON body."""

    def __init__(self, message: str, *, status: int, body: str = "") -> None:
        super().__init__(f"{message} (HTTP {status})")
        self.status = status
        self.body = body


class PaymentRequiredError(SpraayError):
    """The gateway requires x402 payment and no payment hook is configured."""

    def __init__(self, challenge: PaymentChallenge) -> None:
        super().__init__(f"{challenge.url} requires x402 payment and no payment_hook is configured")
        self.challenge = challenge


class AccessFeeTooHighError(SpraayError):
    """Every payment option in the challenge costs more than the configured cap."""


@dataclass(frozen=True)
class PaymentChallenge:
    """A parsed 402 response. ``accepts`` holds only options within the fee cap."""

    url: str
    accepts: list[dict[str, Any]]
    body: dict[str, Any]


PaymentHook = Callable[[PaymentChallenge], Mapping[str, str] | Awaitable[Mapping[str, str]]]
"""App-supplied callback: pay the challenge and return the headers for the retry."""


@dataclass(frozen=True)
class ExecuteResponse:
    """The gateway's execute body, plus the access-fee receipt when one was paid."""

    body: dict[str, Any]
    access_fee: dict[str, Any] | None


def _decode_receipt(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    try:
        decoded = json.loads(base64.b64decode(value + "=" * (-len(value) % 4)))
    except ValueError:
        return None
    return decoded if isinstance(decoded, dict) else None


def _json_body(response: httpx.Response) -> dict[str, Any]:
    # A wrong base URL can answer 200 with an HTML page, so status alone proves nothing.
    if "json" not in response.headers.get("content-type", ""):
        raise GatewayResponseError(
            f"expected JSON from {response.request.url}, is the gateway URL correct?",
            status=response.status_code,
            body=response.text[:500],
        )
    body = response.json()
    if not isinstance(body, dict):
        raise GatewayResponseError("expected a JSON object", status=response.status_code, body=response.text[:500])
    return body


def _checked_body(response: httpx.Response) -> dict[str, Any]:
    if response.status_code != 200:
        raise GatewayResponseError(
            f"gateway rejected {response.request.url.path}", status=response.status_code, body=response.text[:500]
        )
    return _json_body(response)


class SpraayGateway:
    """Calls the live gateway endpoints this plugin depends on.

    Args:
        url: Gateway base URL. Defaults to ``SPRAAY_GATEWAY_URL``, then the public gateway.
        payment_hook: Pays a 402 challenge. Without one, paid calls raise
            ``PaymentRequiredError`` carrying the challenge.
        max_access_fee_raw: Largest access fee, in raw units, offered to the hook.
        http: Optional client, for tests or custom transports. The caller owns it.
    """

    def __init__(
        self,
        url: str | None = None,
        *,
        payment_hook: PaymentHook | None = None,
        max_access_fee_raw: int = DEFAULT_MAX_ACCESS_FEE_RAW,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.url = (url or os.environ.get("SPRAAY_GATEWAY_URL") or DEFAULT_GATEWAY_URL).rstrip("/")
        self._payment_hook = payment_hook
        self._max_access_fee_raw = max_access_fee_raw
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(timeout=60)

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> SpraayGateway:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def tokens(self) -> dict[str, Any]:
        """``GET /api/v1/tokens`` (free)."""
        return _checked_body(await self._http.get(f"{self.url}/api/v1/tokens"))

    async def estimate(self, *, chain: str, recipients: int, total_human: str, token: str) -> dict[str, Any]:
        """``GET /free/estimate-batch`` (free). ``amount`` is in human units on this endpoint."""
        params = {"chain": chain, "recipients": recipients, "amount": total_human, "token": token}
        body = _checked_body(await self._http.get(f"{self.url}/free/estimate-batch", params=params))
        estimate = body.get("estimate")
        if not isinstance(estimate, dict):
            raise GatewayResponseError("estimate response has no estimate object", status=200, body=str(body)[:500])
        return estimate

    async def validate(self, *, chain: str, token: str, sender: str, payments: list[dict[str, str]]) -> dict[str, Any]:
        """``POST /free/validate-batch`` (free). Takes ``recipients: [{to, amount}]``."""
        payload = {"chain": chain, "token": token, "sender": sender, "recipients": payments}
        return _checked_body(await self._http.post(f"{self.url}/free/validate-batch", json=payload))

    async def execute(
        self, *, token: str, chain: str, sender: str, recipients: list[str], amounts_raw: list[str]
    ) -> ExecuteResponse:
        """``POST /api/v1/batch/execute`` (x402). Returns an unsigned transaction."""
        url = f"{self.url}/api/v1/batch/execute"
        payload = {"token": token, "chain": chain, "sender": sender, "recipients": recipients, "amounts": amounts_raw}
        response = await self._http.post(url, json=payload)
        accepts: list[dict[str, Any]] = []
        if response.status_code == 402:
            challenge = self._challenge(url, _json_body(response))
            accepts = challenge.accepts
            response = await self._http.post(url, json=payload, headers=dict(await self._pay(challenge)))
        body = _checked_body(response)
        return ExecuteResponse(body, self._receipt(response, accepts))

    def _challenge(self, url: str, body: dict[str, Any]) -> PaymentChallenge:
        offered = body.get("accepts")
        offered = offered if isinstance(offered, list) else []
        accepts = [item for item in offered if self._within_cap(item)]
        challenge = PaymentChallenge(url=url, accepts=accepts, body=body)
        if self._payment_hook is None:
            raise PaymentRequiredError(challenge)
        if not accepts:
            raise AccessFeeTooHighError(f"no payment option at or below {self._max_access_fee_raw} raw units")
        return challenge

    def _within_cap(self, item: Any) -> bool:
        try:
            return int(item["amount"]) <= self._max_access_fee_raw
        except (KeyError, TypeError, ValueError):
            return False

    async def _pay(self, challenge: PaymentChallenge) -> Mapping[str, str]:
        assert self._payment_hook is not None
        headers = self._payment_hook(challenge)
        return await headers if inspect.isawaitable(headers) else headers

    @staticmethod
    def _receipt(response: httpx.Response, accepts: list[dict[str, Any]]) -> dict[str, Any] | None:
        receipt = _decode_receipt(
            response.headers.get("payment-response") or response.headers.get("x-payment-response")
        )
        if receipt is None:
            return None
        network = receipt.get("network")
        match = next((item for item in accepts if item.get("network") == network), {})
        return {"tx_hash": receipt.get("transaction"), "network": network, "amount_raw": match.get("amount")}
