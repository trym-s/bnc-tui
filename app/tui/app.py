"""
Adım 4: TUI Migration — polling → event-driven

Eski akış:
  timer (3s) → refresh_price() → httpx → FastAPI → fiyat + trade summary → dashboard

Yeni akış:
  WebSocket push → EventBus → _listen_prices() → dashboard (anında)
  ayrı timer (30s)           → _refresh_trades_loop() → REST → trade summary

Neden ikiye ayrıldı?
  Fiyat: her saniye değişiyor, WebSocket idealdir.
  Trade summary: geçmiş işlemler, 30 saniyede bir fetch yeterli, REST mantıklı.
  İkisini aynı döngüde yapmak zorunda değiliz.

Neden create_task()?
  Textual'da on_mount() içinde create_task() ile başlatılan task'lar
  TUI'nun event loop'unda çalışır. TUI kapanınca otomatik iptal edilir.
  asyncio.create_task() ile fark: Textual'ın lifecycle'ına bağlı.
"""

from __future__ import annotations

import asyncio
import math
import os
import subprocess
import sys
import time
from decimal import Decimal, InvalidOperation

from rich.columns import Columns
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal
from textual.widgets import Footer, Header, Label, Static

from app.clients.binance_rest import BinanceClientError, BinanceRestClient, _calculate_summary
from app.models.events import CandleEvent, ConnectionEvent, ConnectionState, PriceEvent
from app.storage.groups import GroupStore
from app.storage.market_data import CandleCache, MarketDataStore, candle_interval_ms
from app.streams.binance_kline_ws import CandleStreamManager
from app.streams.binance_ws import StreamManager
from app.streams.event_bus import EventBus

DEFAULT_SYMBOL = "BTCFDUSD"
APP_TITLE = "bnc"
from app.tui.palette import P
from app.tui.screens.trade_list import TradeListScreen

_TRADE_REFRESH_SECONDS = 30.0
_CANDLE_INTERVALS = (
    ("1", "1m", "1 minute"),
    ("2", "15m", "15 minute"),
    ("3", "1h", "1 hour"),
    ("4", "4h", "4 hour"),
    ("5", "1d", "1 day"),
)
_DEFAULT_CANDLE_INTERVAL = "1m"
_CANDLE_LIMIT = 96
_CANDLE_RESYNC_SECONDS = 300.0


