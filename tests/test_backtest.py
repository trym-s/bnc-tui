import unittest
from decimal import Decimal

from app.backtest.engine import run_backtest
from app.backtest.models import BacktestConfig, Signal, SignalAction
from app.backtest.strategies import (
    BollingerBandReversionStrategy,
    MovingAverageCrossStrategy,
    _atr_at,
    _bollinger_bands,
    _macd_at,
    _moving_average_series,
    _rsi_at,
    _rsi_confirmation_passes,
    _true_range_at,
)
from app.backtest.cli import _build_strategy, _expected_candle_count, _next_open_time_ms, _parse_range_args
from app.models.events import CandleEvent


def candle(index: int, close: str) -> CandleEvent:
    close_value = Decimal(close)
    return ohlc_candle(index, close, close, close, close)


def ohlc_candle(index: int, open_price: str, high: str, low: str, close: str) -> CandleEvent:
    close_value = Decimal(close)
    open_time = index * 60_000
    return CandleEvent(
        symbol="BTCFDUSD",
        interval="1m",
        open_time_ms=open_time,
        close_time_ms=open_time + 59_999,
        open=Decimal(open_price),
        high=Decimal(high),
        low=Decimal(low),
        close=close_value,
        volume=Decimal("1"),
        quote_volume=close_value,
        trade_count=1,
        is_closed=True,
    )


class ScriptedStrategy:
    name = "scripted"

    def on_candle(self, index, candles, position):
        if index == 1:
            return Signal(SignalAction.BUY, "script buy")
        if index == 3:
            return Signal(SignalAction.SELL, "script sell")
        return Signal(SignalAction.HOLD)


class BacktestEngineTest(unittest.TestCase):
    def test_run_backtest_creates_trade_and_pnl(self):
        result = run_backtest(
            [candle(0, "9"), candle(1, "10"), candle(2, "11"), candle(3, "12")],
            ScriptedStrategy(),
            BacktestConfig(
                symbol="BTCFDUSD",
                interval="1m",
                starting_cash=Decimal("1000"),
                order_quote_amount=Decimal("100"),
                fee_rate=Decimal("0"),
            ),
        )

        self.assertEqual(len(result.trades), 1)
        trade = result.trades[0]
        self.assertEqual(trade.entry_price, Decimal("10"))
        self.assertEqual(trade.exit_price, Decimal("12"))
        self.assertEqual(trade.quantity, Decimal("10"))
        self.assertEqual(trade.pnl_quote, Decimal("20"))
        self.assertEqual(result.ending_cash, Decimal("1020"))
        self.assertEqual(result.total_return_percent, Decimal("2.00"))

    def test_run_backtest_closes_open_position_at_end(self):
        result = run_backtest(
            [candle(0, "10"), candle(1, "20")],
            ScriptedStrategy(),
            BacktestConfig(
                symbol="BTCFDUSD",
                interval="1m",
                starting_cash=Decimal("1000"),
                order_quote_amount=Decimal("100"),
                fee_rate=Decimal("0"),
            ),
        )

        self.assertEqual(len(result.trades), 1)
        self.assertEqual(result.trades[0].exit_reason, "end of data")
        self.assertEqual(result.trades[0].pnl_quote, Decimal("0"))


class MovingAverageCrossStrategyTest(unittest.TestCase):
    def test_moving_average_series_supports_sma_ema_wma(self):
        candles = [candle(index, str(index + 1)) for index in range(4)]

        sma = _moving_average_series(candles, 3, "sma")
        ema = _moving_average_series(candles, 3, "ema")
        wma = _moving_average_series(candles, 3, "wma")

        self.assertEqual(sma[2], Decimal("2"))
        self.assertEqual(ema[2], Decimal("2"))
        self.assertEqual(ema[3], Decimal("3.0"))
        self.assertEqual(wma[2], Decimal("14") / Decimal("6"))

    def test_generates_buy_and_sell_crosses(self):
        prices = ["10", "9", "8", "9", "10", "11", "12", "11", "10", "9", "8"]
        candles = [candle(index, price) for index, price in enumerate(prices)]
        strategy = MovingAverageCrossStrategy(short_window=2, long_window=3)
        position = None
        signals = []

        for index in range(len(candles)):
            signal = strategy.on_candle(index, candles, position)
            signals.append(signal.action)
            if signal.action is SignalAction.BUY:
                position = object()
            elif signal.action is SignalAction.SELL:
                position = None

        self.assertIn(SignalAction.BUY, signals)
        self.assertIn(SignalAction.SELL, signals)

    def test_ema_cross_strategy_is_configurable(self):
        strategy = MovingAverageCrossStrategy(short_window=20, long_window=100, average_type="ema")

        self.assertEqual(strategy.name, "EMA 20/100")


