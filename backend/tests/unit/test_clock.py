from datetime import UTC, datetime

import pytest
from bidscope.clock import FixedClock, SystemClock


def test_fixed_clock_returns_the_exact_same_value_each_time() -> None:
    value = datetime(2026, 7, 18, tzinfo=UTC)
    clock = FixedClock(value)

    assert clock.now() == value
    assert clock.now() == value
    assert clock.now() is value


def test_system_clock_returns_a_timezone_aware_datetime() -> None:
    assert SystemClock().now().tzinfo is not None


def test_fixed_clock_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        FixedClock(datetime(2026, 7, 18))
