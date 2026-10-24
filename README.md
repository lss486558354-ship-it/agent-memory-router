# Agent Memory Router

> 基于 MCP 的 Agent 本地记忆路由系统 —— 先检索摘要，再按需拉取原文。
> 让 Agent 拥有长期记忆，同时不撑爆上下文窗口。

[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-2.0-green)](https://modelcontextprotocol.io/)
[![License](https://img.shields.io/badge/license-MIT-lightgrey)](LICENSE)

---

## 问题

Agent 做长期项目时，历史信息只有两种存法：

| 做法 | 代价 |
|---|---|
| 全部塞进上下文 | 上下文窗口有限，越往后越贵、越容易丢关键信息 |
| 不存，每次重新查 | 同样的坑踩第二遍，同样的方案重新讨论一遍 |

这个项目提供第三种：**把历史压成一张小索引，原文留在磁盘上**。
Agent 先用关键词在索引里搜（每次返回几百字节），
确认相关后再按 ID 取那一条的完整原文。

实测：检索 1000 条记录和检索 10 条记录，返回量完全相同 ——
因为搜索只查索引，不碰原文。

## 架构

```
                  ┌────────────────────────────────┐
                  │      Agent (Claude / Cursor)   │
                  └───────────────┬────────────────┘
                                  │ MCP (stdio)
                  ┌───────────────▼────────────────┐
                  │          mcp_server.py         │
                  │ search / read_raw / save / link│
                  └───────────────┬────────────────┘
                                  │
                  ┌───────────────▼────────────────┐
                  │          memory_core.py        │
                  │  唯一一份 schema 与访问逻辑      │
                  └───────┬────────────────┬───────┘
                          │                │
              ┌───────────▼──────┐  ┌──────▼─────────────────┐
              │ data/memory.db   │  │ raw_data/<id>/raw.txt  │
              │ 索引：小、常驻    │  │ 原文：大、按需加载       │
              │ 可全表扫描        │  │ 不进入上下文直到被点名    │
              └──────────────────┘  └────────────────────────┘

              init_db.py  ── 初始化
              cli.py      ── 人用
              mcp_server.py ── Agent 用
                    ↑ 三者共用 memory_core，没有第二套存储逻辑
```

## 目录结构

```
agent-memory-router/
├── memory_core.py              # 存储核心：schema、连接、增删查改
├── mcp_server.py               # MCP Server（4 个工具）
├── cli.py                      # 命令行（8 个子命令）
├── init_db.py                  # 初始化
├── CLAUDE.md                   # Claude Code 强制规则
├── .cursorrules                # Cursor 强制规则
├── .claude/skills/
│   └── memory-manager/
│       └── SKILL.md            # 检索/写入的标准作业流程
├── tests/
│   └── test_memory_core.py     # 29 个离线测试
├── data/memory.db              # 索引（gitignore）
└── raw_data/<id>/raw.txt       # 原文（gitignore）
```

## 快速开始

```bash
git clone https://github.com/<your-name>/agent-memory-router.git
cd agent-memory-router
pip install -r requirements.txt

python init_db.py --stats          # 建库
python cli.py add --type decision \
  --title "选 SQLite 而非向量库" \
  --keywords "sqlite,检索,本地" \
  --content "背景：……
做法：……
原因：……
遗留：……"

python cli.py search sqlite
python cli.py show D_0001
```

### 接入 Claude Code

在项目根目录建 `.mcp.json`（Windows 请用绝对路径）：

```json
{
  "mcpServers": {
    "memory-router": {
      "command": "python",
      "args": ["D:/agent-memory-router/mcp_server.py"]
    }
  }
}
```

或用命令行注册：

```bash
claude mcp add memory-router -- python D:/agent-memory-router/mcp_server.py
```

### 接入 Cursor

`.cursor/mcp.json` 格式相同。规则文件 `.cursorrules` 已在仓库根目录。

## MCP 工具

| 工具 | 作用 | 返回量 |
|---|---|---|
| `search_memory(keyword, type?, limit?)` | 在索引中检索 | 摘要 × N（每条约 200 字节） |
| `read_raw_data(record_id, max_chars?)` | 按 ID 取单条原文 | 一条原文 |
| `save_memory(type, title, content, keywords?, relates_to?)` | 写入新记忆 | 新记录 ID |
| `link_memory(source_id, target_id, relation_type)` | 建立有向关系 | 关系确认 |

所有工具返回 **JSON 字符串**而不是裸文本：模型更容易区分
「摘要」和「原文」，出错时也知道下一步该干什么。

## 数据模型

```sql
CREATE TABLE entities (
    id             TEXT PRIMARY KEY,   -- D_0001 / C_0002，前缀表示类型
    type           TEXT NOT NULL,      -- decision / code_change / issue / ...
    title          TEXT NOT NULL,      -- 一行摘要，检索时唯一可见的内容
    keywords       TEXT NOT NULL,      -- 逗号分隔
    file_path      TEXT NOT NULL,      -- 指向 raw_data/<id>/raw.txt
    created_at     TIMESTAMP NOT NULL,
    update_version INTEGER NOT NULL
);

CREATE TABLE relations (
    source_id     TEXT NOT NULL,
    target_id     TEXT NOT NULL,
    relation_type TEXT NOT NULL,       -- supersedes / caused_by / implements
    PRIMARY KEY (source_id, target_id, relation_type)
);
```

ID 前缀：`D` 决策、`C` 代码修改、`I` 问题、`N` 笔记、`R` 参考资料、`S` 会话、`M` 其他。
扫日志时一眼能看出这条是什么。

## 设计取舍

**为什么索引和原文分离？**
因为两者访问频率差了两个数量级。索引每次对话都要查，原文可能一个月读一次。
放在一起意味着每次检索都要拖着一堆正文走。

**为什么用 SQLite 而不是向量库？**
本地零依赖、单文件、可以直接 `sqlite3` 打开排查。
关键词检索对「记得大概叫什么」的场景已经够用，
而它的失败模式是**可解释的**（没匹配到就是没匹配到），
不像向量检索会返回一堆"语义相近但无关"的结果，反而浪费上下文。

**为什么 ID 用 `D_0001` 而不是 UUID？**
UUID 在日志里无法辨认。前缀 + 序号让「哦这是第 3 个决策」变成零成本判断。

**安全：record_id 直接来自模型输出，模型可能被提示词注入。**
所以有两道防线：

1. 白名单正则 `^[A-Za-z][A-Za-z0-9_]{0,63}$` —— 挡掉 `/`、`..`、空格
2. `Path.is_relative_to()` 二次校验 —— 即使将来正则被放宽，也越不出 `raw_data/`

测试里覆盖了 `../../etc/passwd`、`D_0001/../../secret`、Windows 反斜杠等 8 种变体。

**检索通配符必须转义。**
不转义的话搜 `50%` 会变成「匹配一切」，返回全库 —— 结果看起来"很全"，其实全错。

**每次操作单开 SQLite 连接。**
建连接是微秒级开销，换来的是完全不用考虑线程安全 ——
MCP server 可能在任意线程里调工具。WAL 模式保证 CLI 写入时 MCP 仍可读。

**原文先写 `.tmp` 再 `rename`。**
中途崩溃不会留下半截文件；入库失败会删掉刚写的原文，不留孤儿。

## 测试

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

29 个用例覆盖：写入/读取、按类型生成 ID、Unicode 往返、
LIKE 通配符转义、8 种目录穿越变体、大小上限、截断、
版本自增、关系往返、外键拒绝。

## 局限

- **只有关键词检索**，没有中文分词和向量召回。搜「上下文」能命中「独立浏览器上下文」，
  但搜「记忆溢出」不会命中「上下文膨胀」。缓解办法：keywords 里多写几种说法。
- **单机单用户**。多进程同时写靠 SQLite 的锁，没有做分布式。
- **没有删除命令**（有意为之：误删记忆的代价高于留一点噪声）。需要清理时直接操作 SQLite。
- 单条原文超过 2 MB 会被拒绝加载 —— 超过这个体量就该拆成多条记录了。

## License

MIT
