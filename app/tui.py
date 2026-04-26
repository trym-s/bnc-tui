from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation

import httpx
from rich.columns import Columns
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal
from textual.widgets import Footer, Header, Label, Static


class PriceApp(App[None]):
    CSS = """
    Screen {
        background: #0f1216;
        color: #f2f2f2;
    }

    #panel {
        width: 100%;
        height: 100%;
        align: center top;
        padding: 1 2;
    }

    #price-card {
        width: 100%;
        max-width: 100%;
        height: auto;
        min-height: 30;
        padding: 0 1;
        background: #0f1216;
    }

    #pair-row {
        height: 3;
        align: left middle;
    }

    #symbol {
        text-style: bold;
        color: #f8fafc;
        width: 1fr;
    }

    #status {
        color: #94a3b8;
        width: auto;
    }

    #dashboard {
        height: auto;
    }

    #error {
        color: #f97316;
        height: 3;
    }
    """

    BINDINGS = [("q", "quit", "Quit"), ("r", "refresh", "Refresh")]

    def __init__(self, api_url: str, symbol: str, refresh_seconds: float) -> None:
        super().__init__()
        self.api_url = api_url.rstrip("/")
        self.symbol = symbol.upper()
        self.refresh_seconds = refresh_seconds

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Container(id="panel"):
            with Container(id="price-card"):
                with Horizontal(id="pair-row"):
                    yield Label(self.symbol, id="symbol")
                    yield Label("starting", id="status")
                yield Static(_loading_panel(), id="dashboard")
                yield Static("", id="error")
        yield Footer()

    async def on_mount(self) -> None:
        self.set_interval(self.refresh_seconds, self.refresh_price)
        await self.refresh_price()

    async def action_refresh(self) -> None:
        await self.refresh_price()

    async def refresh_price(self) -> None:
        status = self.query_one("#status", Label)
        error = self.query_one("#error", Static)
        status.update("loading")
        error.update("")

        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                price_response = await client.get(f"{self.api_url}/price/{self.symbol}")
                price_response.raise_for_status()
                price_payload = price_response.json()

                summary_response = await client.get(f"{self.api_url}/trades/{self.symbol}/summary")
                summary_response.raise_for_status()
                summary_payload = summary_response.json()
        except httpx.HTTPStatusError as exc:
            status.update("error")
            error.update(_http_error_message(exc))
            return
        except httpx.HTTPError as exc:
            status.update("error")
            error.update(f"API request failed: {exc}")
            return

        price = _to_decimal(price_payload.get("price"))
        self.query_one("#dashboard", Static).update(
            _build_dashboard(self.symbol, self.api_url, price, summary_payload)
        )
        status.update("live")


def _to_decimal(value: object) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _format_decimal(value: Decimal) -> str:
    normalized = value.normalize()
    return f"{normalized:f}"


def _format_decimal_places(value: Decimal, places: int) -> str:
    quant = Decimal("1").scaleb(-places)
    rounded = value.quantize(quant)
    return f"{rounded:,.{places}f}"


def _format_price(value: object) -> str:
    return _format_decimal(_to_decimal(value))


def _format_optional_price(value: object, quote_asset: str) -> str:
    if value is None:
        return "-"
    return f"{_format_money(_to_decimal(value))} {quote_asset}"


def _format_money(value: Decimal) -> str:
    return _format_decimal_places(value, 2)


def _format_quantity(value: Decimal) -> str:
    text = _format_decimal_places(value, 8)
    return text.rstrip("0").rstrip(".")


def _format_signed_money(value: Decimal) -> str:
    if value > 0:
        return f"+{_format_money(value)}"
    return _format_money(value)


def _format_signed_percent(value: Decimal) -> str:
    formatted = _format_decimal_places(value, 2)
    if value > 0:
        return f"+{formatted}%"
    return f"{formatted}%"


def _split_symbol(symbol: str) -> tuple[str, str]:
    quote_assets = ("USDT", "USDC", "FDUSD", "BUSD", "TRY", "BTC", "ETH", "BNB")
    for quote in quote_assets:
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return symbol[: -len(quote)], quote
    return "BASE", "QUOTE"


def _loading_panel() -> Panel:
    return Panel(
        Text("Loading price and account trades...", style="bold bright_white"),
        border_style="bright_black",
    )


def _pnl_style(value: Decimal) -> str:
    if value > 0:
        return "bold green"
    if value < 0:
        return "bold red"
    return "bright_white"


def _add_metric(table: Table, label: str, value: str, style: str = "bright_white") -> None:
    table.add_row(f"[bright_black]{label}[/]", f"[{style}]{value}[/]")


def _metric_table() -> Table:
    table = Table.grid(expand=True)
    table.add_column(ratio=1)
    table.add_column(justify="right", ratio=1)
    return table


