"""CLI for running a simple backtest on cached candle data."""

from __future__ import annotations

import argparse
import asyncio
import time
from datetime import datetime, timezone
from decimal import Decimal

from rich.console import Console
from rich.table import Table

from app.backtest.engine import run_backtest
from app.backtest.models import BacktestConfig, BacktestResult, BacktestTrade
from app.backtest.strategies import BollingerBandReversionStrategy, MovingAverageCrossStrategy, Strategy
from app.clients.binance_rest import BinanceRestClient
from app.storage.market_data import (
    SUPPORTED_CANDLE_INTERVALS,
    CandleCache,
    MarketDataStore,
    candle_interval_ms,
)

DEFAULT_SYMBOL = "BTCFDUSD"


def main() -> None:
    asyncio.run(async_main())


async def async_main() -> None:
    args = parse_args()
    console = Console()
    store = MarketDataStore()

    if args.start and args.end:
        start_time_ms, end_time_ms = _parse_range_args(args.start, args.end, args.interval)
        expected_count = _expected_candle_count(args.interval, start_time_ms, end_time_ms)
        if args.fetch:
            await _fetch_candle_range(
                console,
                store,
                args.symbol,
                args.interval,
                start_time_ms,
                end_time_ms,
                expected_count,
            )
        candles = store.get_candles(args.symbol, args.interval, start_time_ms, end_time_ms)
        _warn_if_incomplete_range(
            console,
            args.symbol,
            args.interval,
            start_time_ms,
            end_time_ms,
            len(candles),
            expected_count,
            args.fetch,
        )
    else:
        if args.fetch:
            await _fetch_latest_candles(console, store, args.symbol, args.interval, args.limit)
        candles = store.get_latest_candles(args.symbol, args.interval, args.limit)
        if len(candles) < args.limit:
            console.print(
                f"[yellow]Only {len(candles)} cached candles available for "
                f"{args.symbol.upper()} {args.interval}; requested {args.limit}.[/] "
                "Use --fetch to fill the range."
            )

    if not candles:
        console.print(
            f"No cached candles for {args.symbol.upper()} {args.interval}. "
            "Run with --fetch to download the needed range."
        )
        return

    strategy = _build_strategy(args)
    result = run_backtest(
        candles,
        strategy,
        BacktestConfig(
            symbol=args.symbol,
            interval=args.interval,
            starting_cash=_decimal_arg(args.cash),
            order_quote_amount=_decimal_arg(args.order_size),
            fee_rate=_decimal_arg(args.fee_rate),
        ),
    )

    _render_result(console, result, args.quote_asset)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a rule-based backtest on cached candles.")
    parser.add_argument("--symbol", default=DEFAULT_SYMBOL)
    parser.add_argument("--interval", default="1m", choices=SUPPORTED_CANDLE_INTERVALS)
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--cash", default="1000")
    parser.add_argument("--order-size", default="100")
    parser.add_argument("--fee-rate", default="0.001")
    parser.add_argument("--strategy", default="ma-cross", choices=("ma-cross", "sma", "bollinger"))
    parser.add_argument("--ma-type", default="sma", choices=("sma", "ema", "wma"))
    parser.add_argument("--short-window", type=int, default=20)
    parser.add_argument("--long-window", type=int, default=100)
    parser.add_argument("--bb-period", type=int, default=30)
    parser.add_argument("--bb-std", default="3")
    parser.add_argument("--bb-exit", default="middle", choices=("middle", "upper"))
    parser.add_argument("--rsi-period", type=int, default=14)
    parser.add_argument(
        "--rsi-confirm",
        default="off",
        choices=("off", "rising", "turn-up", "cross-above", "below"),
    )
    parser.add_argument("--rsi-level", default="30")
    parser.add_argument("--quote-asset", default="FDUSD")
    parser.add_argument("--fetch", action="store_true", help="Download missing candles before running.")
    parser.add_argument("--start", help="UTC start date/datetime, inclusive. Example: 2024-01-01")
    parser.add_argument("--end", help="UTC end date/datetime, exclusive. Example: 2024-02-01")
    args = parser.parse_args()
    if (args.start is None) != (args.end is None):
        parser.error("--start and --end must be used together")
    if args.limit < 1:
        parser.error("--limit must be >= 1")
    if args.strategy in {"ma-cross", "sma"} and args.long_window <= args.short_window:
        parser.error("--long-window must be greater than --short-window for ma-cross strategy")
    if args.strategy == "bollinger" and args.bb_period < 2:
        parser.error("--bb-period must be >= 2")
    if args.strategy == "bollinger" and args.rsi_period < 2:
        parser.error("--rsi-period must be >= 2")
    rsi_level = _decimal_arg(args.rsi_level)
    if not Decimal("0") <= rsi_level <= Decimal("100"):
        parser.error("--rsi-level must be between 0 and 100")
    return args


def _build_strategy(args: argparse.Namespace) -> Strategy:
    if args.strategy == "bollinger":
        return BollingerBandReversionStrategy(
            period=args.bb_period,
            std_multiplier=_decimal_arg(args.bb_std),
            exit_band=args.bb_exit,
            rsi_period=getattr(args, "rsi_period", 14),
            rsi_confirm=getattr(args, "rsi_confirm", "off"),
            rsi_level=_decimal_arg(getattr(args, "rsi_level", "30")),
        )
    average_type = "sma" if args.strategy == "sma" else args.ma_type
    return MovingAverageCrossStrategy(
        args.short_window,
        args.long_window,
        average_type=average_type,
    )


