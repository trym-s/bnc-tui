"""
Backtest execution engine.

Iterates over a sorted candle list, calls the strategy on each bar, and
simulates BUY/SELL orders using the candle's close price. All monetary
arithmetic uses Decimal to avoid floating-point drift.
"""

from __future__ import annotations

from decimal import Decimal

from app.backtest.models import (
    BacktestConfig,
    BacktestResult,
    BacktestTrade,
    EquityPoint,
    Position,
    Signal,
    SignalAction,
)
from app.backtest.strategies import Strategy
from app.models.events import CandleEvent


def run_backtest(
    candles: list[CandleEvent],
    strategy: Strategy,
    config: BacktestConfig,
) -> BacktestResult:
    """
    Simulate a strategy over historical candles and return a full result.

    Only closed candles are used. Candles are sorted by open_time_ms before
    simulation begins. If config.close_open_position_on_end is True, any
    open position at the end of the data is force-closed at the last candle's
    close price.
    """
    ordered = sorted((candle for candle in candles if candle.is_closed), key=lambda c: c.open_time_ms)
    cash = config.starting_cash
    position: Position | None = None
    trades: list[BacktestTrade] = []
    equity_curve: list[EquityPoint] = []

    for index, candle in enumerate(ordered):
        signal = strategy.on_candle(index, ordered, position)
        if signal.action is SignalAction.BUY and position is None:
            position, cash = _open_position(candle, index, cash, config, signal)
        elif signal.action is SignalAction.SELL and position is not None:
            trade, cash = _close_position(candle, index, position, cash, config, signal.reason or "sell")
            trades.append(trade)
            position = None

        equity_curve.append(_mark_equity(candle, cash, position, config.fee_rate))

    if ordered and position is not None and config.close_open_position_on_end:
        last_index = len(ordered) - 1
        trade, cash = _close_position(
            ordered[-1],
            last_index,
            position,
            cash,
            config,
            "end of data",
        )
        trades.append(trade)
        position = None
        equity_curve[-1] = _mark_equity(ordered[-1], cash, None, config.fee_rate)

    ending_cash = cash
    total_pnl = ending_cash - config.starting_cash
    max_drawdown_quote, max_drawdown_percent = _max_drawdown(equity_curve)
    return BacktestResult(
        config=config,
        strategy_name=strategy.name,
        candle_count=len(ordered),
        starting_cash=config.starting_cash,
        ending_cash=ending_cash,
        total_pnl_quote=total_pnl,
        total_return_percent=_safe_percent(total_pnl, config.starting_cash),
        max_drawdown_quote=max_drawdown_quote,
        max_drawdown_percent=max_drawdown_percent,
        trades=tuple(trades),
        equity_curve=tuple(equity_curve),
    )


def _open_position(
    candle: CandleEvent,
    index: int,
    cash: Decimal,
    config: BacktestConfig,
    signal: Signal,
) -> tuple[Position | None, Decimal]:
    """
    Attempt to open a long position at the candle's close price.

    Returns (Position, remaining_cash) on success, or (None, cash) if
    there is insufficient cash or the candle close price is zero.
    Entry fee is deducted from cash immediately so the net spent equals
    order_quote_amount + fee.
    """
    if cash <= 0:
        return None, cash

    quote_amount = min(config.order_quote_amount, cash / (Decimal("1") + config.fee_rate))
    if quote_amount <= 0 or candle.close <= 0:
        return None, cash

    entry_fee = quote_amount * config.fee_rate
    quantity = quote_amount / candle.close
    position = Position(
        entry_time_ms=candle.close_time_ms,
        entry_index=index,
        entry_price=candle.close,
        quantity=quantity,
        entry_quote=quote_amount,
        entry_fee_quote=entry_fee,
        entry_reason=signal.reason or "buy",
    )
    return position, cash - quote_amount - entry_fee


def _close_position(
    candle: CandleEvent,
    index: int,
    position: Position,
    cash: Decimal,
    config: BacktestConfig,
    reason: str,
) -> tuple[BacktestTrade, Decimal]:
    """
    Close an open position at the candle's close price.

    PnL = exit_proceeds - exit_fee - entry_cost - entry_fee.
    Returns (BacktestTrade, updated_cash).
    """
    exit_quote = position.quantity * candle.close
    exit_fee = exit_quote * config.fee_rate
    total_fee = position.entry_fee_quote + exit_fee
    pnl = exit_quote - exit_fee - position.entry_quote - position.entry_fee_quote
    trade = BacktestTrade(
        symbol=config.symbol.upper(),
        interval=config.interval,
        entry_time_ms=position.entry_time_ms,
        exit_time_ms=candle.close_time_ms,
        entry_index=position.entry_index,
        exit_index=index,
        entry_price=position.entry_price,
        exit_price=candle.close,
        quantity=position.quantity,
        entry_quote=position.entry_quote,
        exit_quote=exit_quote,
        fee_quote=total_fee,
        pnl_quote=pnl,
        pnl_percent=_safe_percent(pnl, position.entry_quote + position.entry_fee_quote),
        bars_held=index - position.entry_index,
        entry_reason=position.entry_reason,
        exit_reason=reason,
    )
    return trade, cash + exit_quote - exit_fee


def _mark_equity(
    candle: CandleEvent,
    cash: Decimal,
    position: Position | None,
    fee_rate: Decimal,
) -> EquityPoint:
    """Compute current equity as cash + mark-to-market position value (net of exit fee)."""
    position_value = Decimal("0")
    if position is not None:
        gross_value = position.quantity * candle.close
        position_value = gross_value - (gross_value * fee_rate)
    return EquityPoint(
        time_ms=candle.close_time_ms,
        equity=cash + position_value,
        cash=cash,
        position_value=position_value,
    )


def _max_drawdown(equity_curve: list[EquityPoint]) -> tuple[Decimal, Decimal]:
    """Return (max_drawdown_quote, max_drawdown_percent) over the equity curve."""
    if not equity_curve:
        return Decimal("0"), Decimal("0")

    peak = equity_curve[0].equity
    max_drawdown = Decimal("0")
    max_drawdown_percent = Decimal("0")
    for point in equity_curve:
        if point.equity > peak:
            peak = point.equity
        drawdown = peak - point.equity
        if drawdown > max_drawdown:
            max_drawdown = drawdown
            max_drawdown_percent = _safe_percent(drawdown, peak)
    return max_drawdown, max_drawdown_percent


def _safe_percent(value: Decimal, basis: Decimal) -> Decimal:
    if basis == 0:
        return Decimal("0")
    return (value / basis) * Decimal("100")
