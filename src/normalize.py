"""规范化引擎:原始报文 → 规范事实(按显式映射版本)。

兼容原则:

- 旧、新 Schema 在明确版本下解释:声明版本缺失/未知/与结构嗅探冲突 → 隔离;
- 缺字段、未知枚举、关系不完整 → 记录进入隔离区,保留原始报文与诊断事由,
  绝不填默认值伪装成有效资产;
- 每个规范字段写入 lineage:来自哪条原始报文(含其 schema 版本)、原始取值、
  转换规则与规则说明;
- 共享资源关系必须完整(引用的资源已在同一事实版本下登记且有效期重叠),
  否则资产报文隔离,待补传到达后重处理;
- 重跑全量按新事实版本落库,旧事实版本保留,期间快照不受影响。

诊断码(stage):
- version_missing / version_unknown / version_mismatch(version 阶段)
- msg_type_unknown / missing_field / invalid_period(schema 阶段)
- unknown_enum / invalid_type / inconsistent_capacity / schema_violation(normalize 阶段)
- incomplete_relation(normalize 阶段:共享资源引用悬空)
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date

from .mappings import (
    CANONICAL_STANDARD_MULTI,
    MAPPING_VERSIONS,
    RULE_NOTES,
    STANDARD_MAPS,
    UNIT_CONVERSIONS,
    fact_version_for,
)
from .schemas import SCHEMA_REGISTRY, detect_version, normalize_declared_version
from .store import Store


class Quarantine(Exception):
    """规范化过程中的可诊断失败:记录进入隔离区,不进入下游。"""

    def __init__(self, code: str, detail: str, stage: str = "normalize"):
        super().__init__(f"[{stage}/{code}] {detail}")
        self.code = code
        self.detail = detail
        self.stage = stage


@dataclass
class NormResult:
    raw_id: str
    outcome: str  # fact | shared | quarantined | cached
    ref_id: str | None = None  # fact_id / sf_id
    code: str | None = None  # 隔离诊断码
    detail: str | None = None


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _num(value: object, path: str) -> float:
    """数值校验:拒绝布尔、字符串等非数值类型(不静默转换)。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise Quarantine("invalid_type", f"{path} 应为数值,实际为 {type(value).__name__}: {value!r}")
    return float(value)


def _parse_period(payload: dict) -> tuple[str, str | None]:
    raw_from = payload.get("valid_from")
    try:
        valid_from = date.fromisoformat(str(raw_from))
    except ValueError:
        raise Quarantine("invalid_period", f"valid_from={raw_from!r} 不是 ISO 日期", stage="schema")
    raw_to = payload.get("valid_to")
    valid_to: date | None = None
    if raw_to is not None:
        try:
            valid_to = date.fromisoformat(str(raw_to))
        except ValueError:
            raise Quarantine("invalid_period", f"valid_to={raw_to!r} 不是 ISO 日期", stage="schema")
    if valid_to is not None and valid_to < valid_from:
        raise Quarantine(
            "invalid_period",
            f"valid_to({valid_to}) 早于 valid_from({valid_from})",
            stage="schema",
        )
    return valid_from.isoformat(), (valid_to.isoformat() if valid_to else None)


def periods_overlap(a_from: str, a_to: str | None, b_from: str, b_to: str | None) -> bool:
    a_end = a_to or "9999-12-31"
    b_end = b_to or "9999-12-31"
    return a_from <= b_end and b_from <= a_end


def _convert_unit(raw_unit: object) -> tuple[str, float]:
    """单位归一。返回 (规范单位, 倍率)。未知单位 → 隔离(不默认换算)。"""
    key = str(raw_unit).strip().upper()
    if key not in UNIT_CONVERSIONS:
        raise Quarantine("unknown_enum", f"unit={raw_unit!r} 无换算规则")
    return UNIT_CONVERSIONS[key]


def _map_standard(raw_std: object, schema_version: str, mapping_version: str) -> str:
    if not isinstance(raw_std, str):
        raise Quarantine(
            "invalid_type", f"standard 应为字符串,实际为 {type(raw_std).__name__}: {raw_std!r}"
        )
    std_map = STANDARD_MAPS[(schema_version, mapping_version)]
    if raw_std not in std_map:
        raise Quarantine(
            "unknown_enum",
            f"standard={raw_std!r} 不在映射版本 {mapping_version} 的枚举表中",
        )
    return std_map[raw_std]


