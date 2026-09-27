from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from binance_scanner.backtest import BacktestCandle

CSV_FIELDS = ("timestamp", "open", "high", "low", "close")


@dataclass(frozen=True, slots=True)
class HistoricalCandle:
    timestamp: datetime
    candle: BacktestCandle


def load_candles_csv(path: Path) -> list[HistoricalCandle]:
    rows: list[HistoricalCandle] = []
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != list(CSV_FIELDS):
            raise ValueError(f"CSV header must be exactly: {','.join(CSV_FIELDS)}")
        for line_number, row in enumerate(reader, start=2):
            try:
                timestamp = _parse_timestamp(row["timestamp"])
                candle = BacktestCandle(
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"invalid candle at CSV line {line_number}") from exc
            if rows and timestamp <= rows[-1].timestamp:
                raise ValueError("timestamps must be strictly increasing")
            rows.append(HistoricalCandle(timestamp=timestamp, candle=candle))
    if not rows:
        raise ValueError("CSV contains no candles")
    return rows


def write_candles_csv(path: Path, rows: list[HistoricalCandle]) -> None:
    path.write_text(render_candles_csv(rows), encoding="utf-8", newline="")


def render_candles_csv(rows: list[HistoricalCandle]) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=CSV_FIELDS)
    writer.writeheader()
    previous: datetime | None = None
    for row in rows:
        if previous is not None and row.timestamp <= previous:
            raise ValueError("timestamps must be strictly increasing")
        previous = row.timestamp
        writer.writerow(
            {
                "timestamp": row.timestamp.astimezone(UTC).isoformat(),
                "open": row.candle.open,
                "high": row.candle.high,
                "low": row.candle.low,
                "close": row.candle.close,
            }
        )
    return output.getvalue()


def _parse_timestamp(value: str | None) -> datetime:
    if not value:
        raise ValueError("timestamp is required")
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return timestamp.astimezone(UTC)
