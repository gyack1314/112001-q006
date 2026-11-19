"""映射注册表:原始 Schema → 规范模型(cf1)的显式版本化映射。

每个映射版本包含:

- 制式枚举映射表(纯数据,可 diff、可审计);
- 单位换算表(纯数据);
- 结构转换规则说明(写入字段级 lineage,解释"如何转换")。

映射调整 = 新增映射版本(如 m1.0 → m1.1 增补厂商扩展枚举);旧版本保留,
已生成的期间快照仍引用当时版本,新旧结果可对比。
"""
from __future__ import annotations

from dataclasses import dataclass

# 规范模型(事实)结构版本。字段增删或口径变化时演进(cf1 → cf2),
# 与映射版本正交:fact_version = "<规范结构版本>+<映射版本>"。
CANONICAL_SCHEMA_VERSION = "cf1"

# 规范制式枚举(技术命名,与 v1 对齐;v2 代际枚举经映射表转换)。
CANONICAL_STANDARDS = ("GSM", "UMTS", "LTE", "NR")

# 共享资源未携带制式时的显式规范标记(跨制式资源,如共享传输)。
# 这是有文档语义的规范取值,不是默认值填充:lineage 会记录该标记的来源规则。
CANONICAL_STANDARD_MULTI = "MULTI"


def fact_version_for(mapping_version: str) -> str:
    """事实版本 = 规范结构版本 + 映射版本,如 "cf1+m1.0"。"""
    return f"{CANONICAL_SCHEMA_VERSION}+{mapping_version}"


# ---------------------------------------------------------------------------
# 制式枚举映射表:(源 schema 版本, 映射版本) -> {原始枚举值: 规范枚举值}
# 不在表中的取值即"未知枚举",记录进入隔离区,绝不默认映射。
# ---------------------------------------------------------------------------
_STANDARD_MAP_V1 = {"GSM": "GSM", "UMTS": "UMTS", "LTE": "LTE", "NR": "NR"}

_STANDARD_MAP_V2_M10 = {"2G": "GSM", "3G": "UMTS", "4G": "LTE", "5G": "NR"}

# m1.1:厂商扩展枚举 '5G-ADVANCED' 确认按 NR 口径入账(映射调整示例)。
_STANDARD_MAP_V2_M11 = {**_STANDARD_MAP_V2_M10, "5G-ADVANCED": "NR"}

STANDARD_MAPS: dict[tuple[str, str], dict[str, str]] = {
    ("1.0", "m1.0"): _STANDARD_MAP_V1,
    ("2.0", "m1.0"): _STANDARD_MAP_V2_M10,
    ("1.0", "m1.1"): _STANDARD_MAP_V1,
    ("2.0", "m1.1"): _STANDARD_MAP_V2_M11,
}

# ---------------------------------------------------------------------------
# 单位换算表:原始单位(归一化为大写)-> (规范单位, 倍率)。
# ERLANG 为语音业务单位,与速率单位不可换算,原样保留;聚合按 (制式, 单位) 分桶,
# 不跨单位合并。
# ---------------------------------------------------------------------------
UNIT_CONVERSIONS: dict[str, tuple[str, float]] = {
    "MBPS": ("MBPS", 1.0),
    "GBPS": ("MBPS", 1000.0),
    "ERLANG": ("ERLANG", 1.0),
}


@dataclass(frozen=True)
class MappingVersion:
    version: str
    note: str


MAPPING_VERSIONS: dict[str, MappingVersion] = {
    "m1.0": MappingVersion(
        "m1.0",
        "初始映射:v1 制式枚举恒等;v2 代际枚举 2G/3G/4G/5G → GSM/UMTS/LTE/NR。",
    ),
    "m1.1": MappingVersion(
        "m1.1",
        "映射调整:v2 增补厂商扩展枚举 '5G-ADVANCED' → NR;其余同 m1.0。",
    ),
}

LATEST_MAPPING_VERSION = "m1.1"

# ---------------------------------------------------------------------------
# 结构转换规则说明。rule_id 写入字段级 lineage,解释每个规范字段"如何转换"。
# ---------------------------------------------------------------------------
RULE_NOTES: dict[str, str] = {
    "id.passthrough": "标识/厂商字段原样保留。",
    "std.map.v1": "v1 制式枚举按技术命名,恒等映射到规范制式。",
    "std.map.v2": "v2 制式枚举按代际命名,按映射版本表转换(如 '5G'→'NR')。",
    "std.shared.multi": (
        "共享资源报文未携带制式,按规则显式标记为 MULTI(跨制式资源);"
        "此为有文档语义的规范取值,非默认值填充。"
    ),
    "unit.normalize": "单位归一:GBPS→MBPS ×1000;MBPS/ERLANG 原样;原始单位大小写不敏感。",
    "cap.v1.down_only": (
        "v1 单一 capacity 字段按旧口径解释为下行容量;v1 无上行字段,"
        "规范 capacity_up 记为 null(未知,非 0),不参与上行合计。"
    ),
    "cap.v2.split": "v2 拆分容量 {down, up} 原样入账为下行/上行独享容量。",
    "period.passthrough": "有效期字段原样保留;valid_to 缺省表示开放区间。",
    "shared.v2.relation": (
        "v2 共享资源成员关系:共享容量按资源归属口径(owner_scope)计一次,"
        "成员资产仅保留追溯引用,不重复计入。"
    ),
    "shared.v2.resource": "v2 共享资源独立上报,归属口径 = owner_scope。",
}
