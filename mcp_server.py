"""MCP Server —— 把记忆库暴露给 Agent。

    python mcp_server.py            # stdio，供 Claude Code / Cursor 调用
    python mcp_server.py --check    # 只做自检，不启动服务

**工具返回值一律是 JSON 字符串**，不是裸文本。原因：
模型拿到的内容越结构化，越不容易把「摘要」当成「原文」，
也越容易在出错时判断下一步该干什么。

注册到 Claude Code（`.mcp.json` 或 `claude mcp add`）::

    {
      "mcpServers": {
        "memory-router": {
          "command": "python",
          "args": ["D:/agent-memory-router/mcp_server.py"]
        }
      }
    }
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import memory_core
from memory_core import (
    InvalidRecordId,
    MemoryRouterError,
    RecordNotFound,
)

try:
    from mcp.server.mcpserver import MCPServer
except ImportError as exc:  # pragma: no cover - 只在依赖缺失时触发
    raise SystemExit(
        "缺少 mcp 依赖。安装：pip install 'mcp>=2.0'\n"
        f"原始错误：{exc}"
    ) from exc


INSTRUCTIONS = """\
这是 Agent 的长期记忆库。使用顺序不可颠倒：

1. 遇到「之前怎么处理的」「这个项目的历史决定」这类问题，
   **先调 search_memory** 拿摘要，不要凭印象回答。
2. 只有摘要不足以回答时，才对**具体某一条**调 read_raw_data。
   不要为了"看看有什么"而批量读原文 —— 那等于把整个历史塞回上下文，
   本系统就失去意义了。
3. 完成重大决策或代码修改后，调 save_memory 写一条记录。
   title 写成**一行能独立读懂的摘要**（它会被存进索引），
   content 写完整背景、原因、取舍。