class BollingerBandReversionStrategyTest(unittest.TestCase):
    def test_bollinger_bands_use_decimal_stddev(self):
        candles = [candle(index, price) for index, price in enumerate(["1", "2", "3", "4", "5"])]

        lower, middle, upper = _bollinger_bands(candles, 4, 5, Decimal("2"))

        self.assertEqual(middle, Decimal("3"))
        self.assertLess(lower, middle)
        self.assertGreater(upper, middle)

    def test_generates_buy_after_lower_band_wick_touch_and_bullish_reclaim(self):
        candles = [candle(index, "100") for index in range(30)]
        candles.append(ohlc_candle(30, "100", "101", "80", "100"))
        candles.append(ohlc_candle(31, "82", "101", "81", "101"))
        strategy = BollingerBandReversionStrategy(period=30, std_multiplier=Decimal("1"))

        signal = strategy.on_candle(31, candles, None)

        self.assertIs(signal.action, SignalAction.BUY)

    def test_reclaim_must_be_bullish(self):
        candles = [candle(index, "100") for index in range(30)]
        candles.append(ohlc_candle(30, "100", "101", "80", "100"))
        candles.append(ohlc_candle(31, "102", "102", "81", "101"))
        strategy = BollingerBandReversionStrategy(period=30, std_multiplier=Decimal("1"))

        signal = strategy.on_candle(31, candles, None)

        self.assertIs(signal.action, SignalAction.HOLD)

    def test_sells_when_price_reaches_middle_band(self):
        candles = [candle(index, "100") for index in range(31)]
        strategy = BollingerBandReversionStrategy(period=30, std_multiplier=Decimal("3"))

        signal = strategy.on_candle(30, candles, object())

        self.assertIs(signal.action, SignalAction.SELL)

    def test_rsi_calculation_handles_trend_and_flat_windows(self):
        rising = [candle(index, str(index + 1)) for index in range(4)]
        falling = [candle(index, str(4 - index)) for index in range(4)]
        flat = [candle(index, "1") for index in range(4)]

        self.assertEqual(_rsi_at(rising, 3, 3), Decimal("100"))
        self.assertEqual(_rsi_at(falling, 3, 3), Decimal("0"))
        self.assertEqual(_rsi_at(flat, 3, 3), Decimal("50"))

    def test_rsi_turn_up_confirmation(self):
        candles = [candle(index, price) for index, price in enumerate(["50", "60", "50", "40", "45"])]

        self.assertTrue(_rsi_confirmation_passes(candles, 4, 2, "turn-up", Decimal("30")))
        self.assertTrue(_rsi_confirmation_passes(candles, 4, 2, "cross-above", Decimal("30")))

    def test_atr_uses_true_range(self):
        candles = [
            ohlc_candle(0, "10", "12", "9", "11"),
            ohlc_candle(1, "11", "15", "10", "14"),
            ohlc_candle(2, "14", "16", "13", "15"),
        ]

        self.assertEqual(_true_range_at(candles, 1), Decimal("5"))
        self.assertEqual(_atr_at(candles, 2, 3), Decimal("11") / Decimal("3"))

    def test_macd_returns_line_signal_and_histogram(self):
        candles = [candle(index, str(index + 1)) for index in range(60)]

        macd = _macd_at(candles, 59)

        self.assertIsNotNone(macd)
        assert macd is not None
        macd_line, signal_line, histogram = macd
        self.assertGreater(macd_line, Decimal("0"))
        self.assertEqual(histogram, macd_line - signal_line)


class BacktestCliTest(unittest.TestCase):
    def test_next_open_time_aligns_fetch_window_to_interval(self):
        self.assertEqual(_next_open_time_ms(125_000, "1m"), 180_000)
        self.assertEqual(_next_open_time_ms(60_000, "1m"), 120_000)
        self.assertEqual(_next_open_time_ms(15 * 60_000 + 1, "15m"), 30 * 60_000)

    def test_expected_candle_count_uses_end_exclusive_range(self):
        start, end = _parse_range_args("2024-01-01", "2024-01-02", "1h")

        self.assertEqual(_expected_candle_count("1h", start, end), 24)
        self.assertEqual(_expected_candle_count("4h", start, end), 6)
        self.assertEqual(_expected_candle_count("1d", start, end), 1)

    def test_parse_range_aligns_unaligned_start_to_next_open_time(self):
        start, end = _parse_range_args("2024-01-01T00:30:00", "2024-01-01T03:00:00", "1h")

        self.assertEqual(_expected_candle_count("1h", start, end), 2)

    def test_build_strategy_supports_bollinger_config(self):
        class Args:
            strategy = "bollinger"
            bb_period = 30
            bb_std = "3"
            bb_exit = "upper"
            rsi_period = 14
            rsi_confirm = "turn-up"
            rsi_level = "30"

        strategy = _build_strategy(Args())

        self.assertIsInstance(strategy, BollingerBandReversionStrategy)
        self.assertEqual(strategy.period, 30)
        self.assertEqual(strategy.std_multiplier, Decimal("3"))
        self.assertEqual(strategy.exit_band, "upper")
        self.assertEqual(strategy.rsi_confirm, "turn-up")

    def test_build_strategy_supports_ma_cross_config(self):
        class Args:
            strategy = "ma-cross"
            ma_type = "ema"
            short_window = 20
            long_window = 100

        strategy = _build_strategy(Args())

        self.assertIsInstance(strategy, MovingAverageCrossStrategy)
        self.assertEqual(strategy.name, "EMA 20/100")


if __name__ == "__main__":
    unittest.main()
