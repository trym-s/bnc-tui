"""Rule-based signal strategies for backtests."""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from app.backtest.models import Position, Signal, SignalAction
from app.models.events import CandleEvent

MOVING_AVERAGE_TYPES = ("sma", "ema", "wma")


class Strategy(Protocol):
    """Interface that every backtest strategy must implement."""

    name: str

    def on_candle(
        self,
        index: int,
        candles: list[CandleEvent],
        position: Position | None,
    ) -> Signal:
        """Return a BUY, SELL, or HOLD signal for the candle at *index*."""
        ...


class MovingAverageCrossStrategy:
    """
    Buy when the short moving average crosses above the long moving average;
    sell on the reverse cross.

    Supports SMA, EMA, and WMA via the *average_type* parameter.
    Moving average series are computed once per unique candle list and cached.
    """

    def __init__(
        self,
        short_window: int = 20,
        long_window: int = 100,
        average_type: str = "sma",
    ) -> None:
        if short_window < 1:
            raise ValueError("short_window must be >= 1")
        if long_window <= short_window:
            raise ValueError("long_window must be greater than short_window")
        if average_type not in MOVING_AVERAGE_TYPES:
            raise ValueError(f"unsupported average_type: {average_type}")
        self.short_window = short_window
        self.long_window = long_window
        self.average_type = average_type
        self.name = f"{average_type.upper()} {short_window}/{long_window}"
        self._average_cache: dict[tuple[int, int, str, int], list[Decimal | None]] = {}

    def on_candle(
        self,
        index: int,
        candles: list[CandleEvent],
        position: Position | None,
    ) -> Signal:
        if index < self.long_window:
            return Signal(SignalAction.HOLD)

        prev_short = self._average_at(candles, index - 1, self.short_window)
        prev_long = self._average_at(candles, index - 1, self.long_window)
        current_short = self._average_at(candles, index, self.short_window)
        current_long = self._average_at(candles, index, self.long_window)
        if None in {prev_short, prev_long, current_short, current_long}:
            return Signal(SignalAction.HOLD)

        if position is None and prev_short <= prev_long and current_short > current_long:
            return Signal(SignalAction.BUY, self.name)
        if position is not None and prev_short >= prev_long and current_short < current_long:
            return Signal(SignalAction.SELL, self.name)
        return Signal(SignalAction.HOLD)

    def _average_at(
        self,
        candles: list[CandleEvent],
        index: int,
        window: int,
    ) -> Decimal | None:
        # Use the first and last open_time_ms as a lightweight content key so
        # the cache remains valid even if the list object is recreated.
        boundary = (
            candles[0].open_time_ms if candles else 0,
            candles[-1].open_time_ms if candles else 0,
        )
        key = (boundary, len(candles), self.average_type, window)
        if key not in self._average_cache:
            self._average_cache[key] = _moving_average_series(
                candles,
                window,
                self.average_type,
            )
        return self._average_cache[key][index]


class BollingerBandReversionStrategy:
    """
    Buys after a lower-band wick touch is reclaimed by a positive candle.

    Entry:
      previous low <= previous lower band
      current close > current lower band
      current close > current open

    Exit:
      middle: current close >= current middle band
      upper: current close >= current upper band
    """

    def __init__(
        self,
        period: int = 30,
        std_multiplier: Decimal = Decimal("3"),
        exit_band: str = "middle",
        rsi_period: int = 14,
        rsi_confirm: str = "off",
        rsi_level: Decimal = Decimal("30"),
    ) -> None:
        if period < 2:
            raise ValueError("period must be >= 2")
        if std_multiplier <= 0:
            raise ValueError("std_multiplier must be > 0")
        if exit_band not in {"middle", "upper"}:
            raise ValueError("exit_band must be 'middle' or 'upper'")
        if rsi_period < 2:
            raise ValueError("rsi_period must be >= 2")
        if rsi_confirm not in {"off", "rising", "turn-up", "cross-above", "below"}:
            raise ValueError("unsupported rsi_confirm")
        if not Decimal("0") <= rsi_level <= Decimal("100"):
            raise ValueError("rsi_level must be between 0 and 100")
        self.period = period
        self.std_multiplier = std_multiplier
        self.exit_band = exit_band
        self.rsi_period = rsi_period
        self.rsi_confirm = rsi_confirm
        self.rsi_level = rsi_level
        self.name = f"BB {period}/{std_multiplier:g} exit:{exit_band}"
        if rsi_confirm != "off":
            self.name += f" rsi:{rsi_confirm}/{rsi_period}"

    def on_candle(
        self,
        index: int,
        candles: list[CandleEvent],
        position: Position | None,
    ) -> Signal:
        if index < self.period:
            return Signal(SignalAction.HOLD)

        current = candles[index]
        previous = candles[index - 1]
        prev_lower, _prev_middle, _prev_upper = _bollinger_bands(
            candles,
            index - 1,
            self.period,
            self.std_multiplier,
        )
        lower, middle, upper = _bollinger_bands(
            candles,
            index,
            self.period,
            self.std_multiplier,
        )

        if position is None:
            touched_lower = previous.low <= prev_lower
            bullish_reclaim = current.close > current.open and current.close > lower
            rsi_ok = _rsi_confirmation_passes(
                candles,
                index,
                self.rsi_period,
                self.rsi_confirm,
                self.rsi_level,
            )
            if touched_lower and bullish_reclaim and rsi_ok:
                return Signal(
                    SignalAction.BUY,
                    f"lower wick reclaim BB {self.period}/{self.std_multiplier:g}",
                )
            return Signal(SignalAction.HOLD)

        exit_level = middle if self.exit_band == "middle" else upper
        if current.close >= exit_level:
            return Signal(
                SignalAction.SELL,
                f"{self.exit_band} band exit BB {self.period}/{self.std_multiplier:g}",
            )
        return Signal(SignalAction.HOLD)


