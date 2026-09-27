from __future__ import annotations

import argparse
import json
from pathlib import Path

import uvicorn

from binance_scanner.backtest import BacktestConfig, BacktestEngine
from binance_scanner.backtest_io import load_candles_csv


def main() -> None:
    parser = argparse.ArgumentParser(description="Binance scanner services")
    subparsers = parser.add_subparsers(dest="command")
    backtest = subparsers.add_parser("backtest", help="run an offline CSV backtest")
    backtest.add_argument("--csv", type=Path, required=True)
    backtest.add_argument("--symbol", required=True)
    backtest.add_argument("--interval", default="1m")
    backtest.add_argument("--initial-balance", type=float, default=10_000.0)
    backtest.add_argument("--warmup-candles", type=int, default=30)
    backtest.add_argument("--fee-fraction", type=float, default=0.001)
    backtest.add_argument("--slippage-fraction", type=float, default=0.0005)
    args = parser.parse_args()
    if args.command == "backtest":
        rows = load_candles_csv(args.csv)
        result = BacktestEngine(
            config=BacktestConfig(
                initial_balance=args.initial_balance,
                warmup_candles=args.warmup_candles,
                fee_fraction=args.fee_fraction,
                slippage_fraction=args.slippage_fraction,
            )
        ).run(args.symbol.upper(), args.interval, [row.candle for row in rows])
        print(
            json.dumps(
                {
                    "initial_balance": result.initial_balance,
                    "ending_balance": result.ending_balance,
                    "total_return_fraction": result.total_return_fraction,
                    "win_rate": result.win_rate,
                    "trade_count": len(result.trades),
                },
                indent=2,
            )
        )
        return
    uvicorn.run("binance_scanner.api:app", host="0.0.0.0", port=8000, reload=False)


if __name__ == "__main__":
    main()
