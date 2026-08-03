"""Second-eye grid evidence contract tests."""

from research.second_eye_grid import _no_trade_cell_metrics


def test_second_eye_grid_records_no_trade_cells_explicitly():
    metrics = _no_trade_cell_metrics()

    assert metrics["n"] == 0
    assert metrics["no_trade_sample"] is True
    assert metrics["taker"]["avg_net_bps"] == 0.0
    assert metrics["maker"]["pf"] == 0.0
