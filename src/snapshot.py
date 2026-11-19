"""期间快照:物化某有效期区间内有效的规范事实,并钉住当时的映射版本与事实版本。

快照生成后不可变:即使之后映射调整、全量重跑产生新事实版本,旧快照仍引用
当时的 (fact_id, fact_version),可精确复现当时口径,并支持新旧对比。
"""
from __future__ import annotations

import hashlib

from .aggregate import aggregate_capacity
from .mappings import fact_version_for
from .normalize import periods_overlap
from .store import Store


def create_snapshot(
    store: Store,
    period_start: str,
    period_end: str,
    mapping_version: str,
    note: str = "",
) -> str:
    """生成期间快照,返回 snapshot_id(内容寻址,相同内容幂等)。"""
    fv = fact_version_for(mapping_version)
    facts = [
        f
        for f in store.get_facts(fact_version=fv)
        if periods_overlap(f["valid_from"], f["valid_to"], period_start, period_end)
    ]
    shared = [
        s
        for s in store.get_shared_facts(fact_version=fv)
        if periods_overlap(s["valid_from"], s["valid_to"], period_start, period_end)
    ]

    members = [("asset", f["fact_id"], fv) for f in facts] + [
        ("shared", s["sf_id"], fv) for s in shared
    ]
    digest = hashlib.sha256(
        "|".join(
            [period_start, period_end, mapping_version]
            + sorted(f"{k}:{i}" for k, i, _ in members)
        ).encode("utf-8")
    ).hexdigest()
    snapshot_id = "SN" + digest[:12]

    store.put_snapshot(
        {
            "snapshot_id": snapshot_id,
            "period_start": period_start,
            "period_end": period_end,
            "mapping_version": mapping_version,
            "fact_version": fv,
            "note": note,
        },
        members,
    )
    return snapshot_id


def snapshot_report(store: Store, snapshot_id: str) -> dict:
    """快照完整报告:元信息(映射/事实版本)、物化事实、按制式聚合。"""
    meta = store.get_snapshot(snapshot_id)
    if meta is None:
        raise KeyError(f"快照不存在: {snapshot_id}")
    members = store.snapshot_members(snapshot_id)
    asset_refs = [(m["fact_id"], m["fact_version"]) for m in members if m["kind"] == "asset"]
    shared_refs = [(m["fact_id"], m["fact_version"]) for m in members if m["kind"] == "shared"]
    facts = store.facts_by_ids(asset_refs, "facts")
    shared = store.facts_by_ids(shared_refs, "shared_facts")
    return {
        "meta": meta,
        "asset_facts": facts,
        "shared_facts": shared,
        "capacity": aggregate_capacity(facts, shared),
        "open_quarantine_under_mapping": store.quarantines(
            status="open", mapping_version=meta["mapping_version"]
        ),
    }
