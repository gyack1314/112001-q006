"""映射调整:新版本映射重跑全量,旧事实版本保留,隔离记录可解除,结果可对比。"""
import unittest

from helpers import v2_asset

from src.compare import compare_mapping_versions
from src.ingest import ingest_payload
from src.normalize import process_all, reprocess_quarantined
from src.snapshot import create_snapshot, snapshot_report
from src.store import Store


class MappingAdjustmentTest(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        # 厂商扩展枚举:m1.0 未收录,m1.1 增补映射为 NR
        ingest_payload(self.store, v2_asset(asset="ADV", standard="5G-ADVANCED", down=400, up=100, unit="Mbps"))
        ingest_payload(self.store, v2_asset(asset="BASE", standard="5G", down=100, up=50, unit="Mbps"))

    def tearDown(self):
        self.store.close()

    def test_unknown_enum_quarantined_under_m10_resolved_under_m11(self):
        process_all(self.store, "m1.0")
        # m1.0:5G-ADVANCED 隔离,BASE 入账
        self.assertEqual(len(self.store.get_facts(fact_version="cf1+m1.0")), 1)
        open_q = self.store.quarantines(status="open")
        self.assertEqual(len(open_q), 1)
        self.assertEqual(open_q[0]["code"], "unknown_enum")

        # m1.1:全量重跑,扩展枚举入账;旧事实版本保留
        process_all(self.store, "m1.1")
        reprocess_quarantined(self.store, "m1.1")
        self.assertEqual(len(self.store.get_facts(fact_version="cf1+m1.1")), 2)
        self.assertEqual(len(self.store.get_facts(fact_version="cf1+m1.0")), 1)

        adv = self.store.get_facts(fact_version="cf1+m1.1", asset_id="ADV")
        self.assertEqual(adv[0]["standard"], "NR")
        # 隔离记录解除,并记录解除时使用的映射版本
        self.assertEqual(self.store.quarantines(status="open"), [])
        resolved = self.store.quarantines(status="resolved")
        self.assertTrue(any(q["resolved_with_mapping"] == "m1.1" for q in resolved))

    def test_compare_mapping_versions_shows_delta(self):
        process_all(self.store, "m1.0")
        self.assertEqual(len(self.store.quarantines(status="open")), 1)
        # 两遍处理下,m1.1 全量重跑即让扩展枚举入账并解除隔离
        process_all(self.store, "m1.1")
        self.assertEqual(self.store.quarantines(status="open"), [])
        self.assertEqual(len(self.store.get_facts(fact_version="cf1+m1.1")), 2)

        diff = compare_mapping_versions(self.store, "2026-01-01", "2026-12-31", "m1.0", "m1.1")
        added_assets = [f["asset_id"] for f in diff["asset_diff"]["added"]]
        self.assertEqual(added_assets, ["ADV"])
        self.assertEqual(diff["asset_diff"]["removed"], [])
        nr_delta = [d for d in diff["capacity_delta"] if d["standard"] == "NR"][0]
        self.assertEqual(nr_delta["available_down"], {"old": 100.0, "new": 500.0})

    def test_snapshot_pins_mapping_and_fact_version(self):
        process_all(self.store, "m1.0")
        sid = create_snapshot(self.store, "2026-01-01", "2026-12-31", "m1.0")
        meta = self.store.get_snapshot(sid)
        self.assertEqual(meta["mapping_version"], "m1.0")
        self.assertEqual(meta["fact_version"], "cf1+m1.0")

        # 映射调整后重跑,旧快照内容不变
        process_all(self.store, "m1.1")
        reprocess_quarantined(self.store, "m1.1")
        report = snapshot_report(self.store, sid)
        self.assertEqual(len(report["asset_facts"]), 1)
        self.assertEqual(report["asset_facts"][0]["asset_id"], "BASE")


if __name__ == "__main__":
    unittest.main()
