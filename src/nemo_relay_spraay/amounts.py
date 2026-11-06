# SPDX-License-Identifier: Apache-2.0
"""Exact conversion between human-decimal amounts and raw base units."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, localcontext

_PRECISION = 120


class AmountError(ValueError):
    """An amount cannot be represented exactly in the token's base units."""


def _parse(value: object) -> Decimal:
    # Floats are refused: 0.1 + 0.2 style error has no place in a payment amount.
    if isinstance(value, (bool, float)) or not isinstance(value, (str, int, Decimal)):
        raise AmountError(f"amount must be a decimal string, int, or Decimal, not {type(value).__name__}")
    try:
        parsed = Decimal(value.strip() if isinstance(value, str) else value)
    except InvalidOperation:
        raise AmountError(f"amount {value!r} is not a decimal number") from None
    if not parsed.is_finite():
        raise AmountError(f"amount {value!r} is not finite")
    return parsed


def to_raw_units(value: object, decimals: int, *, allow_zero: bool = False) -> int:
    """Convert a human-decimal amount to raw base units, refusing any rounding."""
    parsed = _parse(value)
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
