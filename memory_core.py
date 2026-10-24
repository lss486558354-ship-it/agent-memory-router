"""Agent 本地记忆路由 —— 存储核心。

设计目标只有一句话：**Agent 永远不需要把全部历史塞进上下文**。
它先在索引里搜（只拿摘要），确定相关后再按 ID 取原文（只拿一条）。

因此这里刻意分成两层：

    data/memory.db       索引（小、常驻、可全表搜）
    raw_data/<id>/raw.txt 原文（大、按需加载）

三个入口共用本模块，**不存在第二份 schema 或第二套连接逻辑**：
    init_db.py     初始化
    cli.py         人用
    mcp_server.py  Agent 用

环境变量：

    MEMORY_ROUTER_HOME   数据根目录，默认是本文件所在目录。
                         测试和"多项目隔离"都靠它，不要写死路径。
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------

ROOT = Path(
    os.environ.get("MEMORY_ROUTER_HOME") or Path(__file__).resolve().parent
).resolve()
DATA_DIR = ROOT / "data"
RAW_DIR = ROOT / "raw_data"
DB_PATH = DATA_DIR / "memory.db"

# 单条原文上限。再大就不该塞进上下文了 —— 那正是本系统要避免的事。
MAX_RAW_BYTES = 2 * 1024 * 1024

# 一次检索最多返回多少条。宁可让 Agent 多搜一次，也不要把上下文撑爆。
MAX_SEARCH_LIMIT = 50
DEFAULT_SEARCH_LIMIT = 10

# 记录 ID 只允许这些字符。
#
# **安全要点**：MCP 工具的参数直接来自模型输出，模型可以被提示词注入。
# 若把 record_id 直接拼进路径，`../../` 就能读到系统任意文件。
# 这里用白名单正则 + 下文的路径归属校验，双重拦截目录穿越。
_RECORD_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")

# 记录类型 → ID 前缀。D_0001 这种可读性比 uuid 好得多，
# 人扫一眼日志就知道是决策还是代码变更。
TYPE_PREFIXES: dict[str, str] = {
    "decision": "D",
    "code_change": "C",
    "issue": "I",
    "note": "N",
    "session": "S",
    "reference": "R",
}
DEFAULT_PREFIX = "M"

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS entities (
    id             TEXT PRIMARY KEY,
    type           TEXT NOT NULL,
    title          TEXT NOT NULL,
    keywords       TEXT NOT NULL DEFAULT '',
    file_path      TEXT NOT NULL,
    created_at     TIMESTAMP NOT NULL,
    update_version INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_entities_type       ON entities(type);
CREATE INDEX IF NOT EXISTS idx_entities_created_at ON entities(created_at);

CREATE TABLE IF NOT EXISTS relations (
    source_id     TEXT NOT NULL,
    target_id     TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    PRIMARY KEY (source_id, target_id, relation_type),
    FOREIGN KEY (source_id) REFERENCES entities(id) ON DELETE CASCADE,
    FOREIGN KEY (target_id) REFERENCES entities(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_relations_target ON relations(target_id);
"""

_schema_ready = False


class MemoryRouterError(Exception):
    """本模块所有可预期错误的基类，便于 CLI / MCP 统一转成友好提示。

    刻意不叫 `MemoryError` —— 那是 Python 内置异常名，重名会让
    `except MemoryError` 的语义在阅读时产生歧义。
    """


class RecordNotFound(MemoryRouterError):
    pass


class InvalidRecordId(MemoryRouterError):
    pass


class DuplicateRecord(MemoryRouterError):
    pass


