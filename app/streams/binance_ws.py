"""
StreamManager — Reconnect + EventBus entegrasyonu

Adım 2'den değişen:
  - ConnectionState ve event tipleri artık models/events.py'den geliyor
  - StreamManager bir EventBus alıyor, mesajları ona publish ediyor
  - Bağlantı durumu değişince ConnectionEvent de publish ediliyor

State Machine:
  DISCONNECTED ──start()──▶ CONNECTING ──başarı──▶ CONNECTED
                                 │                      │
                            hata/kopuş ◀────────────────┘
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
    Binance WebSocket stream'ini yöneten ve EventBus'a publish eden sınıf.

    Kullanım:
        price_bus = EventBus[PriceEvent]()
        conn_bus  = EventBus[ConnectionEvent]()

        manager = StreamManager("BTCUSDT", price_bus, conn_bus)
        asyncio.create_task(manager.run())   # arka planda çalışır

        # TUI tarafında:
        async with price_bus.subscribe() as queue:
            while True:
                event = await queue.get()
                print(event.price)

    Neden run() ayrı task?
      manager.run() sonsuz döngü. TUI ile aynı anda çalışması gerekiyor.
      asyncio.create_task() ile arka plana alınır, her ikisi paralel yürür.
    """

    def __init__(
        self,
        symbol: str,
        price_bus: EventBus[PriceEvent],
        conn_bus: EventBus[ConnectionEvent],
    ) -> None:
        self.symbol = symbol.lower()
        self.state = ConnectionState.DISCONNECTED
        self._price_bus = price_bus
        self._conn_bus = conn_bus
        self._stream_url = f"wss://stream.binance.com:9443/ws/{self.symbol}@miniTicker"

    async def run(self) -> None:
        """
        Ana döngü. asyncio.create_task() ile başlatılır.
        Durdurmak için task.cancel() kullanılır.
        """
        backoff = _BACKOFF_BASE

        while True:
            await self._set_state(ConnectionState.CONNECTING)
            logger.info("Bağlanıyor: %s", self._stream_url)

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
                logger.warning("Bağlantı kapandı: code=%s reason=%s", exc.code, exc.reason)

            except OSError as exc:
                logger.warning("Ağ hatası: %s", exc)

            except asyncio.CancelledError:
                # Task iptal edildi (uygulama kapanıyor) — temizle ve çık
                await self._set_state(ConnectionState.DISCONNECTED)
                raise

            except Exception as exc:
                logger.error("Beklenmedik hata: %s", exc, exc_info=True)

            await self._set_state(
                ConnectionState.RECONNECTING,
                detail=f"{backoff:.0f}s",
            )
            logger.info("%.0fs sonra yeniden bağlanılacak...", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX)

    async def _set_state(self, state: ConnectionState, detail: str = "") -> None:
        """State'i güncelle ve ConnectionEvent publish et."""
        self.state = state
        await self._conn_bus.publish(ConnectionEvent(state=state, detail=detail))

    @asynccontextmanager
    async def _open_connection(self):
        async with websockets.connect(self._stream_url) as ws:
            try:
                async with asyncio.timeout(_BINANCE_MAX_CONN_SECONDS):
                    yield ws
            except TimeoutError:
                logger.info("23 saatlik limit doldu, yeniden bağlanıyor...")
