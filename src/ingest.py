"""原始报文接入:原样保留、内容 hash 去重、版本声明与嗅探登记。

接入层不做任何规范化解释 —— 报文原文先入库,解释发生在 normalize 阶段,
且按显式映射版本进行。离线系统补传的相同报文只增加 seen_count,不产生重复事实。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .schemas import detect_version
from .store import Store


@dataclass
class IngestOutcome:
    raw_id: str
    is_new: bool
    declared_version: str | None
    detected_version: str | None


def _payload_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def ingest_payload(store: Store, payload: dict, source_note: str = "") -> IngestOutcome:
    """接入单条报文(dict)。原文以规范化 JSON 形式保留(key 排序,保证 hash 稳定)。"""
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return ingest_text(store, text, source_note=source_note)


def ingest_text(store: Store, text: str, source_note: str = "") -> IngestOutcome:
    """接入单条报文(JSON 文本)。原文逐字保留。"""
    payload = json.loads(text)  # 仅用于登记版本标记,不做解释
    if not isinstance(payload, dict):
        raise ValueError("报文须为 JSON 对象")
    digest = _payload_hash(text)
    raw_id = "R" + digest[:15]
    declared = payload.get("schema_version")
    detected = detect_version(payload)
    is_new = store.put_raw(
        raw_id=raw_id,
        payload_text=text,
        payload_hash=digest,
        declared=None if declared is None else str(declared),
        detected=detected,
        source_note=source_note,
    )
    return IngestOutcome(
        raw_id=raw_id,
        is_new=is_new,
        declared_version=None if declared is None else str(declared),
        detected_version=detected,
    )


def ingest_file(store: Store, path: str | Path, source_note: str | None = None) -> list[IngestOutcome]:
    """接入报文文件:JSON 数组或单个 JSON 对象。"""
    path = Path(path)
    note = source_note if source_note is not None else f"file:{path.name}"
    data = json.loads(path.read_text(encoding="utf-8"))
    items = data if isinstance(data, list) else [data]
    return [ingest_payload(store, item, source_note=note) for item in items]