# ---------------------------------------------------------------------------
# 连接
# ---------------------------------------------------------------------------


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """打开一个连接，成功则提交，出错则回滚。

    每次操作单开连接、用完即关：本地 SQLite 建连接是微秒级开销，
    换来的是**不需要考虑线程安全** —— MCP server 可能在任意线程里调工具。
    """
    global _schema_ready

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL：CLI 写入时 MCP server 仍可读，反之亦然
    conn.execute("PRAGMA journal_mode = WAL")

    try:
        if not _schema_ready:
            conn.executescript(SCHEMA_SQL)
            _schema_ready = True
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> dict[str, Any]:
    """建目录、建表。可重复调用（全部 IF NOT EXISTS）。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA_SQL)
    return {
        "db_path": str(DB_PATH),
        "raw_dir": str(RAW_DIR),
        "tables": ["entities", "relations"],
    }


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def configure_console() -> None:
    """把 stdout/stderr 切到 UTF-8。

    中文 Windows 控制台默认 GBK，打印 ✅ / ⚠️ / 中文标点会直接抛
    `UnicodeEncodeError` 让命令崩掉，而报错信息完全看不出跟编码有关。

    三个入口（init_db / cli / mcp_server）都调用它 —— 放在 core 里，
    避免每个文件各抄一份、然后其中一个忘了抄。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _like_escape(term: str) -> str:
    """转义 LIKE 通配符。

    不转义的话，搜 `50%` 会变成「匹配任意内容」，
    搜 `a_b` 里的 `_` 会变成「任意单字符」—— 结果对，但会多返回一堆噪声。
    """
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _validate_record_id(record_id: str) -> str:
    if not isinstance(record_id, str) or not _RECORD_ID_RE.match(record_id):
        raise InvalidRecordId(
            f"非法记录 ID：{record_id!r}。只允许字母开头，"
            "由字母/数字/下划线组成，最长 64 字符。"
        )
    return record_id


def _record_dir(record_id: str) -> Path:
    """解析记录目录，并确认它真的在 raw_data/ 里面。

    正则已经挡掉了 `../`，这里再做一次归属校验 ——
    防御必须假设前一道可能被绕过（比如将来放宽了正则）。
    """
    _validate_record_id(record_id)
    path = (RAW_DIR / record_id).resolve()
    raw_root = RAW_DIR.resolve()
    if not path.is_relative_to(raw_root):
        raise InvalidRecordId(f"记录路径越界：{record_id!r}")
    return path


def _prefix_for(type_: str) -> str:
    return TYPE_PREFIXES.get(normalize_type(type_), DEFAULT_PREFIX)


def normalize_type(type_: str) -> str:
    """类型归一化：小写，空格和连字符转下划线。"""
    cleaned = re.sub(r"[^a-z0-9]+", "_", (type_ or "").strip().lower()).strip("_")
    return cleaned or "note"


def _next_id(conn: sqlite3.Connection, type_: str) -> str:
    """生成 D_0001 这样的顺序 ID。

    取该前缀下已用的最大序号 +1。解析在 Python 里做而不是 SQL ——
    SQL 对字符串做数值排序很容易写出静默错误（'9' > '10'）。
    """
    prefix = _prefix_for(type_)
    rows = conn.execute(
        "SELECT id FROM entities WHERE id LIKE ? ESCAPE '\\'",
        (f"{prefix}\\_%",),
    ).fetchall()
    biggest = 0
    for row in rows:
        suffix = row["id"][len(prefix) + 1 :]
        if suffix.isdigit():
            biggest = max(biggest, int(suffix))
    return f"{prefix}_{biggest + 1:04d}"


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------


