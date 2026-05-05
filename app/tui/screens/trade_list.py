"""
Trade List ekranı — trade'leri görüntüle ve gruplara ata.

Açılış: ana ekranda 't' tuşu
Kontroller:
  g        → seçili trade'i bir gruba ata
  d        → seçili trade'in grup atamasını kaldır
  ESC      → ana ekrana dön
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from textual.app import ComposeResult
from textual.binding import Binding
from textual.coordinate import Coordinate
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, Static

from app.tui.palette import P


class GroupInputModal(ModalScreen[str | None]):
    """
    Grup adı girmek için modal ekran.

    ModalScreen[str | None]:
      Kapanırken bir değer döner. TUI'da modal'ın sonucunu
      `await self.app.push_screen(modal, callback)` ile alırız.
      str → kullanıcı grup adı girdi
      None → iptal etti (ESC)
    """

    # Modal CSS — Textual widget stilleri $değişken ile, diğerleri sabit
    CSS = """
    GroupInputModal {
        align: center middle;
    }
    #dialog {
        width: 52;
        height: 13;
        border: solid $border-accent;
        background: $surface;
        padding: 1 2;
    }
    #dialog Label {
        color: $muted;
        margin-bottom: 1;
    }
    #dialog Input {
        border: solid $border-accent;
        background: $background;
        color: $foreground;
        margin-bottom: 1;
    }
    #dialog Input:focus {
        border: solid $primary;
    }
    #buttons {
        align: right middle;
        height: 3;
    }
    Button {
        background: $surface;
        border: solid $border-accent;
        color: $muted;
        min-width: 8;
    }
    Button:focus {
        border: solid $primary;
        color: $foreground;
    }
    Button.-primary {
        background: $surface;
        border: solid $success;
        color: $success;
    }
    Button.-primary:focus {
        border: solid $secondary;
        color: $secondary;
    }
    """

    BINDINGS = [("escape", "cancel", "İptal")]

    def compose(self) -> ComposeResult:
        with Static(id="dialog"):
            yield Label("Group name:")
            yield Input(placeholder="e.g. Long Term", id="group-input")
            with Static(id="buttons"):
                yield Button("Assign", variant="primary", id="confirm")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm":
            self._submit()
        else:
            self.dismiss(None)

    def on_input_submitted(self) -> None:
        self._submit()

    def _submit(self) -> None:
        value = self.query_one(Input).value.strip()
        self.dismiss(value if value else None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class TradeListScreen(Screen):
    """
    Tüm trade'leri listeleyen ve gruplama yapılan ekran.
    """

    CSS = """
    Screen {
        background: $background;
    }
    DataTable {
        background: $background;
        color: $muted;
    }
    DataTable > .datatable--header {
        background: $surface;
        color: $muted;
        text-style: bold;
    }
    DataTable > .datatable--cursor {
        background: $border-accent;
        color: $foreground;
    }
    DataTable > .datatable--odd-row {
        background: $background;
    }
    DataTable > .datatable--even-row {
        background: $surface;
    }
    #hint {
        height: 1;
        padding: 0 2;
        color: $dim;
        background: $background;
    }
    """

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("up", "cursor_up_or_wrap", "Up", show=False, priority=True),
        Binding("g", "assign_group", "Assign group"),
        Binding("d", "unassign", "Remove from group"),
    ]

    def __init__(self, symbol: str, trades: list[dict], group_store) -> None:
        super().__init__()
        self._symbol = symbol
        self._trades = sorted(trades, key=lambda t: t["time"], reverse=True)
        self._store = group_store

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(id="trade-table", cursor_type="row")
        yield Static(id="hint", markup=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("Date", "Side", "Price", "Qty", "Total", "Commission", "Group")
        self._populate()

    def _populate(self) -> None:
        table = self.query_one(DataTable)
        table.clear()
        for t in self._trades:
            dt = datetime.fromtimestamp(t["time"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            side = f"[{P.positive}]BUY[/]" if t["isBuyer"] else f"[{P.negative}]SELL[/]"
            price = f"{Decimal(t['price']):,.2f}"
            qty = Decimal(t["qty"]).normalize()
            quote = f"{Decimal(t['quoteQty']):,.2f}"
            commission = f"{Decimal(t['commission']).normalize()} {t['commissionAsset']}"
            group = self._store.get_trade_group(self._symbol, t["id"]) or f"[{P.dim}]—[/]"
            table.add_row(dt, side, price, str(qty), quote, commission, group, key=str(t["id"]))

        hint = self.query_one("#hint", Static)
        hint.update(
            f"[{P.dim}]{len(self._trades)} trades  ·  "
            f"[bold {P.muted}]g[/] assign group  "
            f"[bold {P.muted}]d[/] remove from group  "
            f"[bold {P.muted}]ESC[/] back[/]"
        )

    def _selected_trade_id(self) -> int | None:
        table = self.query_one(DataTable)
        if table.cursor_row is None:
            return None
        cursor_key = table.coordinate_to_cell_key(table.cursor_coordinate)
        return int(cursor_key.row_key.value)

    def _refresh_selected_group_cell(self) -> None:
        table = self.query_one(DataTable)
        if table.cursor_row is None:
            return
        trade_id = self._selected_trade_id()
        if trade_id is None:
            return
        group = self._store.get_trade_group(self._symbol, trade_id) or f"[{P.dim}]—[/]"
        table.update_cell_at(Coordinate(table.cursor_row, 6), group, update_width=True)

    def action_cursor_up_or_wrap(self) -> None:
        table = self.query_one(DataTable)
        if table.row_count == 0:
            return
        if table.cursor_row == 0:
            table.move_cursor(row=table.row_count - 1, animate=False)
            return
        table.action_cursor_up()

    def action_assign_group(self) -> None:
        table = self.query_one(DataTable)
        if table.cursor_row is None:
            return

        def on_group_selected(group_name: str | None) -> None:
            if not group_name:
                return
            cursor_key = table.coordinate_to_cell_key(table.cursor_coordinate)
            trade_id = int(cursor_key.row_key.value)
            self._store.assign(self._symbol, trade_id, group_name)
            self._refresh_selected_group_cell()
            self.app.refresh_groups()

        self.app.push_screen(GroupInputModal(), on_group_selected)

    def action_unassign(self) -> None:
        trade_id = self._selected_trade_id()
        if trade_id is None:
            return
        self._store.unassign(self._symbol, trade_id)
        self._refresh_selected_group_cell()
        self.app.refresh_groups()