def _sma(candles: list[CandleEvent], end_index: int, window: int) -> Decimal:
    """Return the simple moving average of closes over the last *window* candles ending at *end_index*."""
    start = end_index - window + 1
    if start < 0:
        raise ValueError("not enough candles for SMA window")
    values = [candle.close for candle in candles[start : end_index + 1]]
    return sum(values) / Decimal(window)


def _moving_average_series(
    candles: list[CandleEvent],
    window: int,
    average_type: str,
) -> list[Decimal | None]:
    """Build a full moving-average series (one value per candle, None until warm-up is complete)."""
    if window < 1:
        raise ValueError("window must be >= 1")
    if average_type == "sma":
        return _sma_series(candles, window)
    if average_type == "ema":
        return _ema_series(candles, window)
    if average_type == "wma":
        return _wma_series(candles, window)
    raise ValueError(f"unsupported average_type: {average_type}")


def _sma_series(candles: list[CandleEvent], window: int) -> list[Decimal | None]:
    """Compute an O(n) rolling SMA series using a running sum."""
    values: list[Decimal | None] = [None] * len(candles)
    running_sum = Decimal("0")
    for index, candle in enumerate(candles):
        running_sum += candle.close
        if index >= window:
            running_sum -= candles[index - window].close
        if index >= window - 1:
            values[index] = running_sum / Decimal(window)
    return values


def _ema_series(candles: list[CandleEvent], window: int) -> list[Decimal | None]:
    """Compute an EMA series seeded with the SMA of the first *window* candles."""
    values: list[Decimal | None] = [None] * len(candles)
    if len(candles) < window:
        return values

    alpha = Decimal("2") / Decimal(window + 1)
    ema = sum(candle.close for candle in candles[:window]) / Decimal(window)
    values[window - 1] = ema
    for index in range(window, len(candles)):
        ema = (candles[index].close * alpha) + (ema * (Decimal("1") - alpha))
        values[index] = ema
    return values


def _wma_series(candles: list[CandleEvent], window: int) -> list[Decimal | None]:
    """Compute a linearly weighted moving average series (most recent bar has the highest weight)."""
    values: list[Decimal | None] = [None] * len(candles)
    weights = list(range(1, window + 1))
    denominator = Decimal(sum(weights))
    for index in range(window - 1, len(candles)):
        start = index - window + 1
        weighted_sum = sum(
            candles[start + offset].close * Decimal(weight)
            for offset, weight in enumerate(weights)
        )
        values[index] = weighted_sum / denominator
    return values


def _bollinger_bands(
    candles: list[CandleEvent],
    end_index: int,
    period: int,
    std_multiplier: Decimal,
) -> tuple[Decimal, Decimal, Decimal]:
    """
    Return (lower, middle, upper) Bollinger Bands at *end_index*.

    Uses population standard deviation (dividing by *period*) and the SMA as
    the middle band.
    """
    start = end_index - period + 1
    if start < 0:
        raise ValueError("not enough candles for Bollinger period")
    closes = [candle.close for candle in candles[start : end_index + 1]]
    middle = sum(closes) / Decimal(period)
    variance = sum((close - middle) ** 2 for close in closes) / Decimal(period)
    stddev = variance.sqrt()
    distance = stddev * std_multiplier
    return middle - distance, middle, middle + distance


