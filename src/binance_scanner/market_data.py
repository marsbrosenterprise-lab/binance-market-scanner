from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from binance_scanner.binance.rest import BinanceRestClient
from binance_scanner.binance.ws import BinanceMarketStream
from binance_scanner.models import Candle, Symbol


@dataclass(frozen=True, slots=True)
class CandleRecord:
    symbol: str
    interval: str
    open_time: datetime
    close_time: datetime
    open_price: Decimal
    high_price: Decimal
    low_price: Decimal
    close_price: Decimal
    volume: Decimal
    quote_volume: Decimal
    trade_count: int
    is_closed: bool


def _utc_from_ms(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1000, tz=UTC)


def parse_rest_kline(symbol: str, interval: str, payload: list[Any]) -> CandleRecord:
    if len(payload) < 9:
        raise ValueError("Binance REST kline payload is incomplete")
    return CandleRecord(
        symbol=symbol,
        interval=interval,
        open_time=_utc_from_ms(int(payload[0])),
        close_time=_utc_from_ms(int(payload[6])),
        open_price=Decimal(str(payload[1])),
        high_price=Decimal(str(payload[2])),
        low_price=Decimal(str(payload[3])),
        close_price=Decimal(str(payload[4])),
        volume=Decimal(str(payload[5])),
        quote_volume=Decimal(str(payload[7])),
        trade_count=int(payload[8]),
        is_closed=True,
    )


def parse_ws_kline(payload: Mapping[str, Any]) -> CandleRecord:
    data = payload.get("data", payload)
    kline = data.get("k") if isinstance(data, dict) else None
    if not isinstance(kline, dict):
        raise ValueError("Binance WebSocket payload does not contain a kline")
    symbol = kline.get("s")
    interval = kline.get("i")
    if not isinstance(symbol, str) or not isinstance(interval, str):
        raise ValueError("Binance WebSocket kline is missing symbol or interval")
    return CandleRecord(
        symbol=symbol,
        interval=interval,
        open_time=_utc_from_ms(int(kline["t"])),
        close_time=_utc_from_ms(int(kline["T"])),
        open_price=Decimal(str(kline["o"])),
        high_price=Decimal(str(kline["h"])),
        low_price=Decimal(str(kline["l"])),
        close_price=Decimal(str(kline["c"])),
        volume=Decimal(str(kline["v"])),
        quote_volume=Decimal(str(kline["q"])),
        trade_count=int(kline["n"]),
        is_closed=bool(kline["x"]),
    )


async def upsert_candle(session: AsyncSession, candle: CandleRecord) -> None:
    statement = insert(Candle).values(
        symbol=candle.symbol,
        interval=candle.interval,
        open_time=candle.open_time,
        close_time=candle.close_time,
        open_price=candle.open_price,
        high_price=candle.high_price,
        low_price=candle.low_price,
        close_price=candle.close_price,
        volume=candle.volume,
        quote_volume=candle.quote_volume,
        trade_count=candle.trade_count,
        is_closed=candle.is_closed,
    )
    statement = statement.on_conflict_do_update(
        index_elements=["symbol", "interval", "open_time"],
        set_={
            "close_time": statement.excluded.close_time,
            "open_price": statement.excluded.open_price,
            "high_price": statement.excluded.high_price,
            "low_price": statement.excluded.low_price,
            "close_price": statement.excluded.close_price,
            "volume": statement.excluded.volume,
            "quote_volume": statement.excluded.quote_volume,
            "trade_count": statement.excluded.trade_count,
            "is_closed": statement.excluded.is_closed,
        },
    )
    await session.execute(statement)


async def backfill_candles(
    client: BinanceRestClient,
    session: AsyncSession,
    symbol: str,
    interval: str = "1m",
    limit: int = 500,
) -> int:
    rows = await client.get_klines(symbol, interval, limit)
    for row in rows:
        await upsert_candle(session, parse_rest_kline(symbol, interval, row))
    await session.commit()
    return len(rows)


async def consume_candle_stream(stream: BinanceMarketStream, session_factory: Any) -> None:
    async for payload in stream.events():
        try:
            candle = parse_ws_kline(payload)
        except (KeyError, TypeError, ValueError):
            continue
        async with session_factory() as session:
            await upsert_candle(session, candle)
            await session.commit()


def parse_exchange_symbols(payload: Mapping[str, Any], quote_asset: str = "USDT") -> list[Symbol]:
    result: list[Symbol] = []
    for item in payload.get("symbols", []):
        if not isinstance(item, dict) or item.get("quoteAsset") != quote_asset:
            continue
        symbol = item.get("symbol")
        if not isinstance(symbol, str):
            continue
        result.append(
            Symbol(
                symbol=symbol,
                base_asset=str(item.get("baseAsset", "")),
                quote_asset=str(item.get("quoteAsset", "")),
                status=str(item.get("status", "")),
                is_spot_trading_allowed=bool(item.get("isSpotTradingAllowed", False)),
                filters_json=json.dumps(item.get("filters", []), separators=(",", ":")),
                exchange_update_time=datetime.now(UTC),
            )
        )
    return result


async def sync_symbols(
    client: BinanceRestClient, session: AsyncSession, quote_asset: str = "USDT"
) -> int:
    payload = await client.get_exchange_info()
    symbols = parse_exchange_symbols(payload, quote_asset)
    for symbol in symbols:
        await session.merge(symbol)
    await session.commit()
    return len(symbols)