def add_entity(
    *,
    type: str,
    title: str,
    content: str,
    keywords: str | list[str] | None = None,
    record_id: str | None = None,
    relations: list[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """新增一条记忆：原文落盘 + 索引入库。

    Args:
        type: 记录类型，决定 ID 前缀（decision → D_0001）。
        title: **一行摘要**。这是检索时唯一会被看到的内容，要写得能独立读懂。
        content: 完整原文，写进 raw_data/<id>/raw.txt，只在需要时读取。
        keywords: 逗号分隔字符串或列表。
        record_id: 指定 ID（默认自动生成）。
        relations: [(目标ID, 关系类型), ...]。
    """
    if not isinstance(title, str) or not title.strip():
        raise MemoryRouterError("title 不能为空 —— 它是检索时唯一可见的摘要。")
    if not isinstance(content, str):
        raise MemoryRouterError("content 必须是字符串。")

    type_ = normalize_type(type)
    if isinstance(keywords, (list, tuple)):
        keyword_text = ",".join(str(k).strip() for k in keywords if str(k).strip())
    else:
        keyword_text = (keywords or "").strip()

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    with connect() as conn:
        rid = record_id or _next_id(conn, type_)
        _validate_record_id(rid)

        exists = conn.execute(
            "SELECT 1 FROM entities WHERE id = ?", (rid,)
        ).fetchone()
        if exists:
            raise DuplicateRecord(f"记录已存在：{rid}")

        record_dir = _record_dir(rid)
        record_dir.mkdir(parents=True, exist_ok=True)
        raw_file = record_dir / "raw.txt"

        # 先写临时文件再改名：中途崩溃不会留下半截原文。
        tmp_file = record_dir / "raw.txt.tmp"
        tmp_file.write_text(content, encoding="utf-8")
        tmp_file.replace(raw_file)

        created_at = now_iso()
        try:
            conn.execute(
                """
                INSERT INTO entities
                    (id, type, title, keywords, file_path, created_at, update_version)
                VALUES (?, ?, ?, ?, ?, ?, 1)
                """,
                (rid, type_, title.strip(), keyword_text, str(raw_file), created_at),
            )
            for target_id, relation_type in relations or []:
                _validate_record_id(target_id)
                conn.execute(
                    """
                    INSERT OR IGNORE INTO relations
                        (source_id, target_id, relation_type)
                    VALUES (?, ?, ?)
                    """,
                    (rid, target_id, relation_type),
                )
        except Exception:
            # 入库失败则把刚写的原文删掉，避免留下没有索引的孤儿文件
            raw_file.unlink(missing_ok=True)
            try:
                record_dir.rmdir()
            except OSError:
                pass
            raise

    return get_entity(rid) or {}


def add_relation(source_id: str, target_id: str, relation_type: str) -> dict[str, Any]:
    """连两条记忆。目标不存在时外键会报错 —— 这是期望行为，不要静默忽略。"""
    _validate_record_id(source_id)
    _validate_record_id(target_id)
    if not relation_type.strip():
        raise MemoryRouterError("relation_type 不能为空。")
    if source_id == target_id:
        raise MemoryRouterError("不能把一条记忆关联到自己。")

    with connect() as conn:
        for rid in (source_id, target_id):
            if not conn.execute(
                "SELECT 1 FROM entities WHERE id = ?", (rid,)
            ).fetchone():
                raise RecordNotFound(f"记录不存在：{rid}")
        conn.execute(
            """
            INSERT OR IGNORE INTO relations (source_id, target_id, relation_type)
            VALUES (?, ?, ?)
            """,
            (source_id, target_id, relation_type.strip()),
        )
    return {"source_id": source_id, "target_id": target_id,
            "relation_type": relation_type.strip()}


def update_entity(
    record_id: str,
    *,
    title: str | None = None,
    keywords: str | list[str] | None = None,
    content: str | None = None,
) -> dict[str, Any]:
    """修改一条记忆，`update_version` 自增。

    原文**覆盖写**，旧版本不保留 —— 需要留痕就再建一条新记录并
    用 relations 连上（`supersedes`）。这样历史是显式的，不靠版本号猜。
    """
    _validate_record_id(record_id)
    current = get_entity(record_id)
    if current is None:
        raise RecordNotFound(f"记录不存在：{record_id}")

    fields: list[str] = []
    params: list[Any] = []

    if title is not None:
        if not title.strip():
            raise MemoryRouterError("title 不能为空。")
        fields.append("title = ?")
        params.append(title.strip())

    if keywords is not None:
        if isinstance(keywords, (list, tuple)):
            keyword_text = ",".join(
                str(k).strip() for k in keywords if str(k).strip()
            )
        else:
            keyword_text = keywords.strip()
        fields.append("keywords = ?")
        params.append(keyword_text)

    if content is not None:
        raw_file = _record_dir(record_id) / "raw.txt"
        raw_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_file = raw_file.with_suffix(".txt.tmp")
        tmp_file.write_text(content, encoding="utf-8")
        tmp_file.replace(raw_file)

    fields.append("update_version = update_version + 1")
    params.append(record_id)

    with connect() as conn:
        conn.execute(
            f"UPDATE entities SET {', '.join(fields)} WHERE id = ?", params
        )

    return get_entity(record_id) or {}


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------


def get_entity(record_id: str) -> dict[str, Any] | None:
    _validate_record_id(record_id)
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM entities WHERE id = ?", (record_id,)
        ).fetchone()
    return row_to_dict(row) if row else None


def search_memory(
    keyword: str = "",
    type: str | None = None,
    *,
    limit: int = DEFAULT_SEARCH_LIMIT,
) -> list[dict[str, Any]]:
    """在索引里搜，**只返回摘要，不返回原文**。

    这是整个系统的省 token 关键：一次搜索的返回量固定在几十到几百字节，
    与历史库有多大无关。
    """
    limit = max(1, min(int(limit), MAX_SEARCH_LIMIT))
    like = f"%{_like_escape(keyword.strip())}%" if keyword.strip() else None

    clauses: list[str] = []
    params: list[Any] = []

    if type:
        clauses.append("type = ?")
        params.append(normalize_type(type))

    if like:
        clauses.append(
            "(title LIKE ? ESCAPE '\\' OR keywords LIKE ? ESCAPE '\\' "
            "OR id LIKE ? ESCAPE '\\')"
        )
        params.extend([like, like, like])

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    # 标题命中排在关键词命中前面 —— 标题是人工写的摘要，相关性更高
    order = (
        "ORDER BY CASE WHEN title LIKE ? ESCAPE '\\' THEN 0 ELSE 1 END, "
        "created_at DESC, id DESC"
        if like
        else "ORDER BY created_at DESC, id DESC"
    )
    if like:
        params.append(like)
    params.append(limit)

    with connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM entities {where} {order} LIMIT ?", params
        ).fetchall()
    return [row_to_dict(r) for r in rows]