索引在 data/memory.db，原文在 raw_data/<id>/raw.txt，都是本地文件。
"""

mcp = MCPServer(
    name="agent-memory-router",
    version="0.1.0",
    instructions=INSTRUCTIONS,
)


def _ok(payload: Any) -> str:
    return json.dumps({"ok": True, **payload}, ensure_ascii=False, indent=2)


def _err(exc: Exception, hint: str = "") -> str:
    payload: dict[str, Any] = {
        "ok": False,
        "error": str(exc),
        "error_type": type(exc).__name__,
    }
    if hint:
        payload["hint"] = hint
    return json.dumps(payload, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# 工具 1/4：检索（只返回摘要）
# ---------------------------------------------------------------------------


@mcp.tool()
def search_memory(keyword: str = "", type: str | None = None, limit: int = 10) -> str:
    """在记忆索引中检索，返回**摘要列表**（不含原文）。

    这是访问历史记忆的**第一步**。不要跳过它直接猜。

    Args:
        keyword: 关键词，匹配标题、关键词标签和记录 ID。留空则按时间倒序返回最近记录。
        type: 可选，按类型过滤。常用值：decision / code_change / issue / note / reference。
        limit: 最多返回条数，1–50，默认 10。

    Returns:
        JSON：{ok, count, results:[{id, type, title, keywords, created_at, update_version}]}
        拿到 id 后，如需全文再调 read_raw_data。
    """
    try:
        results = memory_core.search_memory(keyword, type, limit=limit)
    except MemoryRouterError as exc:
        return _err(exc, hint="检查 keyword / type 参数是否合法。")

    return _ok(
        {
            "count": len(results),
            "query": {"keyword": keyword, "type": type},
            "results": results,
            "next_step": (
                "如需完整内容，对具体某条调用 read_raw_data(record_id)。"
                if results
                else "没有命中。可换更宽泛的关键词，或去掉 type 过滤。"
            ),
        }
    )


# ---------------------------------------------------------------------------
# 工具 2/4：读原文（按 ID，只读一条）
# ---------------------------------------------------------------------------


@mcp.tool()
def read_raw_data(record_id: str, max_chars: int = 0) -> str:
    """按记录 ID 读取**单条**记忆的完整原文。

    只能对 `search_memory` 已经定位到的具体 ID 调用。
    **不要用它来遍历历史** —— 那正好是本系统要避免的行为。

    Args:
        record_id: 记录 ID，形如 `D_0001`。
        max_chars: 可选。>0 时只返回前 N 个字符（长文先看开头再决定）。

    Returns:
        JSON：{ok, id, type, title, content, size_bytes, truncated, relations}
    """
    try:
        record = memory_core.read_raw_data(
            record_id, max_chars=max_chars or None
        )
    except RecordNotFound as exc:
        return _err(
            exc,
            hint="ID 可能记错了。先用 search_memory 找到正确的 ID，不要猜。",
        )
    except (InvalidRecordId, MemoryRouterError) as exc:
        return _err(exc, hint="record_id 必须形如 D_0001（字母开头，字母/数字/下划线）。")

    try:
        relation_info = memory_core.relations_for(record_id)
    except MemoryRouterError:
        relation_info = {"outgoing": [], "incoming": []}

    return _ok(
        {
            "id": record["id"],
            "type": record["type"],
            "title": record["title"],
            "keywords": record["keywords"],
            "created_at": record["created_at"],
            "update_version": record["update_version"],
            "size_bytes": record["size_bytes"],
            "truncated": record["truncated"],
            "content": record["content"],
            "relations": relation_info,
        }
    )


# ---------------------------------------------------------------------------
# 工具 3/4：写入记忆
# ---------------------------------------------------------------------------


@mcp.tool()
def save_memory(
    type: str,
    title: str,
    content: str,
    keywords: str = "",
    relates_to: str = "",
) -> str:
    """把一条新的记忆写入长期库。

    **什么时候必须调用**（Rule 里也有同样的约束）：
    - 做了一个影响后续架构/方案的决策
    - 完成了一次代码修改，且改动原因从代码本身看不出来
    - 排查清楚了一个非显而易见的缺陷
    - 得到了一个以后会反复用到的结论

    Args:
        type: 记录类型。decision / code_change / issue / note / reference。
        title: **一行摘要**，要能脱离原文独立读懂（例：「改用独立浏览器上下文修复跨站污染」）。
        content: 完整原文。建议包含：背景、做了什么、为什么这样做、还有什么没做。
        keywords: 逗号分隔的关键词，用于以后检索（例：playwright,context,假阴性）。
        relates_to: 可选，关联到已有记录，格式 `ID:关系类型`（例：`D_0002:supersedes`）。

    Returns:
        JSON：{ok, id, ...}。拿到的 id 可以用于后续 relations。
    """
    relations: list[tuple[str, str]] = []
    if relates_to.strip():
        raw = relates_to.strip()
        if ":" not in raw:
            return _err(
                MemoryRouterError("relates_to 格式应为 `ID:关系类型`"),
                hint="例如 `D_0002:supersedes`。",
            )
        target_id, relation_type = raw.split(":", 1)
        relations.append((target_id.strip(), relation_type.strip()))

    try:
        record = memory_core.add_entity(
            type=type,
            title=title,
            content=content,
            keywords=keywords,
            relations=relations,
        )
    except MemoryRouterError as exc:
        return _err(exc, hint="检查 title 是否为空、relates_to 的目标是否存在。")

    return _ok(
        {
            "id": record["id"],
            "type": record["type"],
            "title": record["title"],
            "file_path": record["file_path"],
        }
    )


# ---------------------------------------------------------------------------
# 工具 4/4：连接两条记忆
# ---------------------------------------------------------------------------


@mcp.tool()
def link_memory(source_id: str, target_id: str, relation_type: str) -> str:
    """建立两条记忆之间的有向关系。

    Args:
        source_id: 起点 ID，如 `D_0003`。
        target_id: 终点 ID，如 `D_0001`。
        relation_type: 关系名，如 supersedes / caused_by / related_to / implements。

    Returns:
        JSON：{ok, source_id, target_id, relation_type}
    """
    try:
        info = memory_core.add_relation(source_id, target_id, relation_type)
    except (RecordNotFound, InvalidRecordId, MemoryRouterError) as exc:
        return _err(exc, hint="两个 ID 都必须已存在；先用 search_memory 确认。")
    return _ok(info)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def check() -> int:
    """自检：依赖、数据库、四个工具是否正常注册。"""
    memory_core.configure_console()
    info = memory_core.init_db()
    print("✅ MCP Server 自检通过")
    print(f"   数据库：{info['db_path']}")
    print(f"   原文目录：{info['raw_dir']}")
    stats = memory_core.stats()
    print(f"   实体 {stats['entities']} 条 / 关系 {stats['relations']} 条")
    print("   工具：search_memory, read_raw_data, save_memory, link_memory")
    return 0


def main(argv: list[str] | None = None) -> int:
    memory_core.configure_console()
    parser = argparse.ArgumentParser(description="Agent 本地记忆路由 MCP Server")
    parser.add_argument(
        "--check", action="store_true", help="只自检，不启动服务"
    )
    parser.add_argument(
        "--transport",
        default="stdio",
        choices=["stdio", "sse", "streamable-http"],
        help="传输方式，默认 stdio（供本地 Agent 调用）",
    )
    args = parser.parse_args(argv)

    if args.check:
        return check()

    memory_core.init_db()
    mcp.run(transport=args.transport)
    return 0


if __name__ == "__main__":
    sys.exit(main())
