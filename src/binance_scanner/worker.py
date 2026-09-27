from __future__ import annotations

import asyncio
import logging

from binance_scanner.binance.rest import BinanceRestClient
from binance_scanner.binance.ws import BinanceMarketStream
from binance_scanner.config import get_settings
from binance_scanner.database import create_session_factory
from binance_scanner.logging import configure_logging
from binance_scanner.market_data import backfill_candles, consume_candle_stream, sync_symbols

logger = logging.getLogger(__name__)


async def run_worker() -> None:
    settings = get_settings()
    symbols = settings.ingest_symbol_list
    if not symbols:
        raise ValueError("INGEST_SYMBOLS must contain at least one symbol")
    if settings.app_mode not in {
        "read_only_testnet",
        "approval_testnet",
        "read_only_demo",
        "approval_demo",
        "paper",
    }:
        raise ValueError("the ingestion worker requires a supported sandbox mode")

    session_factory = create_session_factory(settings.database_url)
    async with BinanceRestClient(
        str(settings.binance_rest_base_url), settings.binance_timeout_seconds
    ) as client:
        async with session_factory() as session:
            await sync_symbols(client, session, settings.market_data_quote_asset)
        for symbol in symbols:
            async with session_factory() as session:
                count = await backfill_candles(
                    client,
                    session,
                    symbol,
                    settings.ingest_interval,
                    settings.ingest_backfill_limit,
                )
            logger.info(
                "historical candle backfill completed",
                extra={"event_type": "candle_backfill", "symbol": symbol, "count": count},
            )

    stream = BinanceMarketStream(
        settings.binance_ws_base_url,
        symbols,
        stream=f"kline_{settings.ingest_interval}",
    )
    logger.info(
        "starting live candle ingestion",
        extra={"event_type": "candle_stream_start", "symbols": symbols},
    )
    await consume_candle_stream(stream, session_factory)


def main() -> None:
    configure_logging(get_settings().log_level)
    try:
        asyncio.run(run_worker())
    except KeyboardInterrupt:
        logger.info("candle ingestion stopped", extra={"event_type": "worker_stop"})


if __name__ == "__main__":
    main()