# ---------------------------------------------------------------------------
# 资产报文
# ---------------------------------------------------------------------------

def _normalize_asset(
    store: Store,
    payload: dict,
    schema,
    mapping_version: str,
    raw_id: str,
    valid_from: str,
    valid_to: str | None,
) -> str:
    fv = fact_version_for(mapping_version)

    missing = [f for f in schema.required_asset_fields if payload.get(f) is None]
    if missing:
        raise Quarantine("missing_field", f"缺少必填字段: {', '.join(missing)}", stage="schema")

    # 制式枚举:按映射版本表转换,未知枚举隔离(不默认归类)。
    raw_std = payload["standard"]
    standard = _map_standard(raw_std, schema.version, mapping_version)
    std_rule = "std.map.v1" if schema.version == "1.0" else "std.map.v2"

    # 单位归一(大小写不敏感;GBPS→MBPS ×1000;ERLANG 原样)。
    unit, factor = _convert_unit(payload["unit"])

    # 容量口径:按 Schema 版本的容量形状解释。
    if schema.capacity_shape == "scalar":
        # v1:单一 capacity = 下行容量(旧口径);上行未上报 → null(未知,非 0)。
        down = _num(payload["capacity"], "capacity") * factor
        up: float | None = None
        cap_rule = "cap.v1.down_only"
    else:
        capacity = payload["capacity"]
        if not isinstance(capacity, dict):
            raise Quarantine("invalid_type", "v2 capacity 应为对象 {down, up}")
        for part in ("down", "up"):
            if capacity.get(part) is None:
                raise Quarantine("missing_field", f"capacity 缺少 {part} 分量", stage="schema")
        down = _num(capacity["down"], "capacity.down") * factor
        up = _num(capacity["up"], "capacity.up") * factor
        cap_rule = "cap.v2.split"

    if down < 0 or (up is not None and up < 0):
        raise Quarantine("inconsistent_capacity", f"容量为负: down={down}, up={up}")

    # 共享资源关系:必须完整,否则隔离(待资源报文补传后重处理)。
    shared_ref = payload.get("shared_resource")
    if shared_ref is not None:
        if not schema.supports_shared_relation:
            raise Quarantine(
                "schema_violation",
                f"schema {schema.version} 不支持 shared_resource 字段",
            )
        if not isinstance(shared_ref, str) or not shared_ref.strip():
            raise Quarantine("invalid_type", f"shared_resource 应为非空字符串: {shared_ref!r}")
        resources = [
            s
            for s in store.get_shared_facts(fact_version=fv, resource_id=shared_ref)
            if periods_overlap(s["valid_from"], s["valid_to"], valid_from, valid_to)
        ]
        if not resources:
            raise Quarantine(
                "incomplete_relation",
                f"共享资源 {shared_ref!r} 未在同一事实版本({fv})下登记,或有效期不重叠",
            )

    fact_id = "F" + _sha(
        "|".join(
            [
                payload["asset"],
                standard,
                repr(down),
                repr(up),
                unit,
                valid_from,
                valid_to or "",
                shared_ref or "",
            ]
        )
    )[:14]

    fact = {
        "fact_id": fact_id,
        "fact_version": fv,
        "mapping_version": mapping_version,
        "asset_id": payload["asset"],
        "vendor": payload["vendor"],
        "standard": standard,
        "capacity_down": down,
        "capacity_up": up,
        "capacity_unit": unit,
        "shared_ref": shared_ref,
        "valid_from": valid_from,
        "valid_to": valid_to,
        "raw_id": raw_id,
        "warnings": [],
    }
    store.put_fact(fact)

    lineage = [
        ("asset_id", "asset", payload["asset"], "id.passthrough"),
        ("vendor", "vendor", payload["vendor"], "id.passthrough"),
        ("standard", "standard", raw_std, std_rule),
        ("capacity_down", "capacity" if schema.capacity_shape == "scalar" else "capacity.down",
         payload["capacity"] if schema.capacity_shape == "scalar" else payload["capacity"]["down"],
         cap_rule),
        (
            "capacity_up",
            "capacity.up" if schema.capacity_shape == "split" else "(源报文无此字段)",
            payload["capacity"]["up"] if schema.capacity_shape == "split" else None,
            cap_rule,
        ),
        ("capacity_unit", "unit", payload["unit"], "unit.normalize"),
        ("valid_from", "valid_from", valid_from, "period.passthrough"),
        ("valid_to", "valid_to", valid_to, "period.passthrough"),
    ]
    if shared_ref is not None:
        lineage.append(("shared_ref", "shared_resource", shared_ref, "shared.v2.relation"))
    store.put_lineage(
        [
            {
                "fact_id": fact_id,
                "fact_version": fv,
                "field": field,
                "raw_id": raw_id,
                "raw_path": path,
                "raw_value": value,
                "rule_id": rule,
                "rule_note": RULE_NOTES[rule],
            }
            for field, path, value, rule in lineage
        ]
    )
    return fact_id