class PriceApp(App[None]):
    CSS = """
    Screen {
        background: $background;
        color: $muted;
    }

    Header {
        background: $background;
        color: $dim;
    }

    Footer {
        background: $background;
        color: $dim;
    }

    #panel {
        width: 100%;
        height: 1fr;
        align: center top;
        padding: 0 2 1 2;
        overflow-y: auto;
    }

    #price-card {
        width: 100%;
        height: auto;
        background: $background;
    }

    #pair-row {
        height: 3;
        align: left middle;
        padding: 0 1;
        margin-bottom: 0;
    }

    #symbol {
        text-style: bold;
        color: $foreground;
        width: auto;
        padding-right: 2;
    }

    #status {
        color: $dim;
        width: 1fr;
    }

    #dashboard {
        height: auto;
    }

    #error {
        color: $error;
        height: auto;
        min-height: 1;
        padding: 0 1;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh"),
        ("t", "trade_list", "Trades"),
        ("c", "chart_interval_picker", "Chart"),
    ]

    def get_css_variables(self) -> dict[str, str]:
        """Palette renklerini Textual CSS değişkeni olarak expose eder.
        $background, $surface, $foreground ve özel $muted/$dim/$border-*
        tüm ekranlarda (Screen, Modal) geçerli olur."""
        return {
            **super().get_css_variables(),
            # Textual built-in'leri palette ile ezeriz
            "background": P.bg,
            "surface": P.surface,
            "foreground": P.text,
            "error": P.negative,
            "success": P.positive,
            # Özel değişkenler
            "muted": P.muted,
            "dim": P.dim,
            "border-subtle": P.border,
            "border-accent": P.border_accent,
        }

    def __init__(
        self,
        symbol: str,
        price_bus: EventBus[PriceEvent],
        candle_bus: EventBus[CandleEvent],
        conn_bus: EventBus[ConnectionEvent],
        stream_manager: StreamManager,
        rest_client: BinanceRestClient,
    ) -> None:
        super().__init__()
        self.symbol = symbol.upper()
        self._price_bus = price_bus
        self._candle_bus = candle_bus
        self._conn_bus = conn_bus
        self._stream_manager = stream_manager
        self._rest_client = rest_client
        self._candle_cache = CandleCache(MarketDataStore(), rest_client)

        # Son gelen değerleri saklıyoruz.
        # Fiyat WebSocket'ten, trade summary REST'ten geliyor.
        # İkisi bağımsız güncellendiği için ayrı tutulur.
        self._latest_event: PriceEvent | None = None
        self._latest_summary: dict = {}
        self._latest_candles: list[CandleEvent] = []
        self._latest_balances: list[dict] = []
        self._latest_fdusd_rate: Decimal = Decimal("1")
        self._latest_trades: list[dict] = []
        self._candle_interval = _DEFAULT_CANDLE_INTERVAL
        self._is_picking_candle_interval = False
        self._group_store = GroupStore()

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Container(id="panel"):
            with Container(id="price-card"):
                with Horizontal(id="pair-row"):
                    yield Label(self.symbol, id="symbol")
                    yield Label("connecting...", id="status")
                yield Static(_loading_panel(), id="dashboard")
                yield Static("", id="error")
        yield Footer()

    async def on_mount(self) -> None:
        _set_terminal_title(APP_TITLE)

        # StreamManager arka planda WebSocket'e bağlanıp push etmeye başlar
        self.run_worker(self._stream_manager.run(), exclusive=False)
        self._start_candle_stream()

        # Fiyat event'lerini dinle
        self.run_worker(self._listen_prices(), exclusive=False)
        self.run_worker(self._listen_candles(), exclusive=False)

        # Bağlantı durumunu dinle (status label için)
        self.run_worker(self._listen_connection(), exclusive=False)

        # Trade summary ve bakiyeyi ilk çek, sonra 30s'de bir yenile
        self.run_worker(self._refresh_trades_loop(), exclusive=False)
        self.run_worker(self._refresh_candles_loop(), exclusive=False)
        self.set_interval(1.0, self._render_dashboard)

    async def action_refresh(self) -> None:
        """r tuşu: trade summary + bakiyeyi manuel yenile"""
        await self._fetch_trades()
        await self._fetch_balances()
        await self._fetch_candles()

    def action_trade_list(self) -> None:
        """t tuşu: trade listesi ekranına geç"""
        self.push_screen(TradeListScreen(self.symbol, self._latest_trades, self._group_store))

    def action_chart_interval_picker(self) -> None:
        """c tuşu: chart timeframe seçim modunu aç/kapat."""
        self._is_picking_candle_interval = not self._is_picking_candle_interval
        self._render_dashboard()

    async def on_key(self, event) -> None:
        if not self._is_picking_candle_interval:
            return

        interval_by_key = {key: interval for key, interval, _label in _CANDLE_INTERVALS}
        if event.key == "escape":
            self._is_picking_candle_interval = False
            self._render_dashboard()
            event.stop()
            return

        selected_interval = interval_by_key.get(event.key)
        if selected_interval is None:
            return

        event.stop()
        await self._set_candle_interval(selected_interval)

    def refresh_groups(self) -> None:
        """TradeListScreen grup değiştirince çağrılır, dashboard'ı yeniler."""
        self._render_dashboard()

    # ------------------------------------------------------------------ #
    # Internal workers                                                     #
    # ------------------------------------------------------------------ #

    async def _listen_prices(self) -> None:
        """
        EventBus'tan gelen her PriceEvent'te dashboard'ı güncelle.

        Neden ayrı worker?
          Bu fonksiyon `await queue.get()` ile sürekli bekler.
          Ana thread'de çalışsaydı TUI'yu bloklar, hiçbir şey render edilmezdi.
          Worker olarak çalışınca Textual bunu arka planda yönetir.
        """
        async with self._price_bus.subscribe() as queue:
            while True:
                event = await queue.get()
                self._latest_event = event
                self._render_dashboard()

    async def _listen_connection(self) -> None:
        """Bağlantı durumu değişince status label'ı güncelle."""
        status_map = {
            ConnectionState.CONNECTING: ("connecting...", P.muted),
            ConnectionState.CONNECTED: ("live ●", P.positive),
            ConnectionState.RECONNECTING: ("reconnecting...", "#f97316"),
            ConnectionState.DISCONNECTED: ("disconnected", P.negative),
        }
        async with self._conn_bus.subscribe() as queue:
            while True:
                event = await queue.get()
                text, color = status_map.get(event.state, ("unknown", "#475569"))
                status = self.query_one("#status", Label)
                status.update(f"[{color}]{text}[/]")

    async def _listen_candles(self) -> None:
        """Live kline event'lerini mevcut candle penceresine merge eder."""
        async with self._candle_bus.subscribe() as queue:
            while True:
                candle = await queue.get()
                if candle.interval != self._candle_interval:
                    continue
                self._merge_candle(candle)
                self._render_dashboard()

    async def _refresh_trades_loop(self) -> None:
        """Trade summary + bakiyeyi 30 saniyede bir çeker."""
        while True:
            await asyncio.gather(self._fetch_trades(), self._fetch_balances())
            await asyncio.sleep(_TRADE_REFRESH_SECONDS)

    async def _refresh_candles_loop(self) -> None:
        """Candle geçmişini açılışta ve periyodik olarak REST ile senkronlar."""
        while True:
            await self._fetch_candles()
            await asyncio.sleep(_CANDLE_RESYNC_SECONDS)

    async def _fetch_trades(self) -> None:
        """Trade listesi + summary'yi direkt Binance REST'ten çek (cache'li)."""
        error = self.query_one("#error", Static)
        try:
            self._latest_trades = await self._rest_client.get_trades(self.symbol)
            self._latest_summary = await self._rest_client.get_trade_summary(self.symbol)
            error.update("")
            self._render_dashboard()
        except BinanceClientError as exc:
            error.update(f"[{P.negative}]⚠  Trade fetch failed:[/] {exc}")

    async def _fetch_balances(self) -> None:
        """Tüm hesap bakiyelerini çek (cache'li)."""
        try:
            self._latest_balances, self._latest_fdusd_rate = await self._rest_client.get_balances()
            self._render_dashboard()
        except BinanceClientError:
            pass  # bakiye hatası dashboard'ı engellemez

    async def _fetch_candles(self) -> None:
        """OHLC geçmişini public Binance REST'ten çek."""
        interval = self._candle_interval
        try:
            step_ms = candle_interval_ms(interval)
            end_time_ms = int(time.time() * 1000)
            end_time_ms = end_time_ms - (end_time_ms % step_ms) + step_ms
            start_time_ms = end_time_ms - (_CANDLE_LIMIT * step_ms)
            candles = await self._candle_cache.get_or_fetch(
                self.symbol,
                interval=interval,
                start_time_ms=start_time_ms,
                end_time_ms=end_time_ms,
            )
            if interval != self._candle_interval:
                return
            self._latest_candles = _trim_candles(candles)
            self._render_dashboard()
        except BinanceClientError as exc:
            error = self.query_one("#error", Static)
            error.update(f"[{P.negative}]⚠  Kline fetch failed:[/] {exc}")

    def _merge_candle(self, candle: CandleEvent) -> None:
        self._latest_candles = _merge_candle_window(self._latest_candles, candle)

    async def _set_candle_interval(self, interval: str) -> None:
        if interval == self._candle_interval:
            self._is_picking_candle_interval = False
            self._render_dashboard()
            return

        self._candle_interval = interval
        self._is_picking_candle_interval = False
        self._latest_candles = []
        self._start_candle_stream()
        self._render_dashboard()
        await self._fetch_candles()

    def _start_candle_stream(self) -> None:
        self.run_worker(
            CandleStreamManager(
                self.symbol,
                self._candle_bus,
                interval=self._candle_interval,
            ).run(),
            name="candle-stream",
            group="candles",
            exclusive=True,
            exit_on_error=False,
        )

    def on_resize(self) -> None:
        self._render_dashboard()

    def _render_dashboard(self) -> None:
        """
        En son fiyat ve trade summary ile dashboard'ı yeniden çiz.

        Bu fonksiyon hem fiyat gelince hem trade summary gelince çağrılır.
        Her ikisi de instance variable'da saklandığı için her zaman
        en güncel veriyle render edilir.
        """
        if self._latest_event is None:
            return  # henüz fiyat gelmediyse bekle
        groups = self._group_store.get_groups(self.symbol)
        group_summaries = {
            name: _calculate_summary(self.symbol, [t for t in self._latest_trades if t["id"] in set(ids)])
            for name, ids in groups.items()
        }
        self.query_one("#dashboard", Static).update(
            _build_dashboard(
                self.symbol,
                self._latest_event.price,
                self._latest_summary,
                self._latest_balances,
                self._latest_fdusd_rate,
                group_summaries,
                self._latest_event,
                self._latest_candles,
                self._candle_interval,
                self._is_picking_candle_interval,
                max(24, self.size.width - 10),
            )
        )


