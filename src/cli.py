"""命令行入口:接入、处理、查询、快照、对比,以及完整演练(demo)。

用法示例:
    python -m src demo                        # 内存库完整演练
    python -m src --db netres.db ingest data/messages.json
    python -m src --db netres.db process --mapping m1.0
    python -m src --db netres.db capacity --as-of 2026-06-01 --fact-version cf1+m1.0
    python -m src --db netres.db snapshot --start 2026-01-01 --end 2026-12-31 --mapping m1.0
    python -m src --db netres.db compare <SN_OLD> <SN_NEW>
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .aggregate import (
    asset_view,
    capacity_by_standard,
    naive_double_counted_down,
    scope_shared_capacity,
)
from .compare import compare_snapshots
from .ingest import ingest_file
from .mappings import LATEST_MAPPING_VERSION, MAPPING_VERSIONS, fact_version_for
from .normalize import process_all, reprocess_quarantined
from .queries import asset_timeline, field_lineage, quarantine_report, record_provenance
from .snapshot import create_snapshot, snapshot_report
from .store import Store

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def _print_json(obj: object) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _print_quarantine(store: Store, status: str | None = "open") -> None:
    rows = quarantine_report(store, status=status)
    if not rows:
        print(f"  隔离区({status}): 空")
        return
    print(f"  隔离区({status}): {len(rows)} 条")
    for q in rows:
        print(
            f"  - Q{q['quar_id']} [{q['stage']}/{q['code']}] raw={q['raw_id']} "
            f"映射={q['mapping_version']} 来源={q.get('source_note')}\n      {q['detail']}"
        )


def cmd_ingest(store: Store, args: argparse.Namespace) -> None:
    for path in args.paths:
        outcomes = ingest_file(store, path)
        new = sum(1 for o in outcomes if o.is_new)
        print(f"{path}: 接入 {len(outcomes)} 条(新 {new}, 补传去重 {len(outcomes) - new})")


def cmd_process(store: Store, args: argparse.Namespace) -> None:
    results = process_all(store, args.mapping)
    facts = sum(1 for r in results if r.outcome == "fact")
    shared = sum(1 for r in results if r.outcome == "shared")
    quar = sum(1 for r in results if r.outcome == "quarantined")
    cached = sum(1 for r in results if r.outcome == "cached")
    print(
        f"映射 {args.mapping} 处理完成: 资产事实 {facts}, 共享资源 {shared}, "
        f"隔离 {quar}, 已处理跳过 {cached}"
    )
    _print_quarantine(store)


def cmd_reprocess(store: Store, args: argparse.Namespace) -> None:
    results = reprocess_quarantined(store, args.mapping)
    resolved = [r for r in results if r.outcome in ("fact", "shared")]
    still = [r for r in results if r.outcome == "quarantined"]
    print(f"重处理 {len(results)} 条: 解除隔离 {len(resolved)}, 仍隔离 {len(still)}")
    for r in still:
        print(f"  - {r.raw_id}: [{r.code}] {r.detail}")


def cmd_facts(store: Store, args: argparse.Namespace) -> None:
    from .queries import list_assets

    _print_json(
        list_assets(
            store,
            standard=args.standard,
            as_of=args.as_of,
            fact_version=args.fact_version,
        )
    )


def cmd_quarantine(store: Store, args: argparse.Namespace) -> None:
    _print_quarantine(store, status=args.status)


def cmd_capacity(store: Store, args: argparse.Namespace) -> None:
    rows = capacity_by_standard(store, args.as_of, args.fact_version)
    print(f"可用容量 @ {args.as_of} (事实版本 {args.fact_version}, 共享按归属口径计一次):")
    for r in rows:
        print(
            f"  {r['standard']:>5} [{r['unit']}] 下行 独享={r['dedicated_down']:>10.1f} "
            f"共享={r['shared_down']:>10.1f} 可用={r['available_down']:>10.1f} | "
            f"上行 独享={r['dedicated_up']:>8.1f} 共享={r['shared_up']:>8.1f} "
            f"(上行未上报资产 {r['up_missing_count']}/{r['asset_count']})"
        )
    if args.naive:
        naive = naive_double_counted_down(store, args.as_of, args.fact_version, unit="MBPS")
        correct = sum(r["available_down"] for r in rows if r["unit"] == "MBPS")
        print(
            f"  [反例] 朴素口径(共享按成员重复计入)MBPS 下行合计: {naive:.1f};"
            f"正确口径: {correct:.1f};差值即被重复计入的共享容量"
        )


def cmd_asset(store: Store, args: argparse.Namespace) -> None:
    view = asset_view(store, args.asset_id, args.as_of, args.fact_version)
    if view is None:
        print(f"资产 {args.asset_id} 在 {args.as_of} 无有效事实({args.fact_version})")
        return
    _print_json(view)


def cmd_scope(store: Store, args: argparse.Namespace) -> None:
    _print_json(
        scope_shared_capacity(store, args.scope_type, args.scope_id, args.as_of, args.fact_version)
    )


def cmd_lineage(store: Store, args: argparse.Namespace) -> None:
    _print_json(field_lineage(store, args.asset_id, field=args.field, fact_version=args.fact_version))


def cmd_provenance(store: Store, args: argparse.Namespace) -> None:
    _print_json(record_provenance(store, args.raw_id))


def cmd_timeline(store: Store, args: argparse.Namespace) -> None:
    _print_json(asset_timeline(store, args.asset_id))


def cmd_snapshot(store: Store, args: argparse.Namespace) -> None:
    sid = create_snapshot(store, args.start, args.end, args.mapping, note=args.note)
    print(f"快照 {sid} 已生成(映射 {args.mapping}, 事实版本 {fact_version_for(args.mapping)})")
    report = snapshot_report(store, sid)
    print(f"  资产事实 {len(report['asset_facts'])} 条, 共享资源 {len(report['shared_facts'])} 条")


def cmd_snapshots(store: Store, args: argparse.Namespace) -> None:
    for s in store.list_snapshots():
        print(
            f"  {s['snapshot_id']}  期间 {s['period_start']}..{s['period_end']}  "
            f"映射 {s['mapping_version']}  事实版本 {s['fact_version']}  {s['note']}"
        )


def cmd_compare(store: Store, args: argparse.Namespace) -> None:
    _print_json(compare_snapshots(store, args.old, args.new))


def cmd_demo(store: Store, args: argparse.Namespace) -> None:
    """完整演练:接入 → 处理 → 快照 → 补传 → 映射调整 → 对比 → 溯源。"""
    as_of = "2026-06-01"
    print("=" * 72)
    print("网络资源目录接入演练(合成报文,仅用于本地演练,不代表真实厂商接口)")
    print("=" * 72)

    print("\n[1] 接入报文:基础混合批次 + 旧版补传批次 + 新版批次")
    for name in ("messages.json", "v1_backfill.json", "v2_batch.json"):
        outcomes = ingest_file(store, DATA_DIR / name)
        new = sum(1 for o in outcomes if o.is_new)
        print(f"  {name}: {len(outcomes)} 条(新 {new})")

    print("\n[2] 按映射 m1.0 规范化(旧、新 Schema 在明确版本下解释)")
    results = process_all(store, "m1.0")
    for r in results:
        if r.outcome == "quarantined":
            print(f"  隔离 {r.raw_id}: [{r.code}] {r.detail}")
    print(f"  资产事实 {sum(1 for r in results if r.outcome == 'fact')} 条, "
          f"共享资源 {sum(1 for r in results if r.outcome == 'shared')} 条")

    print("\n[3] 容量汇总:正确口径(共享按归属计一次) vs 朴素口径(按成员重复计入)")
    cmd_capacity(store, argparse.Namespace(as_of=as_of, fact_version="cf1+m1.0", naive=True))

    print("\n[4] 生成期间快照 S1(2026 全年, 映射 m1.0)")
    s1 = create_snapshot(store, "2026-01-01", "2026-12-31", "m1.0", note="映射调整前")
    print(f"  S1 = {s1}")

    print("\n[5] 离线系统补传:共享资源 R-LATE 上报到达,重处理挂起记录")
    outcomes = ingest_file(store, DATA_DIR / "v2_late_batch.json")
    print(f"  v2_late_batch.json: {len(outcomes)} 条")
    process_all(store, "m1.0")
    for r in reprocess_quarantined(store, "m1.0"):
        print(f"  重处理 {r.raw_id}: {r.outcome}" + (f" [{r.code}]" if r.code else ""))

    print("\n[6] 映射调整 m1.0 → m1.1(增补 '5G-ADVANCED' → NR),全量重跑(旧事实版本保留)")
    process_all(store, "m1.1")
    for r in reprocess_quarantined(store, "m1.1"):
        print(f"  重处理 {r.raw_id}: {r.outcome}" + (f" [{r.code}]" if r.code else ""))
    _print_quarantine(store)

    print("\n[7] 生成快照 S2(映射 m1.1)并对比 S1/S2")
    s2 = create_snapshot(store, "2026-01-01", "2026-12-31", "m1.1", note="映射调整后")
    print(f"  S2 = {s2}")
    diff = compare_snapshots(store, s1, s2)
    print(f"  新增资产事实: {[f['asset_id'] for f in diff['asset_diff']['added']]}")
    print(f"  移除资产事实: {[f['asset_id'] for f in diff['asset_diff']['removed']]}")
    print(f"  新增共享资源: {[f['resource_id'] for f in diff['shared_diff']['added']]}")
    for row in diff["capacity_delta"]:
        d, u = row["available_down"], row["available_up"]
        if d["old"] != d["new"] or u["old"] != u["new"]:
            print(
                f"  容量变化 {row['standard']}[{row['unit']}]: "
                f"下行 {d['old']} → {d['new']}, 上行 {u['old']} → {u['new']}"
            )
    print(f"  挂起隔离: 旧 {diff['open_quarantine']['old']} → 新 {diff['open_quarantine']['new']}")

    print("\n[8] 字段级溯源示例:资产 A-01 的 standard 与 capacity_up 来自哪版报文、如何转换")
    print("    (同一字段出现多次 = 同一内容存在于多个事实版本,重跑全量不覆盖旧版本)")
    for entry in field_lineage(store, "A-01", field="standard"):
        print(
            f"  [{entry['fact_version']}] standard ← 报文 {entry['raw_id']}(schema {entry['raw_schema_version']}) "
            f"字段 {entry['raw_path']}={entry['raw_value']!r} 经 {entry['rule_id']}: {entry['rule_note']}"
        )
    for entry in field_lineage(store, "A-01", field="capacity_up"):
        print(
            f"  [{entry['fact_version']}] capacity_up ← 报文 {entry['raw_id']}(schema {entry['raw_schema_version']}) "
            f"路径 {entry['raw_path']} 原始值={entry['raw_value']!r} 经 {entry['rule_id']}: {entry['rule_note']}"
        )

    print("\n[9] 共享资源归属口径:R-9 计一次,成员资产可追溯")
    view = asset_view(store, "A-01", as_of, "cf1+m1.1")
    if view:
        print(f"  A-01 入账容量: {view['counted_capacity']}")
        for t in view["shared_traces"]:
            print(f"  追溯共享资源 {t['resource_id']}(归属 {t['owner_scope']}): {t['counting_note']}")
    scope = scope_shared_capacity(store, "site", "SITE-1", as_of, "cf1+m1.1")
    print(f"  SITE-1 归属口径共享合计: 下行 {scope['total_down']} MBPS,成员 "
          f"{[m for r in scope['resources'] for m in r['members']]}")

    print("\n演练完成。样本均为合成报文,仅覆盖本地演练场景。")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="netres", description=__doc__)
    parser.add_argument("--db", default=":memory:", help="SQLite 路径(默认内存库)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ingest", help="接入报文文件")
    p.add_argument("paths", nargs="+")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("process", help="按映射版本全量处理")
    p.add_argument("--mapping", default=LATEST_MAPPING_VERSION, choices=sorted(MAPPING_VERSIONS))
    p.set_defaults(func=cmd_process)

    p = sub.add_parser("reprocess", help="重处理挂起隔离的报文")
    p.add_argument("--mapping", default=LATEST_MAPPING_VERSION, choices=sorted(MAPPING_VERSIONS))
    p.set_defaults(func=cmd_reprocess)

    p = sub.add_parser("facts", help="查询规范资产事实")
    p.add_argument("--standard")
    p.add_argument("--as-of")
    p.add_argument("--fact-version")
    p.set_defaults(func=cmd_facts)

    p = sub.add_parser("quarantine", help="隔离区报告")
    p.add_argument("--status", default="open")
    p.set_defaults(func=cmd_quarantine)

    p = sub.add_parser("capacity", help="按制式容量汇总")
    p.add_argument("--as-of", required=True)
    p.add_argument("--fact-version", required=True)
    p.add_argument("--naive", action="store_true", help="同时输出朴素重复计入口径作对照")
    p.set_defaults(func=cmd_capacity)

    p = sub.add_parser("asset", help="单资产视图(含共享追溯)")
    p.add_argument("asset_id")
    p.add_argument("--as-of", required=True)
    p.add_argument("--fact-version", required=True)
    p.set_defaults(func=cmd_asset)

    p = sub.add_parser("scope", help="归属口径共享容量")
    p.add_argument("scope_type", choices=["site", "asset"])
    p.add_argument("scope_id")
    p.add_argument("--as-of", required=True)
    p.add_argument("--fact-version", required=True)
    p.set_defaults(func=cmd_scope)

    p = sub.add_parser("lineage", help="字段级溯源")
    p.add_argument("asset_id")
    p.add_argument("--field")
    p.add_argument("--fact-version")
    p.set_defaults(func=cmd_lineage)

    p = sub.add_parser("provenance", help="原始报文去向")
    p.add_argument("raw_id")
    p.set_defaults(func=cmd_provenance)

    p = sub.add_parser("timeline", help="资产全时间线(含被取代事实)")
    p.add_argument("asset_id")
    p.set_defaults(func=cmd_timeline)

    p = sub.add_parser("snapshot", help="生成期间快照")
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--mapping", default=LATEST_MAPPING_VERSION, choices=sorted(MAPPING_VERSIONS))
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser("snapshots", help="列出快照")
    p.set_defaults(func=cmd_snapshots)

    p = sub.add_parser("compare", help="对比两个快照")
    p.add_argument("old")
    p.add_argument("new")
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("demo", help="完整演练(合成报文)")
    p.set_defaults(func=cmd_demo)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    with Store(args.db) as store:
        args.func(store, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
