import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from app.models.events import CandleEvent
from app.storage.market_data import CandleCache, MarketDataStore


def candle(open_time: int, interval: str = "1m", close: str = "70000.12345678") -> CandleEvent:
    close_value = Decimal(close)
    return CandleEvent(
        symbol="BTCFDUSD",
        interval=interval,
        open_time_ms=open_time,
        close_time_ms=open_time + 59_999,
        open=Decimal("69900.12345678"),
        high=Decimal("70100.12345678"),
        low=Decimal("69800.12345678"),
        close=close_value,
        volume=Decimal("1.23456789"),
        quote_volume=Decimal("86420.12345678"),
        trade_count=42,
        is_closed=True,
    )


class MarketDataStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "market.sqlite3"

    def test_round_trips_candles_without_decimal_loss(self):
        store = MarketDataStore(self.path)

        store.upsert_candles([candle(60_000)])
        loaded = store.get_candles("btcfdusd", "1m", 0, 120_000)

        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].close, Decimal("70000.12345678"))
        self.assertEqual(loaded[0].volume, Decimal("1.23456789"))
        self.assertEqual(loaded[0].quote_volume, Decimal("86420.12345678"))

    def test_upsert_replaces_same_open_time(self):
        store = MarketDataStore(self.path)

        store.upsert_candles([candle(60_000, close="70000")])
        store.upsert_candles([candle(60_000, close="70100")])
        loaded = store.get_candles("BTCFDUSD", "1m", 0, 120_000)

        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].close, Decimal("70100"))

    def test_get_latest_candles_returns_oldest_to_newest(self):
        store = MarketDataStore(self.path)

        store.upsert_candles([candle(0), candle(60_000), candle(120_000)])
        loaded = store.get_latest_candles("BTCFDUSD", "1m", limit=2)

        self.assertEqual([c.open_time_ms for c in loaded], [60_000, 120_000])


class CandleCacheTest(unittest.IsolatedAsyncioTestCase):
    async def test_fetches_missing_candles_once_then_reads_from_sqlite(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MarketDataStore(Path(tmp) / "market.sqlite3")

            class FakeRest:
                def __init__(self) -> None:
                    self.calls = []

                async def get_historical_klines(
                    self,
                    symbol: str,
                    interval: str,
                    start_time_ms: int,
                    end_time_ms: int,
                ) -> list[CandleEvent]:
                    self.calls.append((symbol, interval, start_time_ms, end_time_ms))
                    return [
                        candle(open_time, interval=interval)
                        for open_time in range(start_time_ms, end_time_ms, 60_000)
                    ]

            rest = FakeRest()
            cache = CandleCache(store, rest)  # type: ignore[arg-type]

            first = await cache.get_or_fetch("BTCFDUSD", "1m", 0, 180_000)
            second = await cache.get_or_fetch("BTCFDUSD", "1m", 0, 180_000)

            self.assertEqual([c.open_time_ms for c in first], [0, 60_000, 120_000])
            self.assertEqual([c.open_time_ms for c in second], [0, 60_000, 120_000])
            self.assertEqual(rest.calls, [("BTCFDUSD", "1m", 0, 180_000)])


if __name__ == "__main__":
    unittest.main()
