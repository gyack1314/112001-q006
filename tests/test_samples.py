"""端到端:仓库合成报文样本的完整接入演练流程(与 demo 一致的叙事)。

样本均为合成报文,仅覆盖本地演练场景。
"""
import unittest
from pathlib import Path

from src.aggregate import capacity_by_standard, naive_double_counted_down
from src.compare import compare_snapshots
from src.ingest import ingest_file
from src.normalize import process_all, reprocess_quarantined
from src.snapshot import create_snapshot, snapshot_report
from src.store import Store

DATA = Path(__file__).resolve().parent.parent / "data"


class SampleFlowTest(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        for name in ("messages.json", "v1_backfill.json", "v2_batch.json"):
            ingest_file(self.store, DATA / name)

    def tearDown(self):
        self.store.close()

    def test_initial_processing_m10(self):
        process_all(self.store, "m1.0")
        # 6 条资产事实:A-01(v1)、A-01(v2)、B-03、A-02、A-03、C-01
        self.assertEqual(len(self.store.get_facts(fact_version="cf1+m1.0")), 6)
        # 2 条共享资源:R-9、R-10
        self.assertEqual(len(self.store.get_shared_facts(fact_version="cf1+m1.0")), 2)
        # 6 条隔离:未知枚举 ×2(NB-IoT、5G-ADVANCED)、缺字段 ×2、有效期倒置、关系悬空
        codes = sorted(q["code"] for q in self.store.quarantines(status="open"))
        self.assertEqual(
            codes,
            sorted(
                [
                    "unknown_enum",
                    "unknown_enum",
                    "missing_field",
                    "missing_field",
                    "invalid_period",
                    "incomplete_relation",
                ]
            ),
        )

    def test_full_drill_flow(self):
        process_all(self.store, "m1.0")
        s1 = create_snapshot(self.store, "2026-01-01", "2026-12-31", "m1.0")

        # 补传:R-LATE 上报 + A-03 重传(幂等去重)
        ingest_file(self.store, DATA / "v2_late_batch.json")
        process_all(self.store, "m1.0")
        reprocess_quarantined(self.store, "m1.0")
        facts_m10 = self.store.get_facts(fact_version="cf1+m1.0")
        # D-01 解除隔离入账;A-03 重传不产生重复事实
        self.assertEqual(len([f for f in facts_m10 if f["asset_id"] == "A-03"]), 1)
        self.assertEqual(len([f for f in facts_m10 if f["asset_id"] == "D-01"]), 1)

        # 映射调整 m1.1:5G-ADVANCED → NR
        process_all(self.store, "m1.1")
        reprocess_quarantined(self.store, "m1.1")
        s2 = create_snapshot(self.store, "2026-01-01", "2026-12-31", "m1.1")

        # 快照对比:S2 相对 S1 新增 D-01(补传解除)与 C-02(映射调整解除)
        diff = compare_snapshots(self.store, s1, s2)
        self.assertEqual(
            sorted(f["asset_id"] for f in diff["asset_diff"]["added"]), ["C-02", "D-01"]
        )
        self.assertEqual([f["resource_id"] for f in diff["shared_diff"]["added"]], ["R-LATE"])

        # 剩余挂起隔离:NB-IoT、缺 capacity、有效期倒置、缺 up 分量
        remaining = sorted(q["code"] for q in self.store.quarantines(status="open", mapping_version="m1.1"))
        self.assertEqual(
            remaining, sorted(["unknown_enum", "missing_field", "missing_field", "invalid_period"])
        )

        # 容量口径 @2026-06-01(事实版本 cf1+m1.1)
        rows = {
            (r["standard"], r["unit"]): r
            for r in capacity_by_standard(self.store, "2026-06-01", "cf1+m1.1")
        }
        nr = rows[("NR", "MBPS")]
        # 独享:A-01(v2) 15000 + B-03 8000 + C-02 400 + D-01 50
        self.assertEqual(nr["dedicated_down"], 23450.0)
        # 共享:R-9 40000 + R-LATE 800,各计一次
        self.assertEqual(nr["shared_down"], 40800.0)
        self.assertEqual(nr["available_down"], 64250.0)
        # 朴素口径(共享按成员重复计入):MBPS 下行 104380,恰好多计一次 R-9 的 40000
        self.assertEqual(
            naive_double_counted_down(self.store, "2026-06-01", "cf1+m1.1"), 104380.0
        )
        # LTE:A-03(v1,上行未知)+ C-01
        lte = rows[("LTE", "MBPS")]
        self.assertEqual(lte["dedicated_down"], 130.0)
        self.assertEqual(lte["up_missing_count"], 1)
        # GSM:ERLANG 独立分桶,不与 MBPS 合并
        self.assertEqual(rows[("GSM", "ERLANG")]["dedicated_down"], 100.0)
        # MULTI:未携带制式的共享资源 R-10
        self.assertEqual(rows[("MULTI", "MBPS")]["shared_down"], 500.0)

        # 旧快照不受后续处理影响
        report_s1 = snapshot_report(self.store, s1)
        self.assertEqual(len(report_s1["asset_facts"]), 6)
        self.assertEqual(report_s1["meta"]["fact_version"], "cf1+m1.0")


if __name__ == "__main__":
    unittest.main()
