from datetime import UTC, datetime

import pytest

from binance_scanner.backtest import BacktestCandle
from binance_scanner.backtest_io import HistoricalCandle, load_candles_csv, write_candles_csv


def test_candle_csv_round_trip(tmp_path) -> None:
    path = tmp_path / "candles.csv"
    rows = [
        HistoricalCandle(datetime(2026, 1, 1, tzinfo=UTC), BacktestCandle(100, 101, 99, 100.5)),
        HistoricalCandle(
            datetime(2026, 1, 1, 0, 1, tzinfo=UTC), BacktestCandle(100.5, 102, 100, 101)
        ),
    ]
    write_candles_csv(path, rows)

    loaded = load_candles_csv(path)

    assert loaded == rows


def test_candle_csv_rejects_unsorted_timestamps(tmp_path) -> None:
    path = tmp_path / "candles.csv"
    path.write_text(
        "timestamp,open,high,low,close\n"
        "2026-01-01T00:01:00+00:00,100,101,99,100\n"
        "2026-01-01T00:00:00+00:00,100,101,99,100\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="strictly increasing"):
        load_candles_csv(path)