def _rsi_at(candles: list[CandleEvent], index: int, period: int) -> Decimal | None:
    """
    Compute RSI at *index* using a simple average of gains and losses over
    the last *period* bars. Returns None when there is insufficient history.
    """
    if period < 2:
        raise ValueError("period must be >= 2")
    if index < period:
        return None

    gains = Decimal("0")
    losses = Decimal("0")
    for current_index in range(index - period + 1, index + 1):
        change = candles[current_index].close - candles[current_index - 1].close
        if change > 0:
            gains += change
        elif change < 0:
            losses += -change

    average_gain = gains / Decimal(period)
    average_loss = losses / Decimal(period)
    if average_loss == 0 and average_gain == 0:
        return Decimal("50")
    if average_loss == 0:
        return Decimal("100")
    if average_gain == 0:
        return Decimal("0")

    relative_strength = average_gain / average_loss
    return Decimal("100") - (Decimal("100") / (Decimal("1") + relative_strength))


def _rsi_confirmation_passes(
    candles: list[CandleEvent],
    index: int,
    period: int,
    mode: str,
    level: Decimal,
) -> bool:
    """
    Return True when the RSI confirmation condition is satisfied.

    Modes:
        off        — always passes
        rising     — RSI is higher than the previous bar
        cross-above — RSI crossed above *level* this bar
        below      — RSI is at or below *level*
        turn-up    — RSI was falling last bar but rose this bar
    """
    if mode == "off":
        return True

    current = _rsi_at(candles, index, period)
    previous = _rsi_at(candles, index - 1, period)
    if current is None or previous is None:
        return False

    if mode == "rising":
        return current > previous
    if mode == "cross-above":
        return previous <= level < current
    if mode == "below":
        return current <= level
    if mode == "turn-up":
        before_previous = _rsi_at(candles, index - 2, period)
        if before_previous is None:
            return False
        return previous < before_previous and current > previous
    raise ValueError(f"unsupported RSI confirmation mode: {mode}")


def _true_range_at(candles: list[CandleEvent], index: int) -> Decimal:
    """Return the True Range for the candle at *index*."""
    candle = candles[index]
    if index == 0:
        return candle.high - candle.low
    previous_close = candles[index - 1].close
    return max(
        candle.high - candle.low,
        abs(candle.high - previous_close),
        abs(candle.low - previous_close),
    )


def _atr_at(candles: list[CandleEvent], index: int, period: int) -> Decimal | None:
    """Return the Average True Range at *index* (simple average, not Wilder's smoothing)."""
    if period < 1:
        raise ValueError("period must be >= 1")
    if index < period - 1:
        return None

    start = index - period + 1
    true_ranges = [_true_range_at(candles, current_index) for current_index in range(start, index + 1)]
    return sum(true_ranges) / Decimal(period)


def _macd_at(
    candles: list[CandleEvent],
    index: int,
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
) -> tuple[Decimal, Decimal, Decimal] | None:
    """Return (macd_line, signal_line, histogram) at *index*, or None if insufficient history."""
    series = _macd_series(candles, fast_period, slow_period, signal_period)
    return series[index]


def _macd_series(
    candles: list[CandleEvent],
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
) -> list[tuple[Decimal, Decimal, Decimal] | None]:
    """Compute the full MACD series as (macd, signal, histogram) tuples. None until warm-up complete."""
    if fast_period < 1 or slow_period < 1 or signal_period < 1:
        raise ValueError("MACD periods must be >= 1")
    if fast_period >= slow_period:
        raise ValueError("fast_period must be less than slow_period")

    fast_ema = _ema_series(candles, fast_period)
    slow_ema = _ema_series(candles, slow_period)
    macd_line: list[Decimal | None] = [
        None if fast is None or slow is None else fast - slow
        for fast, slow in zip(fast_ema, slow_ema)
    ]
    signal_line = _macd_signal_series(macd_line, signal_period)
    return [
        None if macd is None or signal is None else (macd, signal, macd - signal)
        for macd, signal in zip(macd_line, signal_line)
    ]


def _macd_signal_series(
    macd_line: list[Decimal | None],
    signal_period: int,
) -> list[Decimal | None]:
    """Compute the MACD signal line (EMA of macd_line) seeded from the first *signal_period* non-None values."""
    values: list[Decimal | None] = [None] * len(macd_line)
    alpha = Decimal("2") / Decimal(signal_period + 1)
    seed_values: list[Decimal] = []
    signal: Decimal | None = None

    for index, macd in enumerate(macd_line):
        if macd is None:
            continue
        if signal is None:
            seed_values.append(macd)
            if len(seed_values) == signal_period:
                signal = sum(seed_values) / Decimal(signal_period)
                values[index] = signal
            continue
        signal = (macd * alpha) + (signal * (Decimal("1") - alpha))
        values[index] = signal
    return values
