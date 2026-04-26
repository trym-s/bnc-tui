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

import argparse
import asyncio
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
from app.models.events import ConnectionEvent, ConnectionState, PriceEvent
from app.storage.groups import GroupStore
from app.streams.binance_ws import StreamManager
from app.streams.event_bus import EventBus
from app.tui.palette import P
from app.tui.screens.trade_list import TradeListScreen

_TRADE_REFRESH_SECONDS = 30.0


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
        conn_bus: EventBus[ConnectionEvent],
        stream_manager: StreamManager,
        rest_client: BinanceRestClient,
    ) -> None:
        super().__init__()
        self.symbol = symbol.upper()
        self._price_bus = price_bus
        self._conn_bus = conn_bus
        self._stream_manager = stream_manager
        self._rest_client = rest_client

        # Son gelen değerleri saklıyoruz.
        # Fiyat WebSocket'ten, trade summary REST'ten geliyor.
        # İkisi bağımsız güncellendiği için ayrı tutulur.
        self._latest_event: PriceEvent | None = None
        self._latest_summary: dict = {}
        self._latest_balances: list[dict] = []
        self._latest_fdusd_rate: Decimal = Decimal("1")
        self._latest_trades: list[dict] = []
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

        # StreamManager arka planda WebSocket'e bağlanıp push etmeye başlar
        self.run_worker(self._stream_manager.run(), exclusive=False)

        # Fiyat event'lerini dinle
        self.run_worker(self._listen_prices(), exclusive=False)

        # Bağlantı durumunu dinle (status label için)
        self.run_worker(self._listen_connection(), exclusive=False)

        # Trade summary ve bakiyeyi ilk çek, sonra 30s'de bir yenile
        self.run_worker(self._refresh_trades_loop(), exclusive=False)

    async def action_refresh(self) -> None:
        """r tuşu: trade summary + bakiyeyi manuel yenile"""
        await self._fetch_trades()
        await self._fetch_balances()

    def action_trade_list(self) -> None:
        """t tuşu: trade listesi ekranına geç"""
        self.push_screen(TradeListScreen(self.symbol, self._latest_trades, self._group_store))

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

    async def _refresh_trades_loop(self) -> None:
        """Trade summary + bakiyeyi 30 saniyede bir çeker."""
        while True:
            await asyncio.gather(self._fetch_trades(), self._fetch_balances())
            await asyncio.sleep(_TRADE_REFRESH_SECONDS)

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


def _build_dashboard(
    symbol: str,
    price: Decimal,
    payload: dict,
    balances: list[dict],
    fdusd_rate: Decimal,
    group_summaries: dict[str, dict],
    price_event: PriceEvent | None = None,
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

    # ── 2. Open Position + Closed Trades ────────────────────────────── #
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

    # ── 3. Account Balance ───────────────────────────────────────────── #
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

    # ── 4. Groups ────────────────────────────────────────────────────── #
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Live Binance dashboard (WebSocket)")
    parser.add_argument("--symbol", default="BTCUSDT")
    return parser.parse_args()


def main() -> None:
    from dotenv import load_dotenv
    load_dotenv()

    args = parse_args()

    price_bus: EventBus[PriceEvent] = EventBus()
    conn_bus: EventBus[ConnectionEvent] = EventBus()

    PriceApp(
        symbol=args.symbol,
        price_bus=price_bus,
        conn_bus=conn_bus,
        stream_manager=StreamManager(args.symbol, price_bus, conn_bus),
        rest_client=BinanceRestClient(),
    ).run()


if __name__ == "__main__":
    main()
