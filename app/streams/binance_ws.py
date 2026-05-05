"""
StreamManager — WebSocket reconnect loop with EventBus integration.

State machine:
  DISCONNECTED ──run()──▶ CONNECTING ──success──▶ CONNECTED
                               │                      │
                          error/close ◀───────────────┘
                               │
                          RECONNECTING ──backoff──▶ CONNECTING
"""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from decimal import Decimal

import websockets
from websockets.exceptions import ConnectionClosed

from app.models.events import ConnectionEvent, ConnectionState, PriceEvent
from app.streams.event_bus import EventBus

logger = logging.getLogger(__name__)

_BINANCE_MAX_CONN_SECONDS = 23 * 60 * 60
_BACKOFF_BASE = 1.0
_BACKOFF_MAX = 30.0


def _parse(raw: str) -> PriceEvent:
    """Parse a raw miniTicker WebSocket message into a PriceEvent."""
    data = json.loads(raw)
    return PriceEvent(
        symbol=data["s"],
        price=Decimal(data["c"]),
        high_24h=Decimal(data["h"]),
        low_24h=Decimal(data["l"]),
        event_time_ms=data["E"],
    )


class StreamManager:
    """
    Manages the Binance miniTicker WebSocket stream and publishes to EventBus.

    Usage:
        price_bus = EventBus[PriceEvent]()
        conn_bus  = EventBus[ConnectionEvent]()

        manager = StreamManager("BTCUSDT", price_bus, conn_bus)
        asyncio.create_task(manager.run())

        async with price_bus.subscribe() as queue:
            while True:
                event = await queue.get()
                print(event.price)

    run() is an infinite loop that must run concurrently with the TUI.
    Use asyncio.create_task() or Textual's run_worker() to launch it.
    """

    def __init__(
        self,
        symbol: str,
        price_bus: EventBus[PriceEvent],
        conn_bus: EventBus[ConnectionEvent],
        stream_base_url: str = "wss://stream.binance.com:9443/ws",
    ) -> None:
        self.symbol = symbol.lower()
        self.state = ConnectionState.DISCONNECTED
        self._price_bus = price_bus
        self._conn_bus = conn_bus
        self._stream_url = f"{stream_base_url.rstrip('/')}/{self.symbol}@miniTicker"

    async def run(self) -> None:
        """
        Main loop. Start with asyncio.create_task(); cancel the task to stop.

        Reconnects automatically with exponential backoff after any disconnect
        or error. Respects Binance's 23-hour connection limit by cycling the
        connection before it is forcibly closed.
        """
        backoff = _BACKOFF_BASE

        while True:
            await self._set_state(ConnectionState.CONNECTING)
            logger.info("Connecting: %s", self._stream_url)

            try:
                async with self._open_connection() as ws:
                    await self._set_state(
                        ConnectionState.CONNECTED,
                        detail=self.symbol.upper(),
                    )
                    backoff = _BACKOFF_BASE

                    async for raw in ws:
                        await self._price_bus.publish(_parse(raw))

            except ConnectionClosed as exc:
                logger.warning("Connection closed: code=%s reason=%s", exc.code, exc.reason)

            except OSError as exc:
                logger.warning("Network error: %s", exc)

            except asyncio.CancelledError:
                await self._set_state(ConnectionState.DISCONNECTED)
                raise

            except Exception as exc:
                logger.error("Unexpected error: %s", exc, exc_info=True)

            await self._set_state(
                ConnectionState.RECONNECTING,
                detail=f"{backoff:.0f}s",
            )
            logger.info("Reconnecting in %.0f s...", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX)

    async def _set_state(self, state: ConnectionState, detail: str = "") -> None:
        """Update internal state and publish a ConnectionEvent."""
        self.state = state
        await self._conn_bus.publish(ConnectionEvent(state=state, detail=detail))

    @asynccontextmanager
    async def _open_connection(self):
        async with websockets.connect(self._stream_url) as ws:
            try:
                async with asyncio.timeout(_BINANCE_MAX_CONN_SECONDS):
                    yield ws
            except TimeoutError:
                logger.info("23-hour connection limit reached, reconnecting...")
