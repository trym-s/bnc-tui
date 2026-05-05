import asyncio
import contextlib
import unittest
from decimal import Decimal

from app.clients.binance_futures import _position_summary
from app.clients.binance_rest import _calculate_summary, _parse_kline_row
from app.models.events import ConnectionEvent, ConnectionState
from app.streams.binance_kline_ws import CandleStreamManager, _parse as parse_kline
from app.streams.binance_ws import StreamManager, _parse
from app.streams.event_bus import EventBus


class StreamParsingTest(unittest.TestCase):
    def test_trade_summary_includes_quote_commission_in_break_even(self):
        summary = _calculate_summary(
            "BTCFDUSD",
            [
                {
                    "id": 1,
                    "time": 1710000000000,
                    "isBuyer": True,
                    "qty": "1",
                    "quoteQty": "100",
                    "price": "100",
                    "commission": "1",
                    "commissionAsset": "FDUSD",
                }
            ],
        )

        self.assertEqual(summary["remaining_qty"], Decimal("1"))
        self.assertEqual(summary["remaining_cost_quote"], Decimal("101"))
        self.assertEqual(summary["break_even_price"], Decimal("101"))
        self.assertEqual(summary["exact_commission_quote"], Decimal("1"))

    def test_trade_summary_includes_base_commission_in_break_even(self):
        summary = _calculate_summary(
            "BTCFDUSD",
            [
                {
                    "id": 1,
                    "time": 1710000000000,
                    "isBuyer": True,
                    "qty": "1",
                    "quoteQty": "100",
                    "price": "100",
                    "commission": "0.01",
                    "commissionAsset": "BTC",
                }
            ],
        )

        self.assertEqual(summary["remaining_qty"], Decimal("0.99"))
        self.assertEqual(summary["remaining_cost_quote"], Decimal("100"))
        self.assertEqual(summary["break_even_price"], Decimal("100") / Decimal("0.99"))

    def test_trade_summary_realized_pnl_is_net_of_quote_commissions(self):
        summary = _calculate_summary(
            "BTCFDUSD",
            [
                {
                    "id": 1,
                    "time": 1710000000000,
                    "isBuyer": True,
                    "qty": "1",
                    "quoteQty": "100",
                    "price": "100",
                    "commission": "1",
                    "commissionAsset": "FDUSD",
                },
                {
                    "id": 2,
                    "time": 1710000060000,
                    "isBuyer": False,
                    "qty": "1",
                    "quoteQty": "110",
                    "price": "110",
                    "commission": "1",
                    "commissionAsset": "FDUSD",
                },
            ],
        )

        self.assertEqual(summary["remaining_qty"], Decimal("0"))
        self.assertEqual(summary["remaining_cost_quote"], Decimal("0"))
        self.assertEqual(summary["realized_pnl_quote"], Decimal("8"))
        self.assertEqual(summary["exact_commission_quote"], Decimal("2"))

    def test_trade_summary_keeps_external_commissions_separate(self):
        summary = _calculate_summary(
            "BTCFDUSD",
            [
                {
                    "id": 1,
                    "time": 1710000000000,
                    "isBuyer": True,
                    "qty": "1",
                    "quoteQty": "100",
                    "price": "100",
                    "commission": "0.02",
                    "commissionAsset": "BNB",
                }
            ],
        )

        self.assertEqual(summary["break_even_price"], Decimal("100"))
        self.assertEqual(summary["external_commissions"], {"BNB": Decimal("0.02")})

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

    def test_stream_managers_can_use_futures_base_url(self):
        price_manager = StreamManager(
            "BTCUSDT",
            EventBus(),
            EventBus(),
            stream_base_url="wss://fstream.binance.com/ws",
        )
        candle_manager = CandleStreamManager(
            "BTCUSDT",
            EventBus(),
            interval="1m",
            stream_base_url="wss://fstream.binance.com/ws",
        )

        self.assertEqual(
            price_manager._stream_url,
            "wss://fstream.binance.com/ws/btcusdt@miniTicker",
        )
        self.assertEqual(
            candle_manager._stream_url,
            "wss://fstream.binance.com/ws/btcusdt@kline_1m",
        )

    def test_futures_position_summary_uses_exchange_break_even(self):
        summary = _position_summary(
            "BTCUSDT",
            {
                "symbol": "BTCUSDT",
                "positionSide": "BOTH",
                "positionAmt": "0.25",
                "entryPrice": "100000",
                "breakEvenPrice": "100012.5",
                "unRealizedProfit": "10.25",
                "marginAsset": "USDT",
            },
            [
                {
                    "id": 1,
                    "isBuyer": True,
                    "commission": "0.5",
                    "commissionAsset": "USDT",
                    "realizedPnl": "0",
                }
            ],
        )

        self.assertEqual(summary["remaining_qty"], Decimal("0.25"))
        self.assertEqual(summary["average_cost"], Decimal("100000"))
        self.assertEqual(summary["break_even_price"], Decimal("100012.5"))
        self.assertEqual(summary["remaining_cost_quote"], Decimal("25003.125"))
        self.assertEqual(summary["unrealized_pnl_quote"], Decimal("10.25"))
        self.assertEqual(summary["exact_commission_quote"], Decimal("0.5"))


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
