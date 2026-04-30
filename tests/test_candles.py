import unittest
from decimal import Decimal

from app.models.events import CandleEvent
from app.tui.app import _build_candle_chart, _format_candle_countdown, _merge_candle_window


def candle(open_time: int, close: str, volume: str = "1", close_time: int | None = None) -> CandleEvent:
    close_value = Decimal(close)
    return CandleEvent(
        symbol="BTCUSDT",
        interval="1m",
        open_time_ms=open_time,
        close_time_ms=close_time if close_time is not None else open_time + 59999,
        open=close_value - Decimal("10"),
        high=close_value + Decimal("20"),
        low=close_value - Decimal("20"),
        close=close_value,
        volume=Decimal(volume),
        quote_volume=Decimal("1000"),
        trade_count=10,
        is_closed=True,
    )


class CandleWindowTest(unittest.TestCase):
    def test_merge_replaces_existing_open_time(self):
        original = candle(1000, "70000")
        update = candle(1000, "70100")

        merged = _merge_candle_window([original], update)

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].close, Decimal("70100"))

    def test_merge_sorts_and_trims(self):
        candles = [candle(i, str(70000 + i)) for i in range(5)]
        merged = _merge_candle_window(candles, candle(10, "70100"), limit=3)

        self.assertEqual([c.open_time_ms for c in merged], [3, 4, 10])


class CandleChartTest(unittest.TestCase):
    def test_formats_candle_countdown(self):
        countdown = _format_candle_countdown(14_651_000, 2_000_000)

        self.assertEqual(countdown, "3:30:51")

    def test_formats_expired_candle_countdown_as_syncing(self):
        countdown = _format_candle_countdown(1_000, 2_000)

        self.assertEqual(countdown, "syncing")

    def test_empty_chart_renders_waiting_state(self):
        panel = _build_candle_chart([], width=80)

        self.assertIn("Waiting for 1 minute candles", str(panel.renderable))

    def test_chart_renders_interval_picker_options(self):
        panel = _build_candle_chart([], width=80, interval="4h", is_picking_interval=True)
        text = str(panel.renderable)

        self.assertIn("select", text)
        self.assertIn("1:1 minute", text)
        self.assertIn("2:15 minute", text)
        self.assertIn("4:4 hour", text)
        self.assertIn("5:1 day", text)

    def test_chart_renders_time_until_current_bar_closes(self):
        panel = _build_candle_chart(
            [candle(0, "70000", close_time=14_651_000)],
            width=80,
            interval="4h",
            now_ms=2_000_000,
        )
        text = str(panel.renderable)

        self.assertIn("closes in", text)
        self.assertIn("3:30:51", text)

    def test_chart_renders_candle_blocks_and_last_close(self):
        panel = _build_candle_chart(
            [
                candle(1000, "70000", "1"),
                candle(2000, "70100", "2"),
                candle(3000, "70050", "3"),
            ],
            width=80,
        )
        text = str(panel.renderable)

        self.assertIn("█", text)
        self.assertIn("70,050.00", text)


if __name__ == "__main__":
    unittest.main()