# ---------------------------------------------------------------------------
# 共享资源报文
# ---------------------------------------------------------------------------

def _normalize_shared_resource(
    store: Store,
    payload: dict,
    schema,
    mapping_version: str,
    raw_id: str,
    valid_from: str,
    valid_to: str | None,
) -> str:
    fv = fact_version_for(mapping_version)

    missing = [f for f in schema.required_resource_fields if payload.get(f) is None]
    if missing:
        raise Quarantine("missing_field", f"缺少必填字段: {', '.join(missing)}", stage="schema")

    owner_scope = payload["owner_scope"]
    if not isinstance(owner_scope, dict):
        raise Quarantine("invalid_type", "owner_scope 应为对象 {scope_type, scope_id}")
    scope_type = owner_scope.get("scope_type")
    scope_id = owner_scope.get("scope_id")
    if scope_type not in ("site", "asset"):
        raise Quarantine("unknown_enum", f"owner_scope.scope_type={scope_type!r} 未知(支持 site/asset)")
    if not scope_id:
        raise Quarantine("missing_field", "owner_scope 缺少 scope_id", stage="schema")

    # 制式可选:未携带 → 显式规范标记 MULTI(跨制式),非默认值填充。
    raw_std = payload.get("standard")
    if raw_std is None:
        standard = CANONICAL_STANDARD_MULTI
        std_rule = "std.shared.multi"
    else:
        standard = _map_standard(raw_std, schema.version, mapping_version)
        std_rule = "std.map.v2"

    unit, factor = _convert_unit(payload["unit"])

    capacity = payload["capacity"]
    if not isinstance(capacity, dict):
        raise Quarantine("invalid_type", "capacity 应为对象 {down, up}")
    for part in ("down", "up"):
        if capacity.get(part) is None:
            raise Quarantine("missing_field", f"capacity 缺少 {part} 分量", stage="schema")
    down = _num(capacity["down"], "capacity.down") * factor
    up = _num(capacity["up"], "capacity.up") * factor
    if down < 0 or up < 0:
        raise Quarantine("inconsistent_capacity", f"容量为负: down={down}, up={up}")

    sf_id = "S" + _sha(
        "|".join(
            [
                payload["resource_id"],
                scope_type,
                str(scope_id),
                standard,
                repr(down),
                repr(up),
                unit,
                valid_from,
                valid_to or "",
            ]
        )
    )[:14]

    store.put_shared_fact(
        {
            "sf_id": sf_id,
            "fact_version": fv,
            "mapping_version": mapping_version,
            "resource_id": payload["resource_id"],
            "owner_scope_type": scope_type,
            "owner_scope_id": str(scope_id),
            "standard": standard,
            "capacity_down": down,
            "capacity_up": up,
            "capacity_unit": unit,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "raw_id": raw_id,
        }
    )
    store.put_lineage(
        [
            {
                "fact_id": sf_id,
                "fact_version": fv,
                "field": field,
                "raw_id": raw_id,
                "raw_path": path,
                "raw_value": value,
                "rule_id": rule,
                "rule_note": RULE_NOTES[rule],
            }
            for field, path, value, rule in [
                ("resource_id", "resource_id", payload["resource_id"], "id.passthrough"),
                ("owner_scope", "owner_scope", json.dumps(owner_scope, ensure_ascii=False), "shared.v2.resource"),
                ("standard", "standard", raw_std, std_rule),
                ("capacity_down", "capacity.down", capacity["down"], "cap.v2.split"),
                ("capacity_up", "capacity.up", capacity["up"], "cap.v2.split"),
                ("capacity_unit", "unit", payload["unit"], "unit.normalize"),
                ("valid_from", "valid_from", valid_from, "period.passthrough"),
                ("valid_to", "valid_to", valid_to, "period.passthrough"),
            ]
        ]
    )
    return sf_id


