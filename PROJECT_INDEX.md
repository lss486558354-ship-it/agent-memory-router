# 项目索引：Agent Memory Router

> 最后更新：2026-10-24
> 本文件供 Agent 快速建立全局认知。**不需要读它以外的文件就能知道项目长什么样**，
> 具体实现再按需查看对应源码。

## 一、基本信息

| 项 | 值 |
|---|---|
| 定位 | 基于 MCP 的 Agent 本地长期记忆路由系统 |
| 语言 | Python 3.10+ |
| 运行时依赖 | `mcp>=2.0`（唯一） |
| 开发依赖 | `pytest>=8.0` |
| 存储 | SQLite（标准库 `sqlite3`）+ 本地文件 |
| 入口 | `init_db.py` / `cli.py` / `mcp_server.py` |
| 测试 | `tests/test_memory_core.py`，29 个用例 |
| License | MIT |
| 数据目录 | `data/memory.db`（索引）、`raw_data/<id>/raw.txt`（原文），均已 gitignore |

## 二、核心设计

一句话：**索引只存摘要，原文按 ID 单独取。**

```
Agent → search_memory → 摘要列表（~200 字节/条）→ 判断相关 → read_raw_data(单个 ID) → 推理
```

这条链路是项目的全部价值。任何改动如果让 Agent 能"顺手批量读原文"，
都等于破坏设计初衷。

## 三、目录结构与职责

| 文件 | 职责 | 不要在这里做什么 |
|---|---|---|
| `memory_core.py` | 唯一的 schema、连接、CRUD 实现 | —— |
| `mcp_server.py` | MCP 工具定义、参数校验、JSON 包装 | 不要写 SQL |
| `cli.py` | argparse 子命令、表格输出 | 不要写 SQL |
| `init_db.py` | 建库建表 | 不要写业务逻辑 |
| `CLAUDE.md` / `.cursorrules` | Agent 行为约束（四条铁律） | —— |
| `.claude/skills/memory-manager/SKILL.md` | 读写 SOP、反模式 | —— |
| `docs/GOAL.md` | 需求原点、五模块验收标准、明确不做的事 | —— |
| `tests/test_memory_core.py` | 行为测试 + 安全测试 | —— |

## 四、数据流

**写入**（`add_entity`）：

```
校验 title/content
  → 取下一个 ID（按前缀查最大序号，Python 内解析）
  → 原文写 raw_data/<id>/raw.txt.tmp，再 rename
  → INSERT INTO entities
  → 可选 INSERT INTO relations
  → 失败则删除刚写的原文和空目录
  → 返回 get_entity(id)
```

**检索**（`search_memory`）：

```
LIKE 转义 → 拼 WHERE（keyword 匹配 title/keywords/id，可选 type）
  → ORDER BY 标题命中优先, created_at DESC
  → LIMIT 夹在 1..50
  → 只返回 entities 表字段（不含原文）
```

**读取**（`read_raw_data`）：

```
validate ID（正则）→ 查 entities → 查 file_path 存在性
  → 大小 > 2MB 拒绝 → 读 UTF-8 → 可选按 max_chars 截断
```

## 五、关键常量与约定

| 名称 | 值 | 说明 |
|---|---|---|
| `MAX_RAW_BYTES` | 2 MiB | 超过则拒绝整篇加载 |
| `MAX_SEARCH_LIMIT` | 50 | 单次检索硬上限 |
| `DEFAULT_SEARCH_LIMIT` | 10 | 默认返回条数 |
| `_RECORD_ID_RE` | `^[A-Za-z][A-Za-z0-9_]{0,63}$` | 安全白名单 |
| `TYPE_PREFIXES` | decision→D, code_change→C, issue→I, note→N, session→S, reference→R | 未知类型用 M |
| `MEMORY_ROUTER_HOME` | 环境变量 | 覆盖数据根目录，测试与多项目隔离用 |

## 六、用户需求推断

- 目标是**简历项目**：独立开发、MCP、Agent 记忆路由 —— 所以 README 需要把
  设计取舍讲透，而不只是罗列 API。
- 用户要"极轻量"：不引入向量库、ORM、Web 框架，只依赖 MCP SDK。
- 用户明确要求规则文件和 Skill 文件，说明他关心**Agent 是否真的会照着做**，
  而不只是工具能不能跑。因此文档里强调"顺序不可颠倒"和反模式。
- 需要开源到 GitHub（public），所以不能把本地数据、密钥带进仓库。

## 七、编码习惯（本仓库体现的）

- 中文注释，解释**为什么**而不是**做了什么**
- 类型注解 + `from __future__ import annotations`
- 错误用自定义异常层级（`MemoryRouterError` → `RecordNotFound` / `InvalidRecordId` / `DuplicateRecord`）
- CLI 与 MCP 的错误统一转成用户可读提示
- 入口函数签名 `main(argv: list[str] | None = None) -> int`，便于测试
- 常量集中在模块顶部并带注释

## 八、修改历史

| 日期 | 变更 |
|---|---|
| 2026-10-24 | 初始实现：`memory_core.py`、`init_db.py`、`cli.py`、`mcp_server.py` |
| 2026-10-24 | 加入 29 个离线测试（含目录穿越、LIKE 转义、大小上限） |
| 2026-10-24 | 加入 `CLAUDE.md`、`.cursorrules`、`memory-manager` Skill |
| 2026-10-24 | 加入 README、LICENSE、pyproject、gitignore，准备开源 |
| 2026-10-24 | 加入 `docs/GOAL.md`，固化原始需求与验收标准 |

## 九、技术债务

| 项 | 影响 | 建议 |
|---|---|---|
| 无中文分词 | 搜「上下文膨胀」搜不到「上下文溢出」 | 依赖 keywords 多写同义词；后续可接 jieba 或 FTS5 |
| 无删除命令 | 误写入的记录只能手工改 SQLite | 若加，必须做成软删除 + 二次确认 |
| `update_entity` 覆盖写原文 | 旧版本不可追溯 | 需要留痕时新建记录 + `supersedes` 关系 |
| 单连接串行 | 高并发写入会等锁 | 当前单机单用户场景够用 |
| 无 MCP 集成测试 | 只验证了工具函数的返回结构 | 可加 `mcp` 客户端端到端测试 |

## 十、Agent 自检清单

开始工作前：

1. 需要历史信息 → `search_memory`，不要翻文件
2. 需要改存储逻辑 → 只改 `memory_core.py`，另外两个入口不复制 SQL
3. 改了 `_RECORD_ID_RE` 或路径拼接 → 必须重跑目录穿越测试
4. 完成非平凡修改 → `save_memory` 写一条 `code_change`
5. 提交前 → `python -m pytest tests/ -q` 全绿
