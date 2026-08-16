"""No-signal strategy used by the default observation runtime.

The runtime still ingests candles, evaluates data quality and Time Machine
health, publishes snapshots, and exercises the safety spine. This strategy
cannot create an entry intent, so measurement does not imply trade permission.
"""

from __future__ import annotations

import pandas as pd

from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent


class MeasurementOnly(BaseStrategy):
    strategy_id = "measurement_only_v1"
    warmup_bars = 1

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        prepared = candles.copy()
        prepared["measurement_only"] = True
        return prepared

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        del df, index
        return None
