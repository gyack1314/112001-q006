"""查询接口:按资产、制式、有效期间查询规范事实,以及溯源与诊断。"""
from __future__ import annotations

from .store import Store


def list_assets(
    store: Store,
    standard: str | None = None,
    as_of: str | None = None,
    fact_version: str | None = None,
) -> list[dict]:
    """按制式 / 生效日期 / 事实版本过滤的规范资产事实。"""
    return store.get_facts(fact_version=fact_version, standard=standard, as_of=as_of)


def asset_timeline(store: Store, asset_id: str) -> list[dict]:
    """资产全时间线:所有事实版本、含被取代事实(superseded_by 标记)。"""
    return store.get_facts(asset_id=asset_id, include_superseded=True)


def field_lineage(
    store: Store,
    asset_id: str,
    field: str | None = None,
    fact_version: str | None = None,
) -> list[dict]:
    """字段级溯源:规范字段来自哪条原始报文(含其 schema 版本)、原始取值、转换规则。"""
    facts = store.get_facts(asset_id=asset_id, fact_version=fact_version, include_superseded=True)
    rows: list[dict] = []
    for fact in facts:
        for entry in store.lineage_for(fact["fact_id"], fact["fact_version"]):
            if field and entry["field"] != field:
                continue
            raw = store.get_raw(entry["raw_id"]) or {}
            rows.append(
                {
                    "asset_id": asset_id,
                    "fact_id": fact["fact_id"],
                    "fact_version": fact["fact_version"],
                    "canonical_value": fact.get(entry["field"]),
                    "field": entry["field"],
                    "raw_id": entry["raw_id"],
                    "raw_schema_version": raw.get("declared_version"),
                    "raw_path": entry["raw_path"],
                    "raw_value": entry["raw_value"],
                    "rule_id": entry["rule_id"],
                    "rule_note": entry["rule_note"],
                }
            )
    return rows


def record_provenance(store: Store, raw_id: str) -> dict:
    """单条原始报文的去向:处理结果、产生的事实、隔离记录(跨映射版本)。"""
    raw = store.get_raw(raw_id)
    if raw is None:
        raise KeyError(f"原始报文不存在: {raw_id}")
    facts = [
        f
        for f in store.get_facts(include_superseded=True)
        if f["raw_id"] == raw_id
    ]
    shared = [
        s
        for s in store.get_shared_facts(include_superseded=True)
        if s["raw_id"] == raw_id
    ]
    return {
        "raw": raw,
        "processing": store.processing_entries(raw_id),
        "facts": facts,
        "shared_facts": shared,
        "quarantines": store.quarantines(raw_id=raw_id),
    }


def quarantine_report(store: Store, status: str | None = "open") -> list[dict]:
    """隔离区报告:因兼容问题未进入下游的记录及诊断事由。"""
    rows = store.quarantines(status=status)
    for row in rows:
        raw = store.get_raw(row["raw_id"])
        row["source_note"] = (raw or {}).get("source_note")
        row["declared_version"] = (raw or {}).get("declared_version")
    return rows
