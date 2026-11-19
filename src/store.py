"""SQLite 存储层:原始报文、规范事实、字段级 lineage、隔离区、期间快照。

设计要点:

- 原始报文原样保留(verbatim payload + 内容 hash 去重,补传只增加计数);
- 规范事实按 (fact_id, fact_version) 存储,重跑全量产生新事实版本,旧版本保留;
- 同一资产/资源、同一有效期起点、同一事实版本下的重复或更正报文,
  后处理者取代先前事实(旧事实标记 superseded_by,不删除,历史快照仍可追溯);
- 隔离区记录未进入下游的报文及事由,映射调整或补传到达后可重处理并解除。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

DDL = """
CREATE TABLE IF NOT EXISTS raw_messages (
  raw_id TEXT PRIMARY KEY,
  payload_text TEXT NOT NULL,        -- 原始报文原文
  payload_hash TEXT NOT NULL,
  declared_version TEXT,             -- 报文声明的 schema_version(原文)
  detected_version TEXT,             -- 结构嗅探版本
  source_note TEXT,
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  seen_count INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS processing_log (
  raw_id TEXT NOT NULL,
  mapping_version TEXT NOT NULL,
  outcome TEXT NOT NULL,             -- fact | shared | quarantined
  ref_id TEXT,
  processed_at TEXT NOT NULL,
  PRIMARY KEY (raw_id, mapping_version)
);
CREATE TABLE IF NOT EXISTS facts (
  fact_id TEXT NOT NULL,
  fact_version TEXT NOT NULL,
  mapping_version TEXT NOT NULL,
  asset_id TEXT NOT NULL,
  vendor TEXT NOT NULL,
  standard TEXT NOT NULL,
  capacity_down REAL NOT NULL,
  capacity_up REAL,                  -- v1 无上行字段,记 NULL(未知,非 0)
  capacity_unit TEXT NOT NULL,
  shared_ref TEXT,                   -- 共享资源追溯引用(不计入本资产容量)
  valid_from TEXT NOT NULL,
  valid_to TEXT,                     -- NULL = 开放区间
  raw_id TEXT NOT NULL,
  warnings TEXT NOT NULL DEFAULT '[]',
  superseded_by TEXT,                -- 更正语义:被哪条事实取代
  created_at TEXT NOT NULL,
  PRIMARY KEY (fact_id, fact_version)
);
CREATE TABLE IF NOT EXISTS shared_facts (
  sf_id TEXT NOT NULL,
  fact_version TEXT NOT NULL,
  mapping_version TEXT NOT NULL,
  resource_id TEXT NOT NULL,
  owner_scope_type TEXT NOT NULL,    -- site | asset
  owner_scope_id TEXT NOT NULL,
  standard TEXT NOT NULL,            -- 规范制式或 MULTI
  capacity_down REAL NOT NULL,
  capacity_up REAL NOT NULL,
  capacity_unit TEXT NOT NULL,
  valid_from TEXT NOT NULL,
  valid_to TEXT,
  raw_id TEXT NOT NULL,
  superseded_by TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY (sf_id, fact_version)
);
CREATE TABLE IF NOT EXISTS lineage (
  fact_id TEXT NOT NULL,             -- 资产事实或共享资源事实 id
  fact_version TEXT NOT NULL,
  field TEXT NOT NULL,               -- 规范字段名
  raw_id TEXT NOT NULL,
  raw_path TEXT NOT NULL,            -- 原始报文字段路径
  raw_value TEXT,                    -- 原始取值(null 表示源报文无此字段)
  rule_id TEXT NOT NULL,
  rule_note TEXT NOT NULL,
  PRIMARY KEY (fact_id, fact_version, field, raw_path)
);
CREATE TABLE IF NOT EXISTS quarantine (
  quar_id INTEGER PRIMARY KEY AUTOINCREMENT,
  raw_id TEXT NOT NULL,
  mapping_version TEXT NOT NULL,
  stage TEXT NOT NULL,               -- version | schema | normalize
  code TEXT NOT NULL,                -- 见 src/normalize.py 诊断码
  detail TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',  -- open | resolved | superseded
  resolved_by TEXT,
  resolved_with_mapping TEXT,
  created_at TEXT NOT NULL,
  resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS snapshots (
  snapshot_id TEXT PRIMARY KEY,
  period_start TEXT NOT NULL,
  period_end TEXT NOT NULL,
  mapping_version TEXT NOT NULL,     -- 生成时使用的映射版本
  fact_version TEXT NOT NULL,        -- 生成时使用的事实版本
  note TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshot_facts (
  snapshot_id TEXT NOT NULL,
  kind TEXT NOT NULL,                -- asset | shared
  fact_id TEXT NOT NULL,
  fact_version TEXT NOT NULL,
  PRIMARY KEY (snapshot_id, kind, fact_id, fact_version)
);
CREATE TABLE IF NOT EXISTS mapping_definitions (
  mapping_version TEXT NOT NULL,
  source_schema TEXT NOT NULL,
  standard_map_json TEXT NOT NULL,
  unit_map_json TEXT NOT NULL,
  note TEXT NOT NULL,
  registered_at TEXT NOT NULL,
  PRIMARY KEY (mapping_version, source_schema)
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    """存储入口。path=":memory:" 用于测试与一次性演练。"""

    def __init__(self, path: str = ":memory:"):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(DDL)
        self._register_mappings()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- 映射定义登记(审计:快照引用的映射版本在库内可自查) --------------------

    def _register_mappings(self) -> None:
        from .mappings import MAPPING_VERSIONS, STANDARD_MAPS, UNIT_CONVERSIONS

        now = utcnow()
        with self.conn:
            for mv, meta in MAPPING_VERSIONS.items():
                for schema_ver in ("1.0", "2.0"):
                    self.conn.execute(
                        "INSERT OR IGNORE INTO mapping_definitions VALUES (?,?,?,?,?,?)",
                        (
                            mv,
                            schema_ver,
                            json.dumps(
                                STANDARD_MAPS[(schema_ver, mv)],
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                            json.dumps(UNIT_CONVERSIONS, ensure_ascii=False, sort_keys=True),
                            meta.note,
                            now,
                        ),
                    )

    def mapping_definitions(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM mapping_definitions ORDER BY mapping_version, source_schema"
        ).fetchall()
        return [dict(r) for r in rows]

    # -- 原始报文 -------------------------------------------------------------

    def put_raw(
        self,
        raw_id: str,
        payload_text: str,
        payload_hash: str,
        declared: str | None,
        detected: str | None,
        source_note: str,
    ) -> bool:
        """登记原始报文;已存在(同 hash)则只更新补传计数。返回是否为新报文。"""
        now = utcnow()
        with self.conn:
            row = self.conn.execute(
                "SELECT raw_id FROM raw_messages WHERE raw_id = ?", (raw_id,)
            ).fetchone()
            if row:
                self.conn.execute(
                    "UPDATE raw_messages SET last_seen = ?, seen_count = seen_count + 1 "
                    "WHERE raw_id = ?",
                    (now, raw_id),
                )
                return False
            self.conn.execute(
                "INSERT INTO raw_messages VALUES (?,?,?,?,?,?,?,?,1)",
                (raw_id, payload_text, payload_hash, declared, detected, source_note, now, now),
            )
            return True

    def get_raw(self, raw_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM raw_messages WHERE raw_id = ?", (raw_id,)
        ).fetchone()
        return dict(row) if row else None

    def iter_raws(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM raw_messages ORDER BY first_seen, raw_id").fetchall()
        return [dict(r) for r in rows]

    # -- 处理台账 -------------------------------------------------------------

    def log_processed(self, raw_id: str, mapping_version: str, outcome: str, ref_id: str | None) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO processing_log VALUES (?,?,?,?,?)",
                (raw_id, mapping_version, outcome, ref_id, utcnow()),
            )

    def processed_entry(self, raw_id: str, mapping_version: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM processing_log WHERE raw_id = ? AND mapping_version = ?",
            (raw_id, mapping_version),
        ).fetchone()
        return dict(row) if row else None

    def processing_entries(self, raw_id: str | None = None) -> list[dict]:
        if raw_id is None:
            rows = self.conn.execute("SELECT * FROM processing_log ORDER BY processed_at").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM processing_log WHERE raw_id = ? ORDER BY processed_at", (raw_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    # -- 规范事实(资产) -------------------------------------------------------

    def put_fact(self, fact: dict) -> None:
        """写入资产事实。

        同一资产、同一有效期起点、同一事实版本下已存在不同内容的先前事实时,
        将其标记为 superseded_by(更正语义:后报为准);旧事实保留可溯。
        """
        with self.conn:
            stale = self.conn.execute(
                "SELECT fact_id FROM facts WHERE asset_id = ? AND valid_from = ? "
                "AND fact_version = ? AND superseded_by IS NULL AND fact_id <> ?",
                (fact["asset_id"], fact["valid_from"], fact["fact_version"], fact["fact_id"]),
            ).fetchall()
            for row in stale:
                self.conn.execute(
                    "UPDATE facts SET superseded_by = ? WHERE fact_id = ? AND fact_version = ?",
                    (fact["fact_id"], row["fact_id"], fact["fact_version"]),
                )
            self.conn.execute(
                "INSERT OR REPLACE INTO facts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    fact["fact_id"],
                    fact["fact_version"],
                    fact["mapping_version"],
                    fact["asset_id"],
                    fact["vendor"],
                    fact["standard"],
                    fact["capacity_down"],
                    fact["capacity_up"],
                    fact["capacity_unit"],
                    fact["shared_ref"],
                    fact["valid_from"],
                    fact["valid_to"],
                    fact["raw_id"],
                    json.dumps(fact.get("warnings", []), ensure_ascii=False),
                    fact.get("superseded_by"),
                    fact.get("created_at") or utcnow(),
                ),
            )

    def get_facts(
        self,
        fact_version: str | None = None,
        asset_id: str | None = None,
        standard: str | None = None,
        as_of: str | None = None,
        include_superseded: bool = False,
    ) -> list[dict]:
        sql = "SELECT * FROM facts WHERE 1=1"
        args: list[object] = []
        if fact_version:
            sql += " AND fact_version = ?"
            args.append(fact_version)
        if asset_id:
            sql += " AND asset_id = ?"
            args.append(asset_id)
        if standard:
            sql += " AND standard = ?"
            args.append(standard)
        if as_of:
            sql += " AND valid_from <= ? AND (valid_to IS NULL OR valid_to >= ?)"
            args.extend([as_of, as_of])
        if not include_superseded:
            sql += " AND superseded_by IS NULL"
        sql += " ORDER BY asset_id, valid_from"
        rows = self.conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    # -- 规范事实(共享资源) ---------------------------------------------------

    def put_shared_fact(self, sf: dict) -> None:
        with self.conn:
            stale = self.conn.execute(
                "SELECT sf_id FROM shared_facts WHERE resource_id = ? AND valid_from = ? "
                "AND fact_version = ? AND superseded_by IS NULL AND sf_id <> ?",
                (sf["resource_id"], sf["valid_from"], sf["fact_version"], sf["sf_id"]),
            ).fetchall()
            for row in stale:
                self.conn.execute(
                    "UPDATE shared_facts SET superseded_by = ? WHERE sf_id = ? AND fact_version = ?",
                    (sf["sf_id"], row["sf_id"], sf["fact_version"]),
                )
            self.conn.execute(
                "INSERT OR REPLACE INTO shared_facts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    sf["sf_id"],
                    sf["fact_version"],
                    sf["mapping_version"],
                    sf["resource_id"],
                    sf["owner_scope_type"],
                    sf["owner_scope_id"],
                    sf["standard"],
                    sf["capacity_down"],
                    sf["capacity_up"],
                    sf["capacity_unit"],
                    sf["valid_from"],
                    sf["valid_to"],
                    sf["raw_id"],
                    sf.get("superseded_by"),
                    sf.get("created_at") or utcnow(),
                ),
            )

    def get_shared_facts(
        self,
        fact_version: str | None = None,
        resource_id: str | None = None,
        as_of: str | None = None,
        owner_scope: tuple[str, str] | None = None,
        include_superseded: bool = False,
    ) -> list[dict]:
        sql = "SELECT * FROM shared_facts WHERE 1=1"
        args: list[object] = []
        if fact_version:
            sql += " AND fact_version = ?"
            args.append(fact_version)
        if resource_id:
            sql += " AND resource_id = ?"
            args.append(resource_id)
        if as_of:
            sql += " AND valid_from <= ? AND (valid_to IS NULL OR valid_to >= ?)"
            args.extend([as_of, as_of])
        if owner_scope:
            sql += " AND owner_scope_type = ? AND owner_scope_id = ?"
            args.extend(owner_scope)
        if not include_superseded:
            sql += " AND superseded_by IS NULL"
        sql += " ORDER BY resource_id, valid_from"
        rows = self.conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def members_of(self, resource_id: str, fact_version: str) -> list[dict]:
        """共享资源的成员资产(由事实上的 shared_ref 追溯引用派生)。"""
        rows = self.conn.execute(
            "SELECT * FROM facts WHERE shared_ref = ? AND fact_version = ? "
            "AND superseded_by IS NULL ORDER BY asset_id",
            (resource_id, fact_version),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- 字段级 lineage --------------------------------------------------------

    def put_lineage(self, rows: list[dict]) -> None:
        with self.conn:
            for r in rows:
                self.conn.execute(
                    "INSERT OR REPLACE INTO lineage VALUES (?,?,?,?,?,?,?,?)",
                    (
                        r["fact_id"],
                        r["fact_version"],
                        r["field"],
                        r["raw_id"],
                        r["raw_path"],
                        None if r.get("raw_value") is None else str(r["raw_value"]),
                        r["rule_id"],
                        r["rule_note"],
                    ),
                )

    def lineage_for(self, fact_id: str, fact_version: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM lineage WHERE fact_id = ? AND fact_version = ? ORDER BY field, raw_path",
            (fact_id, fact_version),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- 隔离区 ---------------------------------------------------------------

    def add_quarantine(
        self, raw_id: str, mapping_version: str, stage: str, code: str, detail: str
    ) -> int:
        """登记隔离记录。同事由已挂起则不重复;同事同映射的旧挂起条目置为 superseded。"""
        now = utcnow()
        with self.conn:
            dup = self.conn.execute(
                "SELECT quar_id FROM quarantine WHERE raw_id = ? AND mapping_version = ? "
                "AND code = ? AND status = 'open'",
                (raw_id, mapping_version, code),
            ).fetchone()
            if dup:
                return int(dup["quar_id"])
            self.conn.execute(
                "UPDATE quarantine SET status = 'superseded', resolved_at = ? "
                "WHERE raw_id = ? AND mapping_version = ? AND status = 'open'",
                (now, raw_id, mapping_version),
            )
            cur = self.conn.execute(
                "INSERT INTO quarantine (raw_id, mapping_version, stage, code, detail, status, created_at)"
                " VALUES (?,?,?,?,?, 'open', ?)",
                (raw_id, mapping_version, stage, code, detail, now),
            )
            return int(cur.lastrowid)

    def resolve_quarantines_for_raw(self, raw_id: str, resolved_by: str, mapping_version: str) -> int:
        """报文成功进入下游后,解除其所有挂起隔离记录。返回解除条数。"""
        with self.conn:
            cur = self.conn.execute(
                "UPDATE quarantine SET status = 'resolved', resolved_by = ?, "
                "resolved_with_mapping = ?, resolved_at = ? WHERE raw_id = ? AND status = 'open'",
                (resolved_by, mapping_version, utcnow(), raw_id),
            )
            return cur.rowcount

    def quarantines(
        self,
        status: str | None = None,
        mapping_version: str | None = None,
        raw_id: str | None = None,
    ) -> list[dict]:
        sql = "SELECT * FROM quarantine WHERE 1=1"
        args: list[object] = []
        if status:
            sql += " AND status = ?"
            args.append(status)
        if mapping_version:
            sql += " AND mapping_version = ?"
            args.append(mapping_version)
        if raw_id:
            sql += " AND raw_id = ?"
            args.append(raw_id)
        sql += " ORDER BY quar_id"
        rows = self.conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    # -- 期间快照 -------------------------------------------------------------

    def put_snapshot(self, snapshot: dict, members: list[tuple[str, str, str]]) -> None:
        """写入快照及其物化事实清单。members: (kind, fact_id, fact_version)。"""
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?,?,?)",
                (
                    snapshot["snapshot_id"],
                    snapshot["period_start"],
                    snapshot["period_end"],
                    snapshot["mapping_version"],
                    snapshot["fact_version"],
                    snapshot.get("note", ""),
                    snapshot.get("created_at") or utcnow(),
                ),
            )
            self.conn.execute(
                "DELETE FROM snapshot_facts WHERE snapshot_id = ?", (snapshot["snapshot_id"],)
            )
            for kind, fact_id, fact_version in members:
                self.conn.execute(
                    "INSERT INTO snapshot_facts VALUES (?,?,?,?)",
                    (snapshot["snapshot_id"], kind, fact_id, fact_version),
                )

    def get_snapshot(self, snapshot_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM snapshots WHERE snapshot_id = ?", (snapshot_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_snapshots(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM snapshots ORDER BY created_at, snapshot_id").fetchall()
        return [dict(r) for r in rows]

    def snapshot_members(self, snapshot_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM snapshot_facts WHERE snapshot_id = ? ORDER BY kind, fact_id",
            (snapshot_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def facts_by_ids(self, refs: list[tuple[str, str]], table: str) -> list[dict]:
        """按 (id, fact_version) 取事实;table 为 facts 或 shared_facts。"""
        if table not in ("facts", "shared_facts"):
            raise ValueError(table)
        id_col = "fact_id" if table == "facts" else "sf_id"
        out: list[dict] = []
        for fact_id, fact_version in refs:
            row = self.conn.execute(
                f"SELECT * FROM {table} WHERE {id_col} = ? AND fact_version = ?",
                (fact_id, fact_version),
            ).fetchone()
            if row:
                out.append(dict(row))
        return out