def read_raw_data(record_id: str, *, max_chars: int | None = None) -> dict[str, Any]:
    """按 ID 取原文。

    只在 `search_memory` 已经定位到相关记录后调用 ——
    直接拿 ID 就取全文，等于跳过了索引这一层，本系统就白建了。
    """
    entity = get_entity(record_id)
    if entity is None:
        raise RecordNotFound(f"记录不存在：{record_id}")

    path = Path(entity["file_path"])
    if not path.exists():
        raise RecordNotFound(
            f"索引存在但原文文件缺失：{path}。"
            "数据可能被手工删过，建议用 cli.py list 核对。"
        )

    size = path.stat().st_size
    if size > MAX_RAW_BYTES:
        raise MemoryRouterError(
            f"原文 {size:,} 字节，超过 {MAX_RAW_BYTES:,} 字节上限，拒绝整篇加载。"
            "请改读摘要，或把该记录拆成多条。"
        )

    text = path.read_text(encoding="utf-8", errors="replace")
    truncated = False
    if max_chars is not None and 0 < max_chars < len(text):
        text = text[:max_chars]
        truncated = True

    return {**entity, "content": text, "truncated": truncated, "size_bytes": size}


def list_entities(
    type: str | None = None, *, limit: int = 50, offset: int = 0
) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), MAX_SEARCH_LIMIT))
    offset = max(0, int(offset))
    with connect() as conn:
        if type:
            rows = conn.execute(
                "SELECT * FROM entities WHERE type = ? "
                "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                (normalize_type(type), limit, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM entities "
                "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
    return [row_to_dict(r) for r in rows]


def relations_for(record_id: str) -> dict[str, list[dict[str, str]]]:
    _validate_record_id(record_id)
    with connect() as conn:
        outgoing = conn.execute(
            "SELECT target_id, relation_type FROM relations "
            "WHERE source_id = ? ORDER BY target_id",
            (record_id,),
        ).fetchall()
        incoming = conn.execute(
            "SELECT source_id, relation_type FROM relations "
            "WHERE target_id = ? ORDER BY source_id",
            (record_id,),
        ).fetchall()
    return {
        "outgoing": [row_to_dict(r) for r in outgoing],
        "incoming": [row_to_dict(r) for r in incoming],
    }


def stats() -> dict[str, Any]:
    with connect() as conn:
        entity_count = conn.execute("SELECT COUNT(*) AS n FROM entities").fetchone()["n"]
        relation_count = conn.execute(
            "SELECT COUNT(*) AS n FROM relations"
        ).fetchone()["n"]
        by_type = conn.execute(
            "SELECT type, COUNT(*) AS n FROM entities GROUP BY type ORDER BY n DESC"
        ).fetchall()
    return {
        "entities": entity_count,
        "relations": relation_count,
        "by_type": {row["type"]: row["n"] for row in by_type},
        "db_path": str(DB_PATH),
        "raw_dir": str(RAW_DIR),
    }
