# SPDX-License-Identifier: Apache-2.0
"""NeMo Relay plugin that builds unsigned batch transactions through the Spraay gateway.

The plugin builds an unsigned batch transaction; the app signs and broadcasts it.
"""

from .amounts import AmountError, to_human_units, to_raw_units
from .gateway import (
    AccessFeeTooHighError,
    ExecuteResponse,
    GatewayResponseError,
    PaymentChallenge,
    PaymentHook,
    PaymentRequiredError,
    SpraayError,
    SpraayGateway,
)
from .plugin import PLUGIN_KIND, SpraayPaymentPlugin, component
from .tool import (
    TOOL_NAME,
    BatchValidationError,
    ResponseMismatchError,
    UnsupportedChainError,
    build_batch_transaction,
)

__all__ = [
    "PLUGIN_KIND",
    "TOOL_NAME",
    "AccessFeeTooHighError",
    "AmountError",
    "BatchValidationError",
    "ExecuteResponse",
    "GatewayResponseError",
    "PaymentChallenge",
    "PaymentHook",
    "PaymentRequiredError",
    "ResponseMismatchError",
    "SpraayError",
    "SpraayGateway",
    "SpraayPaymentPlugin",
    "UnsupportedChainError",
    "build_batch_transaction",
    "component",
    "to_human_units",
    "to_raw_units",
]
