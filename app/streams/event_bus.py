"""
Adım 3: EventBus — Pub/Sub Pattern

Producer (StreamManager) mesaj publish eder.
Consumer'lar (TUI, AlertChecker, vb.) subscribe olur ve mesaj bekler.
İkisi birbirini tanımaz.

Neden asyncio.Queue?
  - Thread-safe değil ama asyncio single-threaded zaten
  - await queue.get() event loop'u bloklamaz, diğer coroutine'ler çalışır
  - Alternatif: asyncio.Event (sadece "oldu/olmadı", veri taşımaz)
  - Alternatif: callback listesi (senkron, await desteklemez)
  - Alternatif: Redis/RabbitMQ (overkill, network bağımlısı)
"""

import asyncio
import logging
from typing import TypeVar

logger = logging.getLogger(__name__)

# Generic tip: EventBus[PriceEvent] veya EventBus[ConnectionEvent] diyebiliriz
T = TypeVar("T")


class EventBus[T]:
    """
    Basit, asyncio tabanlı pub/sub bus.

    Her subscriber kendi Queue'sunu alır.
    Böylece yavaş bir subscriber diğerini bloklamaz.

    Kullanım (publisher tarafı):
        bus = EventBus()
        await bus.publish(PriceEvent(...))

    Kullanım (subscriber tarafı):
        async with bus.subscribe() as queue:
            async for event in queue:   # ← sonsuz döngü
                print(event)
    """

    def __init__(self, maxsize: int = 64) -> None:
        # maxsize: queue dolunca publish() en eski mesajı atar (drop)
        # Neden 64? TUI'nun gecikmesi genelde < 1 frame, 64 yeterli tampon.
        self._maxsize = maxsize
        self._subscribers: list[asyncio.Queue[T]] = []

    async def publish(self, event: T) -> None:
        """
        Tüm subscriber'lara event gönder.

        Queue doluysa (subscriber çok yavaş) en eski mesajı at, yenisini koy.
        Neden atmak? Fiyat verisi için eski fiyat, yeni fiyattan daha değersiz.
        Geçmişi biriktirmek anlamsız.
        """
        for queue in self._subscribers:
            if queue.full():
                try:
                    queue.get_nowait()  # en eskiyi at
                except asyncio.QueueEmpty:
                    pass
            await queue.put(event)

    def subscribe(self) -> "Subscription[T]":
        """
        Yeni bir subscriber kaydı oluşturur.
        `async with bus.subscribe() as queue:` şeklinde kullanılır.
        """
        queue: asyncio.Queue[T] = asyncio.Queue(maxsize=self._maxsize)
        self._subscribers.append(queue)
        return Subscription(queue, self._subscribers)


class Subscription[T]:
    """
    Bir subscriber'ın queue'sunu temsil eder.
    Context manager olarak kullanılır — çıkınca otomatik unsubscribe.

    Neden context manager?
        async with bus.subscribe() as queue:
            ...
        # bloktan çıkınca queue otomatik silinir, memory leak olmaz
    """

    def __init__(
        self,
        queue: asyncio.Queue[T],
        registry: list[asyncio.Queue[T]],
    ) -> None:
        self._queue = queue
        self._registry = registry

    async def __aenter__(self) -> asyncio.Queue[T]:
        return self._queue

    async def __aexit__(self, *_) -> None:
        try:
            self._registry.remove(self._queue)
        except ValueError:
            pass
        logger.debug("Subscriber kaldırıldı")
