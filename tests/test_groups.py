import tempfile
import unittest
from pathlib import Path

from app.storage.groups import GroupStore


class GroupStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "groups.json"

    def test_assign_creates_group_and_persists(self):
        store = GroupStore(self.path)
        store.assign("btcusdt", 101, "Long")

        self.assertEqual(store.get_groups("BTCUSDT"), {"Long": [101]})
        self.assertEqual(store.get_trade_group("BTCUSDT", 101), "Long")

        reloaded = GroupStore(self.path)
        self.assertEqual(reloaded.get_groups("btcusdt"), {"Long": [101]})
        self.assertEqual(reloaded.get_trade_group("btcusdt", 101), "Long")

    def test_assign_moves_trade_between_groups_and_removes_empty_group(self):
        store = GroupStore(self.path)

        store.assign("ETHUSDT", 201, "Swing")
        store.assign("ETHUSDT", 202, "Swing")
        store.assign("ETHUSDT", 201, "Scalp")

        self.assertEqual(store.get_groups("ETHUSDT"), {"Swing": [202], "Scalp": [201]})
        self.assertEqual(store.get_trade_group("ETHUSDT", 201), "Scalp")

    def test_assign_same_trade_to_same_group_is_idempotent(self):
        store = GroupStore(self.path)

        store.assign("SOLUSDT", 301, "Watch")
        store.assign("SOLUSDT", 301, "Watch")

        self.assertEqual(store.get_groups("SOLUSDT"), {"Watch": [301]})

    def test_unassign_removes_trade_and_cleans_empty_group(self):
        store = GroupStore(self.path)

        store.assign("BNBUSDT", 401, "Long")
        store.unassign("BNBUSDT", 401)

        self.assertEqual(store.get_groups("BNBUSDT"), {})
        self.assertIsNone(store.get_trade_group("BNBUSDT", 401))

    def test_unassign_unknown_trade_is_noop(self):
        store = GroupStore(self.path)

        store.assign("ADAUSDT", 501, "Long")
        store.unassign("ADAUSDT", 999)

        self.assertEqual(store.get_groups("ADAUSDT"), {"Long": [501]})