# ------------------------------------------------------------------ #
# Formatting helpers                                                   #
# ------------------------------------------------------------------ #

def _to_decimal(value: object) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _format_decimal(value: Decimal) -> str:
    return f"{value.normalize():f}"


def _format_decimal_places(value: Decimal, places: int) -> str:
    quant = Decimal("1").scaleb(-places)
    return f"{value.quantize(quant):,.{places}f}"


def _format_money(value: Decimal) -> str:
    return _format_decimal_places(value, 2)


def _format_quantity(value: Decimal) -> str:
    return _format_decimal_places(value, 8).rstrip("0").rstrip(".")


def _format_signed_money(value: Decimal) -> str:
    formatted = _format_money(value)
    return f"+{formatted}" if value > 0 else formatted


def _format_signed_percent(value: Decimal) -> str:
    formatted = _format_decimal_places(value, 2)
    return f"+{formatted}%" if value > 0 else f"{formatted}%"


def _format_optional_price(value: object, quote_asset: str) -> str:
    if value is None:
        return "—"
    return f"{_format_money(_to_decimal(value))} {quote_asset}"


def _split_symbol(symbol: str) -> tuple[str, str]:
    for quote in ("USDT", "USDC", "FDUSD", "BUSD", "TRY", "BTC", "ETH", "BNB"):
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return symbol[: -len(quote)], quote
    return "BASE", "QUOTE"


