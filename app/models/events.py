"""
Event modelleri — EventBus üzerinden taşınan veri tipleri.

Neden ayrı dosya?
  StreamManager ve TUI aynı event tiplerini kullanıyor.
  İkisi de bu dosyaya bağımlı, birbirine değil.
  (Dependency Inversion: ikisi de soyutlamaya bağımlı)
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum, auto


class ConnectionState(Enum):
    DISCONNECTED = auto()
    CONNECTING = auto()
    CONNECTED = auto()
    RECONNECTING = auto()


@dataclass(frozen=True)
class PriceEvent:
    """
    Yeni bir fiyat geldiğinde EventBus'a publish edilen event.

    frozen=True nedir?
      Event oluşturulduktan sonra değiştirilemez.
      Birden fazla subscriber aynı objeyi aldığında
      biri değiştirirse diğeri etkilenmez garantisi.
    """
    symbol: str
    price: Decimal
    high_24h: Decimal
    low_24h: Decimal
    event_time_ms: int


@dataclass(frozen=True)
class CandleEvent:
    """15m OHLC candle carried by REST bootstrap and kline WebSocket updates."""
    symbol: str
    interval: str
    open_time_ms: int
    close_time_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    quote_volume: Decimal
    trade_count: int
    is_closed: bool


@dataclass(frozen=True)
class ConnectionEvent:
    """
    Bağlantı durumu değiştiğinde publish edilen event.
    TUI bunu alınca status bar'ı günceller.
    """
    state: ConnectionState
    detail: str = ""  # opsiyonel: hata mesajı, sembol adı vb.
