"""共享资源:归属口径计一次、成员可追溯、关系不完整可诊断。"""
import unittest

from helpers import v2_asset, v2_resource

from src.aggregate import (
    asset_view,
    capacity_by_standard,
    naive_double_counted_down,
    scope_shared_capacity,
)
from src.ingest import ingest_payload
from src.normalize import process_all, process_raw, reprocess_quarantined
from src.store import Store

FV = "cf1+m1.0"


class SharedCapacityTest(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        # 共享资源 R-1:归属 SITE-1,5G,40/10 Gbps;两个成员资产 A/B。
        for payload in (
            v2_resource(resource_id="R-1", down=40, up=10),
            v2_asset(asset="A", down=15, up=5, shared_resource="R-1"),
            v2_asset(asset="B", down=8, up=2, shared_resource="R-1"),
        ):
            ingest_payload(self.store, payload)
        process_all(self.store, "m1.0")

    def tearDown(self):
        self.store.close()

    def test_shared_counted_once_not_per_member(self):
        [row] = capacity_by_standard(self.store, "2026-06-01", FV)
        self.assertEqual(row["standard"], "NR")
        # 独享:15+8=23 Gbps → 23000 MBPS;共享:40 Gbps 计一次(非 2×40)
        self.assertEqual(row["dedicated_down"], 23000.0)
        self.assertEqual(row["shared_down"], 40000.0)
        self.assertEqual(row["available_down"], 63000.0)
        self.assertEqual(row["shared_resource_count"], 1)

    def test_naive_double_count_differs(self):
        naive = naive_double_counted_down(self.store, "2026-06-01", FV)
        # 朴素口径:独享 23000 + 共享 40000 × 2 个成员 = 103000
        self.assertEqual(naive, 103000.0)
        [row] = capacity_by_standard(self.store, "2026-06-01", FV)
        self.assertNotEqual(naive, row["available_down"])

    def test_member_traces_to_resource(self):
        view = asset_view(self.store, "A", "2026-06-01", FV)
        # 资产入账容量仅含独享部分
        self.assertEqual(view["counted_capacity"]["down"], 15000.0)
        # 但可追溯共享资源及其归属口径
        [trace] = view["shared_traces"]
        self.assertEqual(trace["resource_id"], "R-1")
        self.assertEqual(trace["owner_scope"], "site:SITE-1")

    def test_scope_ownership_view(self):
        scope = scope_shared_capacity(self.store, "site", "SITE-1", "2026-06-01", FV)
        self.assertEqual(scope["total_down"], 40000.0)
        self.assertEqual(sorted(scope["resources"][0]["members"]), ["A", "B"])


class IncompleteRelationTest(unittest.TestCase):
    def setUp(self):
        self.store = Store()

    def tearDown(self):
        self.store.close()

    def test_dangling_reference_quarantined_then_resolved_by_backfill(self):
        # 资产引用尚未上报的共享资源 → 关系不完整,隔离(不丢、不伪装)
        raw_id = ingest_payload(
            self.store, v2_asset(asset="D", shared_resource="R-LATE")
        ).raw_id
        result = process_raw(self.store, raw_id, "m1.0")
        self.assertEqual(result.outcome, "quarantined")
        self.assertEqual(result.code, "incomplete_relation")
        self.assertEqual(self.store.get_facts(), [])

        # 补传到达:资源上报入库并处理
        late_id = ingest_payload(
            self.store, v2_resource(resource_id="R-LATE", valid_from="2026-01-01")
        ).raw_id
        process_raw(self.store, late_id, "m1.0")

        # 重处理挂起记录 → 关系完整,进入下游,隔离解除
        [retry] = reprocess_quarantined(self.store, "m1.0")
        self.assertEqual(retry.outcome, "fact")
        self.assertEqual(self.store.quarantines(status="open"), [])
        resolved = self.store.quarantines(status="resolved")
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0]["resolved_with_mapping"], "m1.0")

    def test_reference_to_non_overlapping_period_quarantined(self):
        # 资源存在但有效期不重叠 → 关系仍不完整
        ingest_payload(self.store, v2_resource(resource_id="R-2", valid_from="2026-06-01"))
        process_all(self.store, "m1.0")
        raw_id = ingest_payload(
            self.store,
            v2_asset(asset="E", valid_from="2026-01-01", valid_to="2026-03-31", shared_resource="R-2"),
        ).raw_id
        result = process_raw(self.store, raw_id, "m1.0")
        self.assertEqual(result.code, "incomplete_relation")


if __name__ == "__main__":
    unittest.main()
