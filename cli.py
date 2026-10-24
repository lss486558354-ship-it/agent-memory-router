"""人机 CLI —— 复用 memory_core，不重复实现任何存储逻辑。

    python cli.py init
    python cli.py list --type decision
    python cli.py search playwright
    python cli.py show D_0001
    python cli.py add --type decision --title "..." --content-file note.md
    python cli.py link D_0002 D_0001 supersedes
    python cli.py stats

所有命令都支持 `--json`，方便脚本化。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import memory_core

TITLE_WIDTH = 58


def _short(text: str, width: int = TITLE_WIDTH) -> str:
    text = (text or "").replace("\n", " ").strip()
    if len(text) <= width:
        return text
    return text[: width - 1] + "…"


def _print_table(records: list[dict[str, Any]]) -> None:
    if not records:
        print("（无记录）")
        return
    print(f"{'ID':<10} {'类型':<13} {'版本':>4}  {'创建时间':<20} 摘要")
    print("-" * 100)
    for r in records:
        print(
            f"{r['id']:<10} {r['type']:<13} {r['update_version']:>4}  "
            f"{r['created_at'][:19]:<20} {_short(r['title'])}"
        )
    print("-" * 100)
    print(f"共 {len(records)} 条")


def _emit(payload: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


def _read_content(args: argparse.Namespace) -> str:
    """从 --content / --content-file / stdin 三者之一取正文。"""
    sources = [
        bool(args.content),
        bool(args.content_file),
        bool(args.stdin),
    ]
    if sum(sources) > 1:
        raise memory_core.MemoryRouterError(
            "--content / --content-file / --stdin 只能三选一。"
        )

    if args.content_file:
        path = Path(args.content_file)
        if not path.exists():
            raise memory_core.MemoryRouterError(f"文件不存在：{path}")
        return path.read_text(encoding="utf-8")
    if args.stdin:
        return sys.stdin.read()
    return args.content or ""


# ---------------------------------------------------------------------------
# 命令
# ---------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    info = memory_core.init_db()
    _emit(info, args.json)
    if not args.json:
        print("✅ 记忆库已就绪")
        print(f"   索引：{info['db_path']}")
        print(f"   原文：{info['raw_dir']}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    records = memory_core.list_entities(
        args.type, limit=args.limit, offset=args.offset
    )
    _emit(records, args.json)
    if not args.json:
        _print_table(records)
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    records = memory_core.search_memory(
        args.keyword, args.type, limit=args.limit
    )
    _emit(records, args.json)
    if not args.json:
        if not records:
            print(f"没有命中「{args.keyword}」。")
            print("试试更宽泛的关键词，或去掉 --type 过滤。")
            return 0
        _print_table(records)
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    record = memory_core.read_raw_data(
        args.record_id, max_chars=args.max_chars or None
    )
    relations = memory_core.relations_for(args.record_id)

    if args.json:
        _emit({**record, "relations": relations}, True)
        return 0

    print(f"ID        : {record['id']}")
    print(f"类型      : {record['type']}")
    print(f"摘要      : {record['title']}")
    print(f"关键词    : {record['keywords'] or '（无）'}")
    print(f"创建时间  : {record['created_at']}")
    print(f"版本      : {record['update_version']}")
    print(f"原文大小  : {record['size_bytes']:,} 字节")
    print(f"文件      : {record['file_path']}")

    out = relations["outgoing"]
    inc = relations["incoming"]
    if out:
        print("指向      : " + "，".join(f"{r['target_id']}({r['relation_type']})" for r in out))
    if inc:
        print("被指向    : " + "，".join(f"{r['source_id']}({r['relation_type']})" for r in inc))

    print("-" * 100)
    print(record["content"])
    if record["truncated"]:
        print(f"\n…（已按 --max-chars 截断，原文共 {record['size_bytes']:,} 字节）")
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    content = _read_content(args)
    relations: list[tuple[str, str]] = []
    for raw in args.relate or []:
        if ":" not in raw:
            raise memory_core.MemoryRouterError(
                f"--relate 格式应为 ID:关系类型，收到 {raw!r}"
            )
        target, relation_type = raw.split(":", 1)
        relations.append((target.strip(), relation_type.strip()))

    record = memory_core.add_entity(
        type=args.type,
        title=args.title,
        content=content,
        keywords=args.keywords,
        record_id=args.id,
        relations=relations,
    )
    _emit(record, args.json)
    if not args.json:
        print(f"✅ 已写入 {record['id']}")
        print(f"   摘要：{record['title']}")
        print(f"   原文：{record['file_path']}")
    return 0


def cmd_link(args: argparse.Namespace) -> int:
    info = memory_core.add_relation(
        args.source_id, args.target_id, args.relation_type
    )
    _emit(info, args.json)
    if not args.json:
        print(
            f"✅ {info['source_id']} --[{info['relation_type']}]--> {info['target_id']}"
        )
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    content = None
    if args.content or args.content_file or args.stdin:
        content = _read_content(args)
    record = memory_core.update_entity(
        args.record_id,
        title=args.title,
        keywords=args.keywords,
        content=content,
    )
    _emit(record, args.json)
    if not args.json:
        print(f"✅ 已更新 {record['id']} → 版本 {record['update_version']}")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    info = memory_core.stats()
    _emit(info, args.json)
    if not args.json:
        print(f"实体：{info['entities']} 条")
        print(f"关系：{info['relations']} 条")
        print(f"索引：{info['db_path']}")
        print(f"原文：{info['raw_dir']}")
        if info["by_type"]:
            print("\n按类型：")
            for type_name, count in info["by_type"].items():
                print(f"  {type_name:<14} {count}")
    return 0


# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    # `--json` 必须挂在每个子命令上，不能只挂在主 parser：
    # argparse 的子命令有自己的 namespace，写在 `cli.py list --json` 末尾时
    # 主 parser 根本收不到。用 parents 共享一份定义，避免逐条复制。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="以 JSON 输出")

    parser = argparse.ArgumentParser(
        description="Agent 本地记忆路由 —— 命令行工具"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", parents=[common], help="初始化数据库")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("list", parents=[common], help="列出记录")
    p.add_argument("--type", help="按类型过滤")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--offset", type=int, default=0)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("search", parents=[common], help="按关键词搜索")
    p.add_argument("keyword", nargs="?", default="")
    p.add_argument("--type", help="按类型过滤")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("show", parents=[common], help="查看单条记录原文")
    p.add_argument("record_id")
    p.add_argument("--max-chars", type=int, default=0, help="只显示前 N 个字符")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("add", parents=[common], help="新增一条记忆")
    p.add_argument("--type", required=True)
    p.add_argument("--title", required=True, help="一行摘要")
    p.add_argument("--keywords", default="", help="逗号分隔")
    p.add_argument("--content", default="", help="正文（短内容直接用）")
    p.add_argument("--content-file", help="从文件读取正文")
    p.add_argument("--stdin", action="store_true", help="从标准输入读取正文")
    p.add_argument("--id", help="指定 ID（默认自动生成）")
    p.add_argument(
        "--relate",
        action="append",
        metavar="ID:关系",
        help="建立关系，可重复，如 --relate D_0001:supersedes",
    )
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("link", parents=[common], help="连接两条记录")
    p.add_argument("source_id")
    p.add_argument("target_id")
    p.add_argument("relation_type")
    p.set_defaults(func=cmd_link)

    p = sub.add_parser("update", parents=[common], help="更新一条记录（版本号自增）")
    p.add_argument("record_id")
    p.add_argument("--title")
    p.add_argument("--keywords")
    p.add_argument("--content", default="")
    p.add_argument("--content-file")
    p.add_argument("--stdin", action="store_true")
    p.set_defaults(func=cmd_update)

    p = sub.add_parser("stats", parents=[common], help="统计信息")
    p.set_defaults(func=cmd_stats)

    return parser


def main(argv: list[str] | None = None) -> int:
    memory_core.configure_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except memory_core.MemoryRouterError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
