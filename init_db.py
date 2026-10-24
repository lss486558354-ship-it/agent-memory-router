"""初始化记忆库。

    python init_db.py
    python init_db.py --stats

可重复执行：所有 DDL 都是 IF NOT EXISTS，不会覆盖已有数据。
"""

from __future__ import annotations

import argparse
import json
import sys

import memory_core


def main(argv: list[str] | None = None) -> int:
    memory_core.configure_console()
    parser = argparse.ArgumentParser(description="初始化 Agent 本地记忆库")
    parser.add_argument(
        "--stats", action="store_true", help="初始化后打印统计信息"
    )
    parser.add_argument(
        "--json", action="store_true", help="以 JSON 输出（供脚本调用）"
    )
    args = parser.parse_args(argv)

    info = memory_core.init_db()

    if args.stats:
        info.update(memory_core.stats())

    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0

    print("✅ 记忆库已就绪")
    print(f"   索引：{info['db_path']}")
    print(f"   原文：{info['raw_dir']}")

    if args.stats:
        print(f"   实体：{info['entities']} 条，关系：{info['relations']} 条")
        by_type = info.get("by_type") or {}
        if by_type:
            detail = "，".join(f"{k} {v}" for k, v in by_type.items())
            print(f"   分布：{detail}")
        else:
            print("   （库里还没有任何记忆）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
