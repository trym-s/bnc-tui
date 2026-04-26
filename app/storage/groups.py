"""
Grup atamaları — hangi trade hangi gruba ait?

Neden disk'e kaydediyoruz?
  Grup atamaları kullanıcının kararı. Uygulama kapanınca silinmemeli.

Format (~/.bnc-tui/groups.json):
  {
    "BTCFDUSD": {
      "Uzun Vadeli": [12345, 12346],
      "Kısa Vadeli": [12347]
    }
  }

Trade ID'ler Binance'in benzersiz integer ID'leri — değişmez, güvenli key.
"""

import json
from pathlib import Path

_STORE_PATH = Path.home() / ".bnc-tui" / "groups.json"


class GroupStore:
    def __init__(self, path: Path = _STORE_PATH) -> None:
        self._path = path
        self._data: dict[str, dict[str, list[int]]] = {}
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            self._data = json.loads(self._path.read_text())

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._data, indent=2))

    def get_groups(self, symbol: str) -> dict[str, list[int]]:
        """Sembol için tüm grupları döner: {grup_adı: [trade_id, ...]}"""
        return dict(self._data.get(symbol.upper(), {}))

    def get_trade_group(self, symbol: str, trade_id: int) -> str | None:
        """Trade'in hangi grupta olduğunu döner, yoksa None."""
        for name, ids in self._data.get(symbol.upper(), {}).items():
            if trade_id in ids:
                return name
        return None

    def assign(self, symbol: str, trade_id: int, group_name: str) -> None:
        """Trade'i bir gruba atar. Önceki grup atamasını kaldırır."""
        sym = symbol.upper()
        if sym not in self._data:
            self._data[sym] = {}
        # Diğer gruplardan kaldır
        for ids in self._data[sym].values():
            if trade_id in ids:
                ids.remove(trade_id)
        # Yeni gruba ekle. Aynı atama tekrar gelirse ID'yi çoğaltma.
        target_ids = self._data[sym].setdefault(group_name, [])
        if trade_id not in target_ids:
            target_ids.append(trade_id)
        self._cleanup(sym)
        self._save()

    def unassign(self, symbol: str, trade_id: int) -> None:
        """Trade'in grup atamasını kaldırır."""
        sym = symbol.upper()
        for ids in self._data.get(sym, {}).values():
            if trade_id in ids:
                ids.remove(trade_id)
        self._cleanup(sym)
        self._save()

    def _cleanup(self, sym: str) -> None:
        """Boş grupları sil."""
        if sym in self._data:
            self._data[sym] = {k: v for k, v in self._data[sym].items() if v}
