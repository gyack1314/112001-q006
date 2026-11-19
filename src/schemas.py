"""报文 Schema 注册表:显式版本、结构要求与能力标记。

校验分两层:

1. 结构层(本模块):版本、报文类型、必填字段、容量形状 —— 由 ``SchemaDef`` 描述;
2. 语义层(``src/mappings.py``):枚举取值、单位换算、容量口径 —— 由映射版本定义。

这样"映射调整"是显式的版本变更(新增映射版本),而不是隐式代码修改;
旧映射版本保留,期间快照可以精确复现当时口径。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SchemaDef:
    """一个显式登记的报文 Schema 版本。"""

    version: str  # 归一化后的版本号,如 "1.0"
    msg_types: tuple[str, ...]  # 支持的报文类型
    required_envelope: tuple[str, ...]  # 信封必填字段(不含 schema_version,单独校验)
    required_asset_fields: tuple[str, ...]  # asset_report 必填字段
    required_resource_fields: tuple[str, ...]  # shared_resource_report 必填字段
    documented_standards: tuple[str, ...]  # 厂商文档中的制式枚举(语义校验以映射表为准)
    documented_units: tuple[str, ...]  # 厂商文档中的单位枚举
    capacity_shape: str  # "scalar":v1 单值(仅下行) | "split":v2 上下行拆分
    supports_shared_relation: bool  # 资产报文是否可携带 shared_resource
    supports_resource_report: bool  # 是否支持 shared_resource_report
    notes: str


SCHEMA_V1 = SchemaDef(
    version="1.0",
    msg_types=("asset_report",),
    required_envelope=("msg_type", "report_id", "reported_at", "valid_from"),
    required_asset_fields=("vendor", "asset", "standard", "capacity", "unit"),
    required_resource_fields=(),
    documented_standards=("GSM", "UMTS", "LTE", "NR"),
    documented_units=("ERLANG", "MBPS", "Gbps", "Mbps"),
    capacity_shape="scalar",
    supports_shared_relation=False,
    supports_resource_report=False,
    notes=(
        "旧版报文:capacity 为单一数值,旧口径仅统计下行容量;无上行字段、无共享资源概念;"
        "制式枚举按技术命名(GSM/UMTS/LTE/NR)。"
    ),
)

SCHEMA_V2 = SchemaDef(
    version="2.0",
    msg_types=("asset_report", "shared_resource_report"),
    required_envelope=("msg_type", "report_id", "reported_at", "valid_from"),
    required_asset_fields=("vendor", "asset", "standard", "capacity", "unit"),
    required_resource_fields=("resource_id", "owner_scope", "capacity", "unit"),
    documented_standards=("2G", "3G", "4G", "5G"),
    documented_units=("MBPS", "GBPS", "Mbps", "Gbps"),
    capacity_shape="split",
    supports_shared_relation=True,
    supports_resource_report=True,
    notes=(
        "新版报文:capacity 拆分为 {down, up};制式枚举按代际命名(2G/3G/4G/5G,含义不同于 v1);"
        "资产可携带 shared_resource 成员关系;共享资源由 shared_resource_report 独立上报,"
        "归属口径为 owner_scope。"
    ),
)

SCHEMA_REGISTRY: dict[str, SchemaDef] = {s.version: s for s in (SCHEMA_V1, SCHEMA_V2)}

# 厂商报文中出现的版本写法归一化为注册表版本号。
VERSION_ALIASES: dict[str, str] = {
    "1": "1.0",
    "1.0": "1.0",
    "2": "2.0",
    "2.0": "2.0",
}


def normalize_declared_version(raw: object) -> str | None:
    """把报文声明的 schema_version 归一化为注册表版本号;无法识别返回 None。"""
    if raw is None:
        return None
    return VERSION_ALIASES.get(str(raw).strip())


def detect_version(payload: dict) -> str | None:
    """按结构嗅探报文版本(与声明版本交叉校验用)。

    v2 结构标记:capacity 为对象(拆分)、携带 shared_resource、或资源上报类型;
    v1 结构标记:capacity 为数值。两者皆无则无法嗅探。
    """
    if payload.get("msg_type") == "shared_resource_report":
        return "2.0"
    capacity = payload.get("capacity")
    if isinstance(capacity, dict):
        return "2.0"
    if isinstance(capacity, bool):
        return None
    if isinstance(capacity, (int, float)):
        return "1.0"
    if payload.get("shared_resource") is not None:
        return "2.0"
    return None
