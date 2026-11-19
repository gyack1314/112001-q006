"""字段级溯源与报文去向:规范字段来自哪版报文、如何转换、哪些记录未进入下游。"""
import unittest

from helpers import v1_asset, v2_asset

from src.ingest import ingest_payload
from src.normalize import process_all, process_raw
from src.queries import asset_timeline, field_lineage, quarantine_report, record_provenance
from src.store import Store


class LineageTest(unittest.TestCase):
    def setUp(self):
        self.store = Store()

    def tearDown(self):
        self.store.close()

    def test_standard_field_lineage_shows_source_version_and_rule(self):
        raw_id = ingest_payload(self.store, v2_asset(asset="A", standard="5G")).raw_id
        process_raw(self.store, raw_id, "m1.0")
        [entry] = field_lineage(self.store, "A", field="standard")
        self.assertEqual(entry["raw_schema_version"], "2")  # 来自 v2 报文
        self.assertEqual(entry["raw_value"], "5G")  # 原始取值
        self.assertEqual(entry["canonical_value"], "NR")  # 规范取值
        self.assertEqual(entry["rule_id"], "std.map.v2")
        self.assertIn("代际", entry["rule_note"])

    def test_v1_capacity_up_lineage_records_absent_source(self):
        raw_id = ingest_payload(self.store, v1_asset(asset="V1A")).raw_id
        process_raw(self.store, raw_id, "m1.0")
        [entry] = field_lineage(self.store, "V1A", field="capacity_up")
        self.assertIsNone(entry["canonical_value"])  # 未知,非 0
        self.assertIsNone(entry["raw_value"])  # 源报文无此字段
        self.assertEqual(entry["rule_id"], "cap.v1.down_only")

    def test_unit_conversion_lineage(self):
        raw_id = ingest_payload(self.store, v1_asset(asset="U", capacity=20, unit="Gbps")).raw_id
        process_raw(self.store, raw_id, "m1.0")
        [entry] = field_lineage(self.store, "U", field="capacity_unit")
        self.assertEqual(entry["raw_value"], "Gbps")
        self.assertEqual(entry["canonical_value"], "MBPS")
        self.assertEqual(entry["rule_id"], "unit.normalize")


class ProvenanceTest(unittest.TestCase):
    def setUp(self):
        self.store = Store()

    def tearDown(self):
        self.store.close()

    def test_record_provenance_tracks_outcomes_across_mappings(self):
        raw_id = ingest_payload(self.store, v2_asset(asset="X", standard="5G-ADVANCED")).raw_id
        process_raw(self.store, raw_id, "m1.0")  # 隔离
        process_raw(self.store, raw_id, "m1.1")  # 入账
        prov = record_provenance(self.store, raw_id)
        outcomes = {(p["mapping_version"], p["outcome"]) for p in prov["processing"]}
        self.assertEqual(outcomes, {("m1.0", "quarantined"), ("m1.1", "fact")})
        self.assertEqual(len(prov["facts"]), 1)
        self.assertEqual(prov["quarantines"][0]["status"], "resolved")

    def test_quarantine_report_keeps_records_diagnosable(self):
        raw_id = ingest_payload(self.store, v1_asset(asset="BAD", standard="NB-IoT")).raw_id
        process_raw(self.store, raw_id, "m1.0")
        [q] = quarantine_report(self.store)
        self.assertEqual(q["raw_id"], raw_id)
        self.assertEqual(q["code"], "unknown_enum")
        self.assertIn("NB-IoT", q["detail"])
        # 原始报文仍可取回
        self.assertEqual(record_provenance(self.store, raw_id)["raw"]["raw_id"], raw_id)

    def test_asset_timeline_includes_superseded(self):
        ingest_payload(self.store, v1_asset(asset="T", capacity=10))
        process_all(self.store, "m1.0")
        ingest_payload(self.store, v1_asset(asset="T", capacity=12, report_id="fix"))
        process_all(self.store, "m1.0")
        timeline = asset_timeline(self.store, "T")
        self.assertEqual(len(timeline), 2)
        self.assertEqual(len([f for f in timeline if f["superseded_by"]]), 1)


if __name__ == "__main__":
    unittest.main()