def _build_dashboard(symbol: str, api_url: str, price: Decimal, payload: dict[str, object]) -> Group:
    base_asset, quote_asset = _split_symbol(symbol)
    remaining_qty = _to_decimal(payload.get("remaining_qty"))
    remaining_cost = _to_decimal(payload.get("remaining_cost_quote"))
    market_value = remaining_qty * price
    unrealized_pnl = market_value - remaining_cost
    realized_pnl = _to_decimal(payload.get("realized_pnl_quote"))

    bought_qty = _format_quantity(_to_decimal(payload.get("bought_qty")))
    sold_qty = _format_quantity(_to_decimal(payload.get("sold_qty")))
    bought_quote = _format_money(_to_decimal(payload.get("buy_quote_qty")))
    sold_quote = _format_money(_to_decimal(payload.get("sell_quote_qty")))
    open_qty = _format_quantity(remaining_qty)
    average_buy = _format_optional_price(payload.get("average_buy_price"), quote_asset)
    average_sell = _format_optional_price(payload.get("average_sell_price"), quote_asset)
    average_cost_value = _to_decimal(payload.get("average_cost"))
    average_cost = _format_optional_price(payload.get("average_cost"), quote_asset)
    price_vs_cost_pct = _safe_percentage(price - average_cost_value, average_cost_value)
    trade_count = payload.get("trade_count", "-")
    buy_count = payload.get("buy_count", "-")
    sell_count = payload.get("sell_count", "-")

    headline = _metric_table()
    _add_metric(headline, "Last price", f"{_format_money(price)} {quote_asset}", "bold green")
    _add_metric(headline, "Average cost", average_cost, "bright_white")
    _add_metric(
        headline,
        "Price vs cost",
        _format_signed_percent(price_vs_cost_pct),
        _pnl_style(price_vs_cost_pct),
    )
    _add_metric(headline, "Open amount", f"{open_qty} {base_asset}", "bold bright_white")

    position = _metric_table()
    _add_metric(position, "Cost paid for open amount", f"{_format_money(remaining_cost)} {quote_asset}")
    _add_metric(position, "Value at current price", f"{_format_money(market_value)} {quote_asset}")
    _add_metric(
        position,
        "Unrealized PnL",
        f"{_format_signed_money(unrealized_pnl)} {quote_asset}",
        _pnl_style(unrealized_pnl),
    )

    flow = _metric_table()
    _add_metric(flow, "FDUSD spent on buys", f"{bought_quote} {quote_asset}", "bold cyan")
    _add_metric(flow, f"{base_asset} bought", f"{bought_qty} {base_asset}")
    _add_metric(flow, "Average buy", average_buy)
    _add_metric(flow, "FDUSD received from sells", f"{sold_quote} {quote_asset}", "bold magenta")
    _add_metric(flow, f"{base_asset} sold", f"{sold_qty} {base_asset}")
    _add_metric(flow, "Average sell", average_sell)

    realized = _metric_table()
    _add_metric(realized, "Realized PnL", f"{_format_signed_money(realized_pnl)} {quote_asset}", _pnl_style(realized_pnl))
    _add_metric(realized, "Fills", f"{trade_count} total  |  {buy_count} buy  |  {sell_count} sell")
    _add_metric(realized, "Data", f"{api_url}  |  fees not included yet", "bright_black")

    panels = [
        Panel(headline, title="Market", border_style="green", padding=(1, 2)),
        Panel(position, title="Open Position", border_style=_pnl_style(unrealized_pnl).replace("bold ", ""), padding=(1, 2)),
        Panel(flow, title="Trade Flow", border_style="cyan", padding=(1, 2)),
        Panel(realized, title="Closed Trades", border_style=_pnl_style(realized_pnl).replace("bold ", ""), padding=(1, 2)),
    ]

    return Group(
        Columns(panels[:2], equal=True, expand=True),
        Columns(panels[2:], equal=True, expand=True),
    )


def _safe_percentage(numerator: Decimal, denominator: Decimal) -> Decimal:
    if denominator == 0:
        return Decimal("0")
    return (numerator / denominator) * Decimal("100")


def _http_error_message(exc: httpx.HTTPStatusError) -> str:
    try:
        payload = exc.response.json()
    except ValueError:
        return f"API returned HTTP {exc.response.status_code}."

    detail = payload.get("detail") if isinstance(payload, dict) else None
    return str(detail or f"API returned HTTP {exc.response.status_code}.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Show a live Binance pair price.")
    parser.add_argument("--symbol", default="BTCUSDT", help="Trading pair symbol, e.g. BTCUSDT.")
    parser.add_argument("--api-url", default="http://127.0.0.1:8000", help="FastAPI base URL.")
    parser.add_argument("--refresh", type=float, default=3.0, help="Refresh interval in seconds.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    PriceApp(args.api_url, args.symbol, args.refresh).run()


if __name__ == "__main__":
    main()
