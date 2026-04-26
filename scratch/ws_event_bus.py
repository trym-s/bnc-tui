"""
Adım 3 Test: EventBus — iki subscriber aynı anda dinliyor

Çalıştır:
    python scratch/ws_event_bus.py

Ne göreceksin:
  [PRICE]  BTCUSDT → 94123.50
  [LOGGER] event_time=1234567890123 price=94123.50
  [PRICE]  BTCUSDT → 94124.10
  [LOGGER] event_time=1234567890456 price=94124.10

İki farklı subscriber aynı event'i alıyor.
StreamManager hangilerinin olduğunu bilmiyor.
"""

import asyncio
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")
sys.path.insert(0, ".")

from app.models.events import ConnectionEvent, PriceEvent
from app.streams.binance_ws import StreamManager
from app.streams.event_bus import EventBus


async def price_display(price_bus: EventBus[PriceEvent]) -> None:
    """Subscriber 1: fiyatı göster"""
    async with price_bus.subscribe() as queue:
        while True:
            event = await queue.get()
            print(f"[PRICE]  {event.symbol} → {event.price}")


async def price_logger(price_bus: EventBus[PriceEvent]) -> None:
    """Subscriber 2: timestamp ile logla (farklı format, aynı veri)"""
    async with price_bus.subscribe() as queue:
        while True:
            event = await queue.get()
            print(f"[LOGGER] event_time={event.event_time_ms} price={event.price}")


async def conn_monitor(conn_bus: EventBus[ConnectionEvent]) -> None:
    """Subscriber 3: bağlantı durumu değişikliklerini izle"""
    async with conn_bus.subscribe() as queue:
        while True:
            event = await queue.get()
            print(f"[CONN]   state={event.state.name} detail={event.detail!r}")


async def main() -> None:
    price_bus: EventBus[PriceEvent] = EventBus()
    conn_bus: EventBus[ConnectionEvent] = EventBus()

    manager = StreamManager("BTCUSDT", price_bus, conn_bus)

    print("EventBus testi başlıyor. Ctrl+C ile durdur.\n")

    await asyncio.gather(
        manager.run(),        # producer
        price_display(price_bus),   # consumer 1
        price_logger(price_bus),    # consumer 2
        conn_monitor(conn_bus),     # bağlantı durumu consumer
    )


if __name__ == "__main__":
    asyncio.run(main())