# ---------------------------------------------------------------------------
# 处理入口
# ---------------------------------------------------------------------------

def _dispatch(store: Store, payload: dict, raw_id: str, mapping_version: str) -> tuple[str, str]:
    declared_raw = payload.get("schema_version")
    if declared_raw is None:
        raise Quarantine(
            "version_missing", "报文缺少 schema_version,无法在明确版本下解释", stage="version"
        )
    declared = normalize_declared_version(declared_raw)
    if declared is None:
        raise Quarantine(
            "version_unknown",
            f"schema_version={declared_raw!r} 不在注册表 {sorted(SCHEMA_REGISTRY)} 中",
            stage="version",
        )
    schema = SCHEMA_REGISTRY[declared]

    detected = detect_version(payload)
    if detected is not None and detected != declared:
        raise Quarantine(
            "version_mismatch",
            f"声明版本 {declared} 与结构嗅探 {detected} 不一致",
            stage="version",
        )

    msg_type = payload.get("msg_type")
    if msg_type not in schema.msg_types:
        raise Quarantine(
            "msg_type_unknown",
            f"msg_type={msg_type!r} 不被 schema {declared} 支持(支持: {', '.join(schema.msg_types)})",
            stage="schema",
        )

    missing_env = [f for f in schema.required_envelope if payload.get(f) is None]
    if missing_env:
        raise Quarantine(
            "missing_field", f"缺少信封字段: {', '.join(missing_env)}", stage="schema"
        )

    valid_from, valid_to = _parse_period(payload)

    if msg_type == "asset_report":
        return "fact", _normalize_asset(
            store, payload, schema, mapping_version, raw_id, valid_from, valid_to
        )
    return "shared", _normalize_shared_resource(
        store, payload, schema, mapping_version, raw_id, valid_from, valid_to
    )


def process_raw(
    store: Store, raw_id: str, mapping_version: str, force: bool = False
) -> NormResult:
    """按指定映射版本规范化一条原始报文(幂等;force 强制重处理)。"""
    if mapping_version not in MAPPING_VERSIONS:
        raise ValueError(f"未知映射版本: {mapping_version}(已登记: {sorted(MAPPING_VERSIONS)})")
    raw = store.get_raw(raw_id)
    if raw is None:
        raise KeyError(f"原始报文不存在: {raw_id}")

    prior = store.processed_entry(raw_id, mapping_version)
    if prior is not None and not force:
        return NormResult(raw_id, "cached", ref_id=prior["ref_id"])

    payload = json.loads(raw["payload_text"])
    try:
        kind, ref_id = _dispatch(store, payload, raw_id, mapping_version)
    except Quarantine as q:
        quar_id = store.add_quarantine(raw_id, mapping_version, q.stage, q.code, q.detail)
        store.log_processed(raw_id, mapping_version, "quarantined", f"Q{quar_id}")
        return NormResult(raw_id, "quarantined", code=q.code, detail=q.detail)

    store.log_processed(raw_id, mapping_version, kind, ref_id)
    # 报文成功进入下游:解除其在任意映射版本下的挂起隔离记录。
    store.resolve_quarantines_for_raw(raw_id, resolved_by=ref_id, mapping_version=mapping_version)
    return NormResult(raw_id, kind, ref_id=ref_id)


def process_all(store: Store, mapping_version: str) -> list[NormResult]:
    """全量处理:跳过该映射版本下已处理的报文(幂等重跑)。

    两遍处理:先 shared_resource_report(建立共享资源登记),再 asset_report
    (成员关系引用方能解析);跨批次到达的资源上报仍走隔离 + 重处理。
    """
    raws = store.iter_raws()

    def is_resource_report(raw: dict) -> bool:
        try:
            return json.loads(raw["payload_text"]).get("msg_type") == "shared_resource_report"
        except (ValueError, AttributeError):
            return False

    ordered = sorted(raws, key=lambda r: (0 if is_resource_report(r) else 1, r["first_seen"]))
    return [process_raw(store, raw["raw_id"], mapping_version) for raw in ordered]


def reprocess_quarantined(store: Store, mapping_version: str) -> list[NormResult]:
    """重处理所有挂起隔离的报文(补传到达或映射调整后调用)。"""
    raw_ids = sorted({q["raw_id"] for q in store.quarantines(status="open")})
    return [process_raw(store, rid, mapping_version, force=True) for rid in raw_ids]
