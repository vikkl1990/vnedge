import pytest

from vnedge.data.symbols import canonical_symbol


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("BTC/USDT:USDT", "BTCUSDT"),
        ("BTCUSDT", "BTCUSDT"),
        (" btc-usdt ", "BTCUSDT"),
        ("BTC/USD:USD", "BTCUSD"),
    ],
)
def test_canonical_symbol_is_one_data_plane_identity(raw, expected):
    assert canonical_symbol(raw) == expected


def test_canonical_symbol_rejects_empty_identity():
    with pytest.raises(ValueError):
        canonical_symbol(" / :USD")
    with pytest.raises(TypeError):
        canonical_symbol(None)  # type: ignore[arg-type]
