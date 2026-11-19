"""容量聚合:共享资源按归属口径计一次,成员资产不重复计入。

口径定义:

- 资产可用容量 = 其独享容量(capacity_down/up);共享容量不在资产层重复计入;
- 共享资源容量计入其归属口径(owner_scope),每个资源在同一口径下计一次;
- 全局/制式合计 = Σ 资产独享 + Σ 共享资源(每资源一次);
- 上行合计只统计实际上报了上行的资产(v1 资产上行为 null,不以 0 计入),
  并通过 up_missing_count 暴露覆盖缺口;
- 聚合按 (制式, 单位) 分桶,不跨单位合并(ERLANG 与 MBPS 不可换算)。
"""
from __future__ import annotations

from .store import Store


def aggregate_capacity(facts: list[dict], shared: list[dict]) -> list[dict]:
    """纯函数:规范事实 → 按 (制式, 单位) 分桶的容量合计。"""
    buckets: dict[tuple[str, str], dict] = {}

    def bucket(standard: str, unit: str) -> dict:
        return buckets.setdefault(
            (standard, unit),
            {
                "asset_count": 0,
                "dedicated_down": 0.0,
                "dedicated_up": 0.0,
                "up_reported_count": 0,
                "up_missing_count": 0,
                "shared_resource_count": 0,
                "shared_down": 0.0,
                "shared_up": 0.0,
            },
        )

    for f in facts:
        b = bucket(f["standard"], f["capacity_unit"])
        b["asset_count"] += 1
        b["dedicated_down"] += f["capacity_down"]
        if f["capacity_up"] is None:
            b["up_missing_count"] += 1  # v1 资产:上行未知,不以 0 计入
        else:
            b["dedicated_up"] += f["capacity_up"]
            b["up_reported_count"] += 1

    for s in shared:
        b = bucket(s["standard"], s["capacity_unit"])
        b["shared_resource_count"] += 1  # 每资源计一次,与成员数量无关
        b["shared_down"] += s["capacity_down"]
        b["shared_up"] += s["capacity_up"]

    rows = []
    for (standard, unit), b in sorted(buckets.items()):
        rows.append(
            {
                "standard": standard,
                "unit": unit,
                **b,
                "available_down": b["dedicated_down"] + b["shared_down"],
                "available_up": b["dedicated_up"] + b["shared_up"],
            }
        )
    return rows


def capacity_by_standard(store: Store, as_of: str, fact_version: str) -> list[dict]:
    """某生效日期、某事实版本下,按制式分桶的可用容量(正确口径)。"""
    facts = store.get_facts(fact_version=fact_version, as_of=as_of)
    shared = store.get_shared_facts(fact_version=fact_version, as_of=as_of)
    return aggregate_capacity(facts, shared)


def naive_double_counted_down(store: Store, as_of: str, fact_version: str, unit: str = "MBPS") -> float:
    """反例口径(仅用于对比演示):共享容量按成员关系重复计入后的下行总量。

    即"把共享资源容量随每个成员资产各计一次"的错误结果,用于与正确口径对照。
    与正确口径的差值恰好是被重复计入的共享容量。
    """
    facts = [
        f
        for f in store.get_facts(fact_version=fact_version, as_of=as_of)
        if f["capacity_unit"] == unit
    ]
    shared = {
        s["resource_id"]: s
        for s in store.get_shared_facts(fact_version=fact_version, as_of=as_of)
        if s["capacity_unit"] == unit
    }
    total = sum(f["capacity_down"] for f in facts)
    for f in facts:
        ref = f["shared_ref"]
        if ref and ref in shared:
            total += shared[ref]["capacity_down"]  # 错误:成员数 × 资源容量
    return total


def asset_view(store: Store, asset_id: str, as_of: str, fact_version: str) -> dict | None:
    """单资产视图:独享容量入账,共享资源仅追溯引用(不重复计入)。"""
    facts = store.get_facts(fact_version=fact_version, asset_id=asset_id, as_of=as_of)
    if not facts:
        return None
    fact = facts[0]
    traces = []
    if fact["shared_ref"]:
        for s in store.get_shared_facts(fact_version=fact_version, resource_id=fact["shared_ref"]):
            traces.append(
                {
                    "resource_id": s["resource_id"],
                    "owner_scope": f"{s['owner_scope_type']}:{s['owner_scope_id']}",
                    "capacity_down": s["capacity_down"],
                    "capacity_up": s["capacity_up"],
                    "unit": s["capacity_unit"],
                    "counting_note": "该共享容量计入其归属口径,不在本资产重复计入",
                }
            )
    return {
        "asset_id": asset_id,
        "as_of": as_of,
        "fact_version": fact_version,
        "fact": fact,
        "counted_capacity": {
            "down": fact["capacity_down"],
            "up": fact["capacity_up"],
            "unit": fact["capacity_unit"],
        },
        "shared_traces": traces,
    }


def scope_shared_capacity(
    store: Store, scope_type: str, scope_id: str, as_of: str, fact_version: str
) -> dict:
    """归属口径视图:某 scope 名下的共享资源合计(每资源一次)及其成员追溯。"""
    resources = store.get_shared_facts(
        fact_version=fact_version, as_of=as_of, owner_scope=(scope_type, scope_id)
    )
    return {
        "scope": f"{scope_type}:{scope_id}",
        "as_of": as_of,
        "fact_version": fact_version,
        "resources": [
            {
                **s,
                "members": [m["asset_id"] for m in store.members_of(s["resource_id"], fact_version)],
            }
            for s in resources
        ],
        "total_down": sum(s["capacity_down"] for s in resources),
        "total_up": sum(s["capacity_up"] for s in resources),
    }