def _loading_panel() -> Panel:
    t = Text()
    t.append("  Connecting to Binance stream  ···", style=P.dim)
    return Panel(t, border_style=P.border, padding=(1, 2))


def _set_terminal_title(title: str) -> None:
    sys.stdout.write(f"\033]0;{title}\007")
    sys.stdout.flush()

    if "TMUX" not in os.environ:
        return

    try:
        subprocess.run(
            ["tmux", "rename-window", title],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass


def _pnl_style(value: Decimal) -> str:
    if value > 0:
        return f"bold {P.positive}"
    if value < 0:
        return f"bold {P.negative}"
    return P.neutral


def _pnl_border(value: Decimal) -> str:
    if value > 0:
        return P.positive
    if value < 0:
        return P.negative
    return P.border


def _add_metric(table: Table, label: str, value: str, style: str = "") -> None:
    style = style or P.neutral
    table.add_row(f"[{P.muted}]{label}[/]", f"[{style}]{value}[/]")


def _metric_table() -> Table:
    table = Table.grid(expand=True)
    table.add_column(ratio=1)
    table.add_column(justify="right", ratio=1)
    return table


def _safe_percentage(numerator: Decimal, denominator: Decimal) -> Decimal:
    if denominator == 0:
        return Decimal("0")
    return (numerator / denominator) * Decimal("100")


def _trim_candles(candles: list[CandleEvent], limit: int = _CANDLE_LIMIT) -> list[CandleEvent]:
    return sorted(candles, key=lambda c: c.open_time_ms)[-limit:]


def _merge_candle_window(
    candles: list[CandleEvent],
    candle: CandleEvent,
    limit: int = _CANDLE_LIMIT,
) -> list[CandleEvent]:
    by_open_time = {existing.open_time_ms: existing for existing in candles}
    by_open_time[candle.open_time_ms] = candle
    return _trim_candles(list(by_open_time.values()), limit)


def _price_to_row(value: Decimal, log_low: float, log_span: float, height: int) -> int:
    if log_span == 0:
        return height // 2
    log_val = math.log(float(value))
    scaled = (log_val - log_low) / log_span
    row = height - 1 - int(scaled * (height - 1) + 0.5)
    return max(0, min(height - 1, row))


def _volume_block(volume: Decimal, max_volume: Decimal) -> str:
    blocks = "▁▂▃▄▅▆▇█"
    if max_volume <= 0 or volume <= 0:
        return " "
    idx = int(((volume / max_volume) * Decimal(len(blocks) - 1)).to_integral_value())
    return blocks[max(0, min(len(blocks) - 1, idx))]


def _candle_interval_label(interval: str) -> str:
    for _key, candidate, label in _CANDLE_INTERVALS:
        if candidate == interval:
            return label
    return interval


def _format_candle_countdown(close_time_ms: int, now_ms: int) -> str:
    remaining_ms = close_time_ms - now_ms
    if remaining_ms <= 0:
        return "syncing"

    total_seconds = (remaining_ms + 999) // 1000
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    return f"{hours}:{minutes:02}:{seconds:02}"


def _format_candle_interval_options(
    active_interval: str,
    is_picking: bool,
    close_time_ms: int | None = None,
    now_ms: int | None = None,
) -> Text:
    text = Text()
    prefix = "select " if is_picking else "c "
    text.append(prefix, style=P.dim)
    for index, (key, interval, label) in enumerate(_CANDLE_INTERVALS):
        if index:
            text.append("  ", style=P.dim)
        is_active = interval == active_interval
        style = f"bold {P.text}" if is_active else P.dim
        text.append(f"{key}:{label}", style=style)
    if close_time_ms is not None:
        current_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        text.append("  closes in ", style=P.dim)
        text.append(_format_candle_countdown(close_time_ms, current_ms), style=f"bold {P.neutral}")
    return text


def _build_candle_chart(
    candles: list[CandleEvent],
    width: int,
    interval: str = _DEFAULT_CANDLE_INTERVAL,
    is_picking_interval: bool = False,
    now_ms: int | None = None,
) -> Panel:
    interval_label = _candle_interval_label(interval)
    body = Text()
    close_time_ms = candles[-1].close_time_ms if candles else None
    body.append_text(
        _format_candle_interval_options(
            interval,
            is_picking_interval,
            close_time_ms=close_time_ms,
            now_ms=now_ms,
        )
    )
    body.append("\n\n")

    if not candles:
        body.append(f"Waiting for {interval_label} candles...", style=P.dim)
        return Panel(
            body,
            title=f"  {interval_label}  OHLC",
            border_style=P.border,
            padding=(1, 2),
        )

    SLOT_WIDTH = 3
    chart_height = 14

    visible_count = max(4, min(len(candles), (width - 2) // SLOT_WIDTH))
    visible = candles[-visible_count:]
    plot_width = len(visible) * SLOT_WIDTH

    low = min(c.low for c in visible)
    high = max(c.high for c in visible)
    max_volume = max(c.volume for c in visible)
    log_low = math.log(float(low))
    log_span = math.log(float(high)) - log_low

    # SMA-20: precompute row position for each candle column
    closes = [c.close for c in visible]
    sma_rows: dict[int, int] = {}
    if log_span > 0:
        for i in range(len(visible)):
            window = closes[max(0, i - 19): i + 1]
            sma_val = sum(window) / len(window)
            sma_rows[i] = _price_to_row(sma_val, log_low, log_span, chart_height)

    for row in range(chart_height):
        for i, candle in enumerate(visible):
            high_row = _price_to_row(candle.high, log_low, log_span, chart_height)
            low_row = _price_to_row(candle.low, log_low, log_span, chart_height)
            open_row = _price_to_row(candle.open, log_low, log_span, chart_height)
            close_row = _price_to_row(candle.close, log_low, log_span, chart_height)
            body_top = min(open_row, close_row)
            body_bottom = max(open_row, close_row)
            candle_range = candle.high - candle.low
            is_doji = candle_range > 0 and abs(candle.close - candle.open) / candle_range < Decimal("0.08")

            is_live = (i == len(visible) - 1) and not candle.is_closed
            is_bull = candle.close >= candle.open
            body_style = P.positive if is_bull else P.negative
            wick_style = "#f59e0b" if is_live else body_style

            if body_top <= row <= body_bottom:
                if is_doji:
                    body.append(" ─ ", style=body_style)
                else:
                    char = "▒" if is_live else "█"
                    body.append(f" {char} ", style=body_style)
            elif high_row <= row < body_top or body_bottom < row <= low_row:
                body.append(" │ ", style=wick_style)
            elif sma_rows.get(i) == row:
                body.append(" · ", style=P.muted)
            else:
                body.append(" " * SLOT_WIDTH)

        body.append("\n")

    # Volume section: thin separator + volume bars
    body.append("╌" * plot_width, style=P.border)
    body.append("\n")
    for i, candle in enumerate(visible):
        is_live = (i == len(visible) - 1) and not candle.is_closed
        is_bull = candle.close >= candle.open
        style = "#f59e0b" if is_live else (P.positive if is_bull else P.negative)
        body.append(_volume_block(candle.volume, max_volume) * SLOT_WIDTH, style=style)

    # Footer: last close + period move %
    last = visible[-1]
    move = last.close - visible[0].open
    move_pct = _safe_percentage(move, visible[0].open)
    move_color = P.positive if move >= 0 else P.negative
    body.append(f"  {_format_money(last.close)}  {_format_signed_percent(move_pct)}", style=move_color)
    if not last.is_closed:
        body.append("  forming", style=P.dim)

    title = f"{interval_label}  ·  {len(visible)} bars  ·  {_format_signed_percent(move_pct)}"
    border = P.positive if move >= 0 else P.negative
    return Panel(body, title=title, border_style=border, padding=(1, 2))


def _build_dashboard(
    symbol: str,
    price: Decimal,
    payload: dict,
    balances: list[dict],
    fdusd_rate: Decimal,
    group_summaries: dict[str, dict],
    price_event: PriceEvent | None = None,
    candles: list[CandleEvent] | None = None,
    candle_interval: str = _DEFAULT_CANDLE_INTERVAL,
    is_picking_candle_interval: bool = False,
    width: int = 80,
) -> Group:
    base_asset, quote_asset = _split_symbol(symbol)
    remaining_qty = _to_decimal(payload.get("remaining_qty"))
    remaining_cost = _to_decimal(payload.get("remaining_cost_quote"))
    market_value = remaining_qty * price
    unrealized_pnl = market_value - remaining_cost
    realized_pnl = _to_decimal(payload.get("realized_pnl_quote"))
    average_cost_value = _to_decimal(payload.get("average_cost"))

    rows: list = []

    # ── 1. Price Ticker ──────────────────────────────────────────────── #
    ticker = Table.grid(expand=True, padding=(0, 1))
    ticker.add_column(ratio=3)
    ticker.add_column(ratio=2, justify="right")

    price_text = Text()
    price_text.append(f"{_format_money(price)} {quote_asset}", style=f"bold {P.text}")

    if average_cost_value > 0:
        diff = price - average_cost_value
        pct = _safe_percentage(diff, average_cost_value)
        arrow = "▲" if diff >= 0 else "▼"
        pct_color = P.positive if diff >= 0 else P.negative
        price_text.append(f"   {arrow} {_format_signed_percent(pct)}", style=pct_color)

    if remaining_qty > 0:
        price_text.append(
            f"   ·   {_format_quantity(remaining_qty)} {base_asset}",
            style=P.dim,
        )

    stats_text = Text(justify="right")
    if price_event:
        stats_text.append(f"H  {_format_money(price_event.high_24h)}\n", style=P.dim)
        stats_text.append(f"L  {_format_money(price_event.low_24h)}", style=P.dim)

    ticker.add_row(price_text, stats_text)

    ticker_border = _pnl_border(price - average_cost_value) if average_cost_value > 0 else "bright_black"
    rows.append(Panel(ticker, border_style=ticker_border, padding=(1, 2)))

    # ── 2. OHLC Chart ───────────────────────────────────────────────── #
    rows.append(_build_candle_chart(candles or [], width, candle_interval, is_picking_candle_interval))

    # ── 3. Open Position + Closed Trades ────────────────────────────── #
    position = _metric_table()
    _add_metric(position, "Avg cost", _format_optional_price(payload.get("average_cost"), quote_asset))
    _add_metric(position, "Cost basis", f"{_format_money(remaining_cost)} {quote_asset}")
    _add_metric(position, "Market value", f"{_format_money(market_value)} {quote_asset}")
    _add_metric(
        position,
        "Unrealized",
        f"{_format_signed_money(unrealized_pnl)} {quote_asset}",
        _pnl_style(unrealized_pnl),
    )

    realized = _metric_table()
    _add_metric(
        realized,
        "Realized",
        f"{_format_signed_money(realized_pnl)} {quote_asset}",
        _pnl_style(realized_pnl),
    )
    _add_metric(
        realized,
        "Trades",
        f"{payload.get('trade_count', '—')} total  ·  {payload.get('buy_count', '—')} buy  ·  {payload.get('sell_count', '—')} sell",
    )

    rows.append(Columns(
        [
            Panel(position, title="Open Position", border_style=_pnl_border(unrealized_pnl), padding=(1, 2)),
            Panel(realized, title="Closed Trades", border_style=_pnl_border(realized_pnl), padding=(1, 2)),
        ],
        equal=True,
        expand=True,
    ))

    # ── 4. Account Balance ───────────────────────────────────────────── #
    filtered_balances = [b for b in balances if not b["asset"].startswith("LD")]
    balance_table = _metric_table()
    total_usd = Decimal("0")
    for b in filtered_balances:
        usd = b["usd_value"]
        total_usd += usd
        locked = b["locked"]
        label = b["asset"] + (f"  [{P.dim}](+{_format_quantity(locked)} locked)[/]" if locked > 0 else "")
        _add_metric(balance_table, label, f"{_format_money(usd)} FDUSD")
    if filtered_balances:
        _add_metric(balance_table, "─" * 20, "")
        total_real_usd = total_usd * fdusd_rate
        _add_metric(
            balance_table,
            "Total",
            f"{_format_money(total_usd)} FDUSD  [{P.dim}]≈ {_format_money(total_real_usd)} USD[/]",
            f"bold {P.text}",
        )

    rows.append(Panel(balance_table, title="Account Balance", border_style=P.border_accent, padding=(1, 2)))

    # ── 5. Groups ────────────────────────────────────────────────────── #
    for group_name, g in group_summaries.items():
        g_remaining_qty = _to_decimal(g.get("remaining_qty"))
        g_remaining_cost = _to_decimal(g.get("remaining_cost_quote"))
        g_market_value = g_remaining_qty * price
        g_unrealized = g_market_value - g_remaining_cost
        g_realized = _to_decimal(g.get("realized_pnl_quote"))

        t = _metric_table()
        _add_metric(t, "Open qty", f"{_format_quantity(g_remaining_qty)} {base_asset}", f"bold {P.text}")
        _add_metric(t, "Avg cost", _format_optional_price(g.get("average_cost"), quote_asset))
        _add_metric(t, "Cost basis", f"{_format_money(g_remaining_cost)} {quote_asset}")
        _add_metric(t, "Market value", f"{_format_money(g_market_value)} {quote_asset}")
        _add_metric(t, "Unrealized", f"{_format_signed_money(g_unrealized)} {quote_asset}", _pnl_style(g_unrealized))
        _add_metric(t, "Realized", f"{_format_signed_money(g_realized)} {quote_asset}", _pnl_style(g_realized))
        _add_metric(t, "Trades", f"{g.get('trade_count', 0)} total  ·  {g.get('buy_count', 0)} buy  ·  {g.get('sell_count', 0)} sell")

        border = _pnl_border(g_unrealized) if g_remaining_qty > 0 else "bright_black"
        rows.append(Panel(t, title=f"[bold {P.neutral}]{group_name}[/]", border_style=border, padding=(1, 2)))

    if not group_summaries:
        rows.append(Panel(
            Text.from_markup(
                f"[{P.dim}]No groups yet.  Press [bold {P.muted}]t[/] to open trade list, "
                f"select a trade and press [bold {P.muted}]g[/] to assign to a group.[/]"
            ),
            title=f"[{P.dim}]Groups[/]",
            border_style=P.border,
            padding=(1, 2),
        ))

    return Group(*rows)


def main() -> None:
    from dotenv import load_dotenv
    load_dotenv()

    price_bus: EventBus[PriceEvent] = EventBus()
    candle_bus: EventBus[CandleEvent] = EventBus()
    conn_bus: EventBus[ConnectionEvent] = EventBus()

    PriceApp(
        symbol=DEFAULT_SYMBOL,
        price_bus=price_bus,
        candle_bus=candle_bus,
        conn_bus=conn_bus,
        stream_manager=StreamManager(DEFAULT_SYMBOL, price_bus, conn_bus),
        rest_client=BinanceRestClient(),
    ).run()


if __name__ == "__main__":
    main()
