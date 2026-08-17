from vnedge.performance import profit_factor


def test_profit_factor_is_finite_or_explicitly_undefined() -> None:
    assert profit_factor(40.0, 10.0) == 4.0
    assert profit_factor(40.0, 0.0) is None
    assert profit_factor(0.0, 0.0) is None


def test_profit_factor_normalizes_invalid_negative_totals() -> None:
    assert profit_factor(-1.0, 10.0) == 0.0
    assert profit_factor(10.0, -1.0) is None