async def _fetch_latest_candles(
    console: Console,
    store: MarketDataStore,
    symbol: str,
    interval: str,
    limit: int,
) -> None:
    step_ms = candle_interval_ms(interval)
    end_time_ms = _next_open_time_ms(int(time.time() * 1000), interval)
    start_time_ms = end_time_ms - (max(1, limit) * step_ms)
    await _fetch_candle_range(
        console,
        store,
        symbol,
        interval,
        start_time_ms,
        end_time_ms,
        max(1, limit),
    )


async def _fetch_candle_range(
    console: Console,
    store: MarketDataStore,
    symbol: str,
    interval: str,
    start_time_ms: int,
    end_time_ms: int,
    expected_count: int,
) -> None:
    before = len(store.get_candles(symbol, interval, start_time_ms, end_time_ms))
    console.print(
        f"Fetching missing {symbol.upper()} {interval} candles "
        f"for {_format_time(start_time_ms)} -> {_format_time(end_time_ms)} "
        f"({expected_count} expected)..."
    )
    cache = CandleCache(store, BinanceRestClient())
    candles = await cache.get_or_fetch(symbol, interval, start_time_ms, end_time_ms)
    added_or_existing = len(candles)
    after = len(store.get_candles(symbol, interval, start_time_ms, end_time_ms))
    console.print(
        f"Cache ready: {after} candles in range "
        f"({max(0, after - before)} newly available, {added_or_existing} loaded)."
    )


def _warn_if_incomplete_range(
    console: Console,
    symbol: str,
    interval: str,
    start_time_ms: int,
    end_time_ms: int,
    actual_count: int,
    expected_count: int,
    did_fetch: bool,
) -> None:
    if actual_count >= expected_count:
        return
    advice = "Binance may not have listed data for the whole range."
    if not did_fetch:
        advice = "Use --fetch to fill missing candles before running."
    console.print(
        f"[yellow]{symbol.upper()} {interval} has {actual_count}/{expected_count} "
        f"candles for {_format_time(start_time_ms)} -> {_format_time(end_time_ms)}.[/] "
        f"{advice}"
    )


def _render_result(console: Console, result: BacktestResult, quote_asset: str) -> None:
    console.print()
    console.print(
        f"[bold]{result.config.symbol.upper()} {result.config.interval}[/]  "
        f"{result.strategy_name}  "
        f"{result.candle_count} candles"
    )

    summary = Table.grid(padding=(0, 2))
    summary.add_column(style="dim")
    summary.add_column(justify="right")
    summary.add_row("Starting cash", f"{_money(result.starting_cash)} {quote_asset}")
    summary.add_row("Ending cash", f"{_money(result.ending_cash)} {quote_asset}")
    summary.add_row("Total PnL", _signed_money(result.total_pnl_quote, quote_asset))
    summary.add_row("Total return", _signed_percent(result.total_return_percent))
    summary.add_row("Trades", str(len(result.trades)))
    summary.add_row("Win rate", f"{_money(result.win_rate_percent)}%")
    summary.add_row("Max drawdown", f"{_money(result.max_drawdown_quote)} {quote_asset} ({_money(result.max_drawdown_percent)}%)")
    console.print(summary)

    if not result.trades:
        console.print("\n[dim]No trades generated by this rule set.[/]")
        return

    table = Table(title="Trades", show_lines=False)
    table.add_column("#", justify="right", style="dim")
    table.add_column("Entry")
    table.add_column("Exit")
    table.add_column("Entry px", justify="right")
    table.add_column("Exit px", justify="right")
    table.add_column("Qty", justify="right")
    table.add_column("PnL", justify="right")
    table.add_column("Bars", justify="right")

    for index, trade in enumerate(result.trades, 1):
        pnl_style = "green" if trade.pnl_quote >= 0 else "red"
        table.add_row(
            str(index),
            _format_time(trade.entry_time_ms),
            _format_time(trade.exit_time_ms),
            _money(trade.entry_price),
            _money(trade.exit_price),
            _quantity(trade.quantity),
            f"[{pnl_style}]{_signed_money(trade.pnl_quote, quote_asset)}[/]",
            str(trade.bars_held),
        )
    console.print()
    console.print(table)


def _decimal_arg(value: str) -> Decimal:
    return Decimal(str(value))


def _parse_range_args(start: str, end: str, interval: str) -> tuple[int, int]:
    start_time_ms = _align_start_time_ms(_parse_datetime_arg(start), interval)
    end_time_ms = _parse_datetime_arg(end)
    if end_time_ms <= start_time_ms:
        raise SystemExit("--end must be after --start")
    return start_time_ms, end_time_ms


def _parse_datetime_arg(value: str) -> int:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise SystemExit(f"Invalid date/datetime: {value}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return int(dt.timestamp() * 1000)


def _align_start_time_ms(timestamp_ms: int, interval: str) -> int:
    step_ms = candle_interval_ms(interval)
    remainder = timestamp_ms % step_ms
    if remainder == 0:
        return timestamp_ms
    return timestamp_ms + (step_ms - remainder)


def _next_open_time_ms(timestamp_ms: int, interval: str) -> int:
    step_ms = candle_interval_ms(interval)
    return timestamp_ms - (timestamp_ms % step_ms) + step_ms


def _expected_candle_count(interval: str, start_time_ms: int, end_time_ms: int) -> int:
    if end_time_ms <= start_time_ms:
        return 0
    step_ms = candle_interval_ms(interval)
    return ((end_time_ms - 1 - start_time_ms) // step_ms) + 1


def _format_time(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _money(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01')):,.2f}"


def _signed_money(value: Decimal, quote_asset: str) -> str:
    sign = "+" if value > 0 else ""
    return f"{sign}{_money(value)} {quote_asset}"


def _signed_percent(value: Decimal) -> str:
    sign = "+" if value > 0 else ""
    return f"{sign}{_money(value)}%"


def _quantity(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.00000001')):f}".rstrip("0").rstrip(".")


if __name__ == "__main__":
    main()
