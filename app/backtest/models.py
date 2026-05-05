"""Backtest domain models."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class SignalAction(Enum):
    HOLD = "HOLD"
    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True)
class Signal:
    action: SignalAction
    reason: str = ""


@dataclass(frozen=True)
class BacktestConfig:
    symbol: str
    interval: str
    starting_cash: Decimal = Decimal("1000")
    order_quote_amount: Decimal = Decimal("100")
    fee_rate: Decimal = Decimal("0.001")
    close_open_position_on_end: bool = True


@dataclass(frozen=True)
class Position:
    entry_time_ms: int
    entry_index: int
    entry_price: Decimal
    quantity: Decimal
    entry_quote: Decimal
    entry_fee_quote: Decimal
    entry_reason: str


@dataclass(frozen=True)
class BacktestTrade:
    symbol: str
    interval: str
    entry_time_ms: int
    exit_time_ms: int
    entry_index: int
    exit_index: int
    entry_price: Decimal
    exit_price: Decimal
    quantity: Decimal
    entry_quote: Decimal
    exit_quote: Decimal
    fee_quote: Decimal
    pnl_quote: Decimal
    pnl_percent: Decimal
    bars_held: int
    entry_reason: str
    exit_reason: str


@dataclass(frozen=True)
class EquityPoint:
    time_ms: int
    equity: Decimal
    cash: Decimal
    position_value: Decimal


@dataclass(frozen=True)
class BacktestResult:
    config: BacktestConfig
    strategy_name: str
    candle_count: int
    starting_cash: Decimal
    ending_cash: Decimal
    total_pnl_quote: Decimal
    total_return_percent: Decimal
    max_drawdown_quote: Decimal
    max_drawdown_percent: Decimal
    trades: tuple[BacktestTrade, ...]
    equity_curve: tuple[EquityPoint, ...]

    @property
    def winning_trades(self) -> int:
        return sum(1 for trade in self.trades if trade.pnl_quote > 0)

    @property
    def losing_trades(self) -> int:
        return sum(1 for trade in self.trades if trade.pnl_quote < 0)

    @property
    def win_rate_percent(self) -> Decimal:
        if not self.trades:
            return Decimal("0")
        return (Decimal(self.winning_trades) / Decimal(len(self.trades))) * Decimal("100")
