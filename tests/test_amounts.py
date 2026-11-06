# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from decimal import Decimal

import pytest

from nemo_relay_spraay import AmountError, to_human_units, to_raw_units


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
def test_converts_exactly(value: object, decimals: int, raw: int) -> None:
    assert to_raw_units(value, decimals) == raw


@pytest.mark.parametrize("value", [0.1, True, None, "abc", "NaN", "Infinity", "0", "-1", "0.0000001"])
def test_rejects_inexact_or_non_positive(value: object) -> None:
    with pytest.raises(AmountError):
        to_raw_units(value, 6)


def test_zero_is_allowed_only_when_requested() -> None:
    assert to_raw_units("0", 6, allow_zero=True) == 0


@pytest.mark.parametrize(("raw", "human"), [(1_500_000, "1.5"), (2_000_000, "2"), (1, "0.000001"), (10_000_000, "10")])
def test_formats_without_trailing_zeros(raw: int, human: str) -> None:
    assert to_human_units(raw, 6) == human


def test_round_trip_is_stable() -> None:
    assert to_raw_units(to_human_units(30090, 6), 6) == 30090
