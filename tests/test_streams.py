import asyncio
import contextlib
import unittest
from decimal import Decimal

from app.clients.binance_rest import _parse_kline_row
from app.models.events import ConnectionEvent, ConnectionState
from app.streams.binance_kline_ws import CandleStreamManager, _parse as parse_kline
from app.streams.binance_ws import StreamManager, _parse
from app.streams.event_bus import EventBus


class StreamParsingTest(unittest.TestCase):
    def test_parse_mini_ticker_payload(self):
        event = _parse(
            '{"e":"24hrMiniTicker","E":1710000000000,"s":"BTCUSDT",'
            '"c":"70123.45","h":"71000.00","l":"69000.50"}'
        )

        self.assertEqual(event.symbol, "BTCUSDT")
        self.assertEqual(event.price, Decimal("70123.45"))
        self.assertEqual(event.high_24h, Decimal("71000.00"))
        self.assertEqual(event.low_24h, Decimal("69000.50"))
        self.assertEqual(event.event_time_ms, 1710000000000)

    def test_stream_manager_builds_symbol_specific_url(self):
        manager = StreamManager("BTCUSDT", EventBus(), EventBus())

        self.assertEqual(manager.symbol, "btcusdt")
        self.assertEqual(
            manager._stream_url,
            "wss://stream.binance.com:9443/ws/btcusdt@miniTicker",
        )

    def test_parse_rest_kline_payload(self):
        candle = _parse_kline_row(
            "BTCUSDT",
            "15m",
            [
                1710000000000,
                "70000.00",
                "70100.00",
                "69900.00",
                "70050.00",
                "12.50000000",
                1710000899999,
                "875625.00",
                1234,
                "6.2",
                "434000",
                "0",
            ],
        )

        self.assertEqual(candle.symbol, "BTCUSDT")
        self.assertEqual(candle.interval, "15m")
        self.assertEqual(candle.open_time_ms, 1710000000000)
        self.assertEqual(candle.close_time_ms, 1710000899999)
        self.assertEqual(candle.open, Decimal("70000.00"))
        self.assertEqual(candle.high, Decimal("70100.00"))
        self.assertEqual(candle.low, Decimal("69900.00"))
        self.assertEqual(candle.close, Decimal("70050.00"))
        self.assertEqual(candle.volume, Decimal("12.50000000"))
        self.assertEqual(candle.quote_volume, Decimal("875625.00"))
        self.assertEqual(candle.trade_count, 1234)
        self.assertTrue(candle.is_closed)

    def test_parse_kline_stream_payload(self):
        candle = parse_kline(
            '{"e":"kline","E":1710000001000,"s":"BTCUSDT","k":{'
            '"t":1710000000000,"T":1710000899999,"s":"BTCUSDT","i":"15m",'
            '"f":100,"L":200,"o":"70000.00","c":"70010.00","h":"70100.00",'
            '"l":"69900.00","v":"10.5","n":101,"x":false,"q":"735000.00",'
            '"V":"5.0","Q":"350000.00","B":"0"}}'
        )

        self.assertEqual(candle.symbol, "BTCUSDT")
        self.assertEqual(candle.interval, "15m")
        self.assertEqual(candle.open_time_ms, 1710000000000)
        self.assertEqual(candle.close, Decimal("70010.00"))
        self.assertFalse(candle.is_closed)

    def test_candle_stream_manager_builds_symbol_specific_url(self):
        manager = CandleStreamManager("BTCUSDT", EventBus(), interval="15m")

        self.assertEqual(manager.symbol, "btcusdt")
        self.assertEqual(manager.interval, "15m")
        self.assertEqual(
            manager._stream_url,
            "wss://stream.binance.com:9443/ws/btcusdt@kline_15m",
        )


class EventBusTest(unittest.IsolatedAsyncioTestCase):
    async def test_broadcasts_to_each_subscriber(self):
        bus = EventBus[str]()

        async with bus.subscribe() as first, bus.subscribe() as second:
            await bus.publish("tick")

            self.assertEqual(await first.get(), "tick")
            self.assertEqual(await second.get(), "tick")

    async def test_drops_oldest_when_subscriber_is_full(self):
        bus = EventBus[str](maxsize=1)

        async with bus.subscribe() as queue:
            await bus.publish("old")
            await bus.publish("new")

            self.assertEqual(await queue.get(), "new")

    async def test_unsubscribes_when_context_exits(self):
        bus = EventBus[str]()

        async with bus.subscribe():
            self.assertEqual(len(bus._subscribers), 1)

        self.assertEqual(len(bus._subscribers), 0)


class StreamManagerTest(unittest.IsolatedAsyncioTestCase):
    async def test_set_state_updates_state_and_publishes_connection_event(self):
        conn_bus = EventBus[ConnectionEvent]()
        manager = StreamManager("ETHUSDT", EventBus(), conn_bus)

        async with conn_bus.subscribe() as queue:
            await manager._set_state(ConnectionState.CONNECTED, detail="ETHUSDT")

            event = await queue.get()

        self.assertIs(manager.state, ConnectionState.CONNECTED)
        self.assertEqual(event, ConnectionEvent(ConnectionState.CONNECTED, detail="ETHUSDT"))

    async def test_run_publishes_connection_states_and_price_until_cancelled(self):
        price_bus = EventBus()
        conn_bus = EventBus()
        manager = StreamManager("BTCUSDT", price_bus, conn_bus)

        class FakeWebSocket:
            def __init__(self) -> None:
                self._sent = False

            def __aiter__(self):
                return self

            async def __anext__(self):
                if not self._sent:
                    self._sent = True
                    return (
                        '{"e":"24hrMiniTicker","E":1710000000000,"s":"BTCUSDT",'
                        '"c":"70123.45","h":"71000.00","l":"69000.50"}'
                    )
                await asyncio.Event().wait()
                raise StopAsyncIteration

        @contextlib.asynccontextmanager
        async def fake_open_connection():
            yield FakeWebSocket()

        manager._open_connection = fake_open_connection

        async with price_bus.subscribe() as prices, conn_bus.subscribe() as states:
            task = asyncio.create_task(manager.run())

            self.assertIs((await states.get()).state, ConnectionState.CONNECTING)
            self.assertIs((await states.get()).state, ConnectionState.CONNECTED)
            self.assertEqual((await prices.get()).price, Decimal("70123.45"))

            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

            self.assertIs((await states.get()).state, ConnectionState.DISCONNECTED)
