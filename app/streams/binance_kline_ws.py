"""Binance kline WebSocket stream for the 15m OHLC chart."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from decimal import Decimal

import websockets
from websockets.exceptions import ConnectionClosed

from app.models.events import CandleEvent, ConnectionEvent, ConnectionState
from app.streams.event_bus import EventBus

logger = logging.getLogger(__name__)

_BINANCE_MAX_CONN_SECONDS = 23 * 60 * 60
_BACKOFF_BASE = 1.0
_BACKOFF_MAX = 30.0


def _parse(raw: str) -> CandleEvent:
    data = json.loads(raw)
    kline = data["k"]
    return CandleEvent(
        symbol=kline["s"],
        interval=kline["i"],
        open_time_ms=int(kline["t"]),
        close_time_ms=int(kline["T"]),
        open=Decimal(kline["o"]),
        high=Decimal(kline["h"]),
        low=Decimal(kline["l"]),
        close=Decimal(kline["c"]),
        volume=Decimal(kline["v"]),
        quote_volume=Decimal(kline["q"]),
        trade_count=int(kline["n"]),
        is_closed=bool(kline["x"]),
    )


class CandleStreamManager:
    """Publishes live kline updates into an EventBus."""

    def __init__(
        self,
        symbol: str,
        candle_bus: EventBus[CandleEvent],
        conn_bus: EventBus[ConnectionEvent] | None = None,
        interval: str = "15m",
    ) -> None:
        self.symbol = symbol.lower()
        self.interval = interval
        self.state = ConnectionState.DISCONNECTED
        self._candle_bus = candle_bus
        self._conn_bus = conn_bus
        self._stream_url = (
            f"wss://stream.binance.com:9443/ws/{self.symbol}@kline_{self.interval}"
        )

    async def run(self) -> None:
        backoff = _BACKOFF_BASE

        while True:
            await self._set_state(ConnectionState.CONNECTING)
            logger.info("Kline stream bağlanıyor: %s", self._stream_url)

            try:
                async with self._open_connection() as ws:
                    await self._set_state(ConnectionState.CONNECTED, self.symbol.upper())
                    backoff = _BACKOFF_BASE

                    async for raw in ws:
                        await self._candle_bus.publish(_parse(raw))

            except ConnectionClosed as exc:
                logger.warning("Kline bağlantısı kapandı: code=%s reason=%s", exc.code, exc.reason)

            except OSError as exc:
                logger.warning("Kline ağ hatası: %s", exc)

            except asyncio.CancelledError:
                await self._set_state(ConnectionState.DISCONNECTED)
                raise

            except Exception as exc:
                logger.error("Kline beklenmedik hata: %s", exc, exc_info=True)

            await self._set_state(ConnectionState.RECONNECTING, f"{backoff:.0f}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX)

    async def _set_state(self, state: ConnectionState, detail: str = "") -> None:
        self.state = state
        if self._conn_bus is not None:
            await self._conn_bus.publish(ConnectionEvent(state=state, detail=detail))

    @asynccontextmanager
    async def _open_connection(self):
        async with websockets.connect(self._stream_url) as ws:
            try:
                async with asyncio.timeout(_BINANCE_MAX_CONN_SECONDS):
                    yield ws
            except TimeoutError:
                logger.info("Kline 23 saatlik limit doldu, yeniden bağlanıyor...")
