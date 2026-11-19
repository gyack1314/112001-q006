# 网络资源目录数据接入与规范建模

面向多制式、多设备厂商的资产数据接入:旧版(v1)与新版(v2)报文并存,新版本拆分
容量字段、改变枚举含义、增加可选的共享资源关系;旧报文仍会因离线系统补传而到达。
本项目把原始报文原样保留,按**显式版本化的 Schema 映射**形成规范事实,供下游按
资产、制式和有效期间查询。

运行环境为 Python 3.11 或更高版本,仅使用标准库(SQLite 存储)。

## 快速开始

```bash
python -m unittest discover -s tests   # 运行测试
python -m src demo                     # 完整演练(内存库,合成报文)
```

持久化分步操作:

```bash
python -m src --db netres.db ingest data/messages.json data/v1_backfill.json data/v2_batch.json
python -m src --db netres.db process --mapping m1.0        # 全量规范化(幂等)
python -m src --db netres.db quarantine                    # 隔离区:未进入下游的记录
python -m src --db netres.db capacity --as-of 2026-06-01 --fact-version cf1+m1.0 --naive
python -m src --db netres.db snapshot --start 2026-01-01 --end 2026-12-31 --mapping m1.0
python -m src --db netres.db snapshots
python -m src --db netres.db compare <SN_OLD> <SN_NEW>
python -m src --db netres.db lineage A-01 --field standard # 字段级溯源
python -m src --db netres.db asset A-01 --as-of 2026-06-01 --fact-version cf1+m1.0
```

## 设计

### 分层

```
data/*.json ──▶ ingest ──▶ raw_messages(原文保留,hash 去重,补传计数)
                  │
                  ▼  normalize(按显式映射版本,两遍:先资源上报后资产报文)
            ┌─────────────┬──────────────┐
            ▼             ▼              ▼
        facts        shared_facts    quarantine(隔离区,可诊断、可重处理)
      (规范资产事实)  (共享资源事实)        │
            │             │              │ 补传到达 / 映射调整后 reprocess
            ▼             ▼              ▼
        lineage(字段级溯源) ◀── 每个规范字段:来源报文、原始值、转换规则
            │
            ▼  snapshot(期间快照:物化事实 + 钉住映射版本与事实版本)
        snapshots ──▶ compare(映射调整后比较新旧结果)
```

### Schema 与映射双版本

- **Schema 注册表**(`src/schemas.py`):v1 / v2 显式登记结构要求与能力标记。
  v1 单一 `capacity`(旧口径仅下行);v2 拆分 `{down, up}`、代际枚举、可选
  `shared_resource` 关系与 `shared_resource_report`。声明版本缺失 / 未知 /
  与结构嗅探冲突 → 隔离(`version_missing` / `version_unknown` / `version_mismatch`)。
- **映射注册表**(`src/mappings.py`):枚举映射与单位换算为**数据**,
  按映射版本(`m1.0`、`m1.1`)显式登记;映射调整 = 新增版本,旧版本保留。
  事实版本 `fact_version = cf1+<映射版本>`,重跑全量产生新事实版本,不覆盖旧版本。

### 规范事实与口径

- 规范制式:`GSM / UMTS / LTE / NR`;v2 代际枚举经映射表转换(如 `5G→NR`);
  共享资源未携带制式时显式标记 `MULTI`(跨制式,非默认值填充)。
- 容量:v1 标量按旧口径解释为下行,上行记 `null`(**未知,非 0**),不参与上行
  合计并以 `up_missing_count` 暴露缺口;v2 `{down, up}` 为独享容量。
- 单位归一:`Gbps→MBPS ×1000`(大小写不敏感);`ERLANG` 不可换算,原样保留,
  聚合按 (制式, 单位) 分桶,不跨单位合并。
- **共享资源按归属口径(`owner_scope`)计一次**:资产入账容量仅含独享部分,
  成员资产保留对共享资源的追溯引用;全局/制式合计 = Σ 独享 + Σ 共享资源
  (每资源一次)。`--naive` 输出"按成员重复计入"的反例口径作对照。

### 可诊断性(不填默认值)

缺字段、未知枚举、未知单位、容量为负/非数值、有效期倒置、共享关系悬空
(引用未登记或有效期不重叠)→ 记录进入隔离区,保留原始报文与诊断事由,
**不产生任何事实**。补传到达或映射调整后 `reprocess` 重处理,成功即解除隔离
并记录解除时使用的映射版本。同一资产同一有效期起点的更正报文以后报为准,
旧事实标记 `superseded_by` 保留可溯。

### 快照与溯源

- 期间快照物化该期间内有效的事实(资产 + 共享资源),并记录生成时使用的
  `mapping_version` 与 `fact_version`;快照内容寻址、幂等,生成后不受后续
  重跑影响。`compare` 输出事实增删改、分制式容量差异、隔离区变化。
- 字段级 lineage:每个规范字段记录来源报文(含其 schema 版本)、原始字段路径
  与取值、转换规则及规则说明;`provenance` 反查单条原始报文的去向。

## 目录

- `src/` — 包:schemas / mappings / ingest / normalize / aggregate / snapshot /
  compare / queries / store / cli
- `data/` — 合成报文样本(**仅用于本地演练,不代表真实厂商接口**,见 data/README.md)
- `tests/` — 单元测试与端到端样本流程测试

## 边界与限制

- 合成报文只覆盖本地演练场景;真实厂商接口的认证、传输、增量协议不在范围内。
- 有效期重叠的不同事实不做自动合并(仅同一有效期起点的更正以后报为准);
  跨期间容量汇总请使用 `capacity --as-of` 的时点口径,快照聚合为期间内有效事实的合集。
- 共享资源容量与成员独享容量之间不做超额订阅校验(成员共享部分之和可超过资源容量)。
