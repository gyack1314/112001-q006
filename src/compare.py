"""快照/映射版本对比:映射调整后比较新旧结果。"""
from __future__ import annotations

from .snapshot import create_snapshot, snapshot_report
from .store import Store

_FACT_FIELDS = (
    "vendor",
    "standard",
    "capacity_down",
    "capacity_up",
    "capacity_unit",
    "shared_ref",
    "valid_to",
)
_SHARED_FIELDS = (
    "owner_scope_type",
    "owner_scope_id",
    "standard",
    "capacity_down",
    "capacity_up",
    "capacity_unit",
    "valid_to",
)


def _diff_entities(old: list[dict], new: list[dict], key_fields: tuple[str, ...], cmp_fields: tuple[str, ...]) -> dict:
    def key(row: dict) -> tuple:
        return tuple(row[k] for k in key_fields)

    old_map = {key(r): r for r in old}
    new_map = {key(r): r for r in new}
    added = [new_map[k] for k in sorted(new_map.keys() - old_map.keys())]
    removed = [old_map[k] for k in sorted(old_map.keys() - new_map.keys())]
    changed = []
    for k in sorted(old_map.keys() & new_map.keys()):
        diffs = {
            f: {"old": old_map[k].get(f), "new": new_map[k].get(f)}
            for f in cmp_fields
            if old_map[k].get(f) != new_map[k].get(f)
        }
        if diffs:
            changed.append({"key": dict(zip(key_fields, k)), "fields": diffs})
    return {"added": added, "removed": removed, "changed": changed}


def compare_snapshots(store: Store, old_id: str, new_id: str) -> dict:
    """对比两个期间快照:事实增删改、容量口径差异、隔离区变化。"""
    old = snapshot_report(store, old_id)
    new = snapshot_report(store, new_id)

    asset_diff = _diff_entities(
        old["asset_facts"], new["asset_facts"], ("asset_id", "valid_from"), _FACT_FIELDS
    )
    shared_diff = _diff_entities(
        old["shared_facts"], new["shared_facts"], ("resource_id", "valid_from"), _SHARED_FIELDS
    )

    def capacity_index(rows: list[dict]) -> dict:
        return {(r["standard"], r["unit"]): r for r in rows}

    old_cap = capacity_index(old["capacity"])
    new_cap = capacity_index(new["capacity"])
    capacity_delta = []
    for bucket in sorted(set(old_cap) | set(new_cap)):
        o = old_cap.get(bucket, {})
        n = new_cap.get(bucket, {})
        capacity_delta.append(
            {
                "standard": bucket[0],
                "unit": bucket[1],
                "available_down": {"old": o.get("available_down", 0), "new": n.get("available_down", 0)},
                "available_up": {"old": o.get("available_up", 0), "new": n.get("available_up", 0)},
                "asset_count": {"old": o.get("asset_count", 0), "new": n.get("asset_count", 0)},
            }
        )

    def quarantine_counts(report: dict) -> dict[str, int]:
        counts: dict[str, int] = {}
        for q in report["open_quarantine_under_mapping"]:
            counts[q["code"]] = counts.get(q["code"], 0) + 1
        return counts

    return {
        "old": old["meta"],
        "new": new["meta"],
        "asset_diff": asset_diff,
        "shared_diff": shared_diff,
        "capacity_delta": capacity_delta,
        "open_quarantine": {
            "old": quarantine_counts(old),
            "new": quarantine_counts(new),
        },
    }


def compare_mapping_versions(
    store: Store,
    period_start: str,
    period_end: str,
    old_mapping: str,
    new_mapping: str,
) -> dict:
    """映射调整对比:同一期间分别按两个映射版本生成快照并比较。"""
    old_id = create_snapshot(store, period_start, period_end, old_mapping, note=f"对比基准 {old_mapping}")
    new_id = create_snapshot(store, period_start, period_end, new_mapping, note=f"映射调整 {new_mapping}")
    result = compare_snapshots(store, old_id, new_id)
    result["snapshot_ids"] = {"old": old_id, "new": new_id}
    return result
