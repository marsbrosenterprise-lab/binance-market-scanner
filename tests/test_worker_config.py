from __future__ import annotations

from binance_scanner.config import Settings


def test_ingest_symbols_are_normalized() -> None:
    settings = Settings(ingest_symbols=" btcusdt, ETHUSDT, ")

    assert settings.ingest_symbol_list == ["BTCUSDT", "ETHUSDT"]
