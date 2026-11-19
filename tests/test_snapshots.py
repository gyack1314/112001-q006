"""期间快照:钉住映射/事实版本、内容幂等、有效期过滤、跨快照对比。"""
import unittest

from helpers import v1_asset, v2_asset

from src.compare import compare_snapshots
from src.ingest import ingest_payload
from src.normalize import process_all
from src.snapshot import create_snapshot, snapshot_report
from src.store import Store


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        ingest_payload(self.store, v1_asset(asset="OLD", valid_from="2026-01-01", valid_to="2026-03-31"))
        ingest_payload(self.store, v2_asset(asset="NEW", valid_from="2026-03-01"))
        process_all(self.store, "m1.0")

    def tearDown(self):
        self.store.close()

    def test_snapshot_materializes_facts_overlapping_period(self):
        sid = create_snapshot(self.store, "2026-01-01", "2026-12-31", "m1.0")
        report = snapshot_report(self.store, sid)
        self.assertEqual(
            sorted(f["asset_id"] for f in report["asset_facts"]), ["NEW", "OLD"]
        )

    def test_snapshot_period_filters_non_overlapping_facts(self):
        sid = create_snapshot(self.store, "2026-06-01", "2026-06-30", "m1.0")
        report = snapshot_report(self.store, sid)
        # OLD 有效期止于 2026-03-31,不在 6 月期间内
        self.assertEqual([f["asset_id"] for f in report["asset_facts"]], ["NEW"])

    def test_snapshot_idempotent_by_content(self):
        s1 = create_snapshot(self.store, "2026-01-01", "2026-12-31", "m1.0")
        s2 = create_snapshot(self.store, "2026-01-01", "2026-12-31", "m1.0")
        self.assertEqual(s1, s2)
        self.assertEqual(len(self.store.list_snapshots()), 1)

    def test_snapshot_immutable_after_reprocessing(self):
        s1 = create_snapshot(self.store, "2026-01-01", "2026-12-31", "m1.0")
        before = snapshot_report(self.store, s1)
        # 更正报文:NEW 容量修正(同一有效期起点,后报为准)
        ingest_payload(
            self.store,
            v2_asset(asset="NEW", down=99, up=5, valid_from="2026-03-01", report_id="fix-1"),
        )
        process_all(self.store, "m1.0")
        after = snapshot_report(self.store, s1)

        def canonical(rows):
            # 快照物化的事实内容(fact_id 为内容寻址)不受后续更正影响;
            # superseded_by 是后续更正留下的溯源标记,不属于当时的事实内容。
            return sorted(
                (f["fact_id"], f["asset_id"], f["capacity_down"], f["valid_from"]) for f in rows
            )

        self.assertEqual(canonical(before["asset_facts"]), canonical(after["asset_facts"]))
        # 而当前查询反映更正后的值
        current = self.store.get_facts(fact_version="cf1+m1.0", asset_id="NEW")
        self.assertEqual(current[0]["capacity_down"], 99000.0)

    def test_compare_snapshots_reports_changes(self):
        s1 = create_snapshot(self.store, "2026-01-01", "2026-12-31", "m1.0")
        ingest_payload(self.store, v2_asset(asset="EXTRA", down=1, up=1, valid_from="2026-05-01"))
        process_all(self.store, "m1.0")
        s2 = create_snapshot(self.store, "2026-01-01", "2026-12-31", "m1.0")
        diff = compare_snapshots(self.store, s1, s2)
        self.assertEqual([f["asset_id"] for f in diff["asset_diff"]["added"]], ["EXTRA"])
        self.assertEqual(diff["asset_diff"]["removed"], [])
        self.assertEqual(diff["asset_diff"]["changed"], [])


if __name__ == "__main__":
    unittest.main()
