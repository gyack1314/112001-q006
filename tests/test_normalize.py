"""规范化引擎:版本校验、枚举映射、容量口径、隔离诊断。"""
import unittest

from helpers import v1_asset, v2_asset, v2_resource

from src.ingest import ingest_payload
from src.normalize import process_all, process_raw
from src.store import Store


class NormalizeTest(unittest.TestCase):
    def setUp(self):
        self.store = Store()

    def tearDown(self):
        self.store.close()

    def _ingest(self, payload):
        return ingest_payload(self.store, payload).raw_id

    def _process_one(self, payload, mapping="m1.0"):
        raw_id = self._ingest(payload)
        return process_raw(self.store, raw_id, mapping)

    # -- 正常路径 ------------------------------------------------------------

    def test_v1_scalar_capacity_maps_to_down_only(self):
        result = self._process_one(v1_asset(capacity=20, unit="Gbps"))
        self.assertEqual(result.outcome, "fact")
        [fact] = self.store.get_facts()
        self.assertEqual(fact["capacity_down"], 20000.0)  # Gbps → MBPS ×1000
        self.assertIsNone(fact["capacity_up"])  # v1 无上行字段:未知,非 0
        self.assertEqual(fact["capacity_unit"], "MBPS")
        self.assertEqual(fact["fact_version"], "cf1+m1.0")

    def test_v2_split_capacity(self):
        result = self._process_one(v2_asset(down=15, up=5, unit="Gbps"))
        self.assertEqual(result.outcome, "fact")
        [fact] = self.store.get_facts()
        self.assertEqual(fact["capacity_down"], 15000.0)
        self.assertEqual(fact["capacity_up"], 5000.0)

    def test_enum_mapping_v2_generation_to_technology(self):
        self._process_one(v2_asset(standard="5G"))
        [fact] = self.store.get_facts()
        self.assertEqual(fact["standard"], "NR")

    def test_declared_version_alias(self):
        # 厂商写法 "1"/"2" 归一化为注册版本 "1.0"/"2.0"
        result = self._process_one(v1_asset(schema_version="1"))
        self.assertEqual(result.outcome, "fact")

    def test_unit_case_insensitive(self):
        self._process_one(v1_asset(unit="gbps", capacity=1))
        [fact] = self.store.get_facts()
        self.assertEqual(fact["capacity_unit"], "MBPS")
        self.assertEqual(fact["capacity_down"], 1000.0)

    def test_erlang_unit_preserved(self):
        self._process_one(v1_asset(standard="GSM", capacity=100, unit="Erlang"))
        [fact] = self.store.get_facts()
        self.assertEqual(fact["capacity_unit"], "ERLANG")

    # -- 版本校验 --------------------------------------------------------------

    def test_version_missing(self):
        payload = v1_asset()
        del payload["schema_version"]
        result = self._process_one(payload)
        self.assertEqual(result.outcome, "quarantined")
        self.assertEqual(result.code, "version_missing")
        self.assertEqual(self.store.get_facts(), [])

    def test_version_unknown(self):
        result = self._process_one(v1_asset(schema_version="3"))
        self.assertEqual(result.code, "version_unknown")

    def test_version_mismatch(self):
        # 声明 v1,但 capacity 为 v2 拆分结构
        payload = v2_asset(schema_version="1")
        result = self._process_one(payload)
        self.assertEqual(result.code, "version_mismatch")

    # -- 隔离诊断:不填默认值 ----------------------------------------------------

    def test_unknown_enum_quarantined_not_defaulted(self):
        result = self._process_one(v1_asset(standard="NB-IoT"))
        self.assertEqual(result.outcome, "quarantined")
        self.assertEqual(result.code, "unknown_enum")
        # 关键断言:不产生任何事实(没有默认归类)
        self.assertEqual(self.store.get_facts(), [])
        [q] = self.store.quarantines(status="open")
        self.assertIn("NB-IoT", q["detail"])

    def test_unknown_unit_quarantined(self):
        result = self._process_one(v1_asset(unit="Tbps"))
        self.assertEqual(result.code, "unknown_enum")
        self.assertEqual(self.store.get_facts(), [])

    def test_missing_field(self):
        payload = v1_asset()
        del payload["capacity"]
        result = self._process_one(payload)
        self.assertEqual(result.code, "missing_field")
        self.assertEqual(self.store.get_facts(), [])

    def test_v2_missing_up_component(self):
        payload = v2_asset()
        del payload["capacity"]["up"]
        result = self._process_one(payload)
        self.assertEqual(result.code, "missing_field")

    def test_invalid_period_inverted(self):
        result = self._process_one(v1_asset(valid_from="2026-05-01", valid_to="2026-03-01"))
        self.assertEqual(result.code, "invalid_period")

    def test_invalid_period_unparseable(self):
        result = self._process_one(v1_asset(valid_from="2026/01/01"))
        self.assertEqual(result.code, "invalid_period")

    def test_negative_capacity(self):
        result = self._process_one(v1_asset(capacity=-5))
        self.assertEqual(result.code, "inconsistent_capacity")

    def test_non_numeric_capacity(self):
        result = self._process_one(v1_asset(capacity="20"))
        self.assertEqual(result.code, "invalid_type")

    def test_v1_with_shared_resource_is_schema_violation(self):
        result = self._process_one(v1_asset(shared_resource="R-1"))
        self.assertEqual(result.code, "schema_violation")

    def test_unknown_msg_type(self):
        result = self._process_one(v1_asset(msg_type="mystery_report"))
        self.assertEqual(result.code, "msg_type_unknown")

    # -- 幂等与更正 --------------------------------------------------------------

    def test_process_all_idempotent(self):
        self._ingest(v1_asset())
        first = process_all(self.store, "m1.0")
        second = process_all(self.store, "m1.0")
        self.assertEqual([r.outcome for r in first], ["fact"])
        self.assertEqual([r.outcome for r in second], ["cached"])
        self.assertEqual(len(self.store.get_facts()), 1)

    def test_correction_supersedes_previous_fact(self):
        self._process_one(v1_asset(capacity=10))
        # 同一资产、同一有效期起点的更正报文(容量修正为 12)
        self._process_one(v1_asset(capacity=12, report_id="rpt-A-1-fix"))
        current = self.store.get_facts()
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["capacity_down"], 12.0)
        # 旧事实保留可溯,标记 superseded_by
        history = self.store.get_facts(include_superseded=True)
        self.assertEqual(len(history), 2)
        superseded = [f for f in history if f["superseded_by"]]
        self.assertEqual(len(superseded), 1)
        self.assertEqual(superseded[0]["capacity_down"], 10.0)

    def test_backfill_identical_report_dedupes(self):
        p1 = v1_asset()
        p2 = v1_asset(report_id="rpt-backfill-dup", reported_at="2026-07-01T00:00:00Z")
        self._process_one(p1)
        self._process_one(p2)
        # 规范内容一致 → 同一 fact_id,不产生重复事实
        self.assertEqual(len(self.store.get_facts()), 1)
        self.assertEqual(len(self.store.iter_raws()), 2)  # 原始报文均保留


class SharedResourceNormalizeTest(unittest.TestCase):
    def setUp(self):
        self.store = Store()

    def tearDown(self):
        self.store.close()

    def test_resource_without_standard_marked_multi(self):
        raw_id = ingest_payload(self.store, v2_resource(standard=None)).raw_id
        result = process_raw(self.store, raw_id, "m1.0")
        self.assertEqual(result.outcome, "shared")
        [sf] = self.store.get_shared_facts()
        self.assertEqual(sf["standard"], "MULTI")

    def test_unknown_scope_type(self):
        raw_id = ingest_payload(self.store, v2_resource(scope_type="region")).raw_id
        result = process_raw(self.store, raw_id, "m1.0")
        self.assertEqual(result.code, "unknown_enum")

    def test_resource_missing_owner_scope(self):
        payload = v2_resource()
        del payload["owner_scope"]
        raw_id = ingest_payload(self.store, payload).raw_id
        result = process_raw(self.store, raw_id, "m1.0")
        self.assertEqual(result.code, "missing_field")


if __name__ == "__main__":
    unittest.main()
