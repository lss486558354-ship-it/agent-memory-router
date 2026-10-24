# 项目目标与验收标准

> 记录于 2026-10-24。本文件是项目的**需求原点**，后续改动若与此冲突，先回来对齐。

## 一句话目标

不只是造一个工具，而是把「外部索引 + 按需加载」固化为 Agent 的底层运行规则（Rule）
和标准作业技能（Skill），解决长任务场景下的上下文截断、遗忘与幻觉。

## 简历定位

> 独立开发基于 MCP 的 Agent 专属记忆路由系统，通过配置自定义 Rule 与 Skill，
> 强制 Agent 采用「外部索引 + 按需加载」机制，彻底解决长任务场景下的上下文遗忘问题。

## 五个模块的验收标准

| # | 模块 | 验收标准 | 对应文件 | 状态 |
|---|---|---|---|---|
| 1 | 底层数据库 | SQLite 两张表：`entities`(id, type, title, keywords, file_path, created_at, update_version) / `relations`(source_id, target_id, relation_type)；原文落 `raw_data/<id>/raw.txt` | `init_db.py` + `memory_core.py` | ✅ |
| 2 | MCP Server | `search_memory(keyword, type=None)` 查表返回摘要；`read_raw_data(record_id)` 按 ID 拉全文 | `mcp_server.py` | ✅ |
| 3 | 人机 CLI | `list` 列记录、`search` 搜关键词、`show` 看原文，复用同一套连接 | `cli.py` | ✅ |
| 4 | 约束规则 | 禁止盲读全量上下文；历史问题必须先 `search_memory`；确认相关后再 `read_raw_data`；重大决策/代码修改必须写回 `entities` | `CLAUDE.md` + `.cursorrules` | ✅ |
| 5 | 作业技能 | Markdown SOP：如何写入新记忆、如何精准检索 | `.claude/skills/memory-manager/SKILL.md` | ✅ |

## 超出原始需求的补充（以及为什么）

| 补充 | 原因 |
|---|---|
| 额外两个 MCP 工具 `save_memory` / `link_memory` | 规则 4 要求 Agent 自动写回记忆，只有两个只读工具无法闭环 |
| `update_version` 自增 | 让「这条记忆被改过几次」可判断 |
| `relations` 外键 + 复合主键 | 防止悬空关系与重复边 |
| ID 白名单 + 路径归属校验 | `record_id` 来自模型输出，必须假设可被提示词注入 |
| LIKE 通配符转义 | 否则搜 `50%` 会返回全库 |
| 原文 2 MB 上限、检索 50 条上限 | 参数越界时夹住，而不是把上下文撑爆 |
| 29 个离线测试 | 把上述承诺固化成可回归的断言 |

## 明确不做的事

- 不引入向量库 / embedding：本地零依赖、结果可解释优先
- 不做 Web UI：Agent 走 MCP，人走 CLI
- 不做多用户与权限系统：单机单用户
- 不提供删除命令：误删记忆的代价高于留噪声（需要时直接操作 SQLite）

## 验收命令

```bash
python init_db.py --stats        # 建库并打印统计
python -m pytest tests/ -q       # 期望 29 passed
python mcp_server.py --check     # 期望 4 个工具注册成功
python cli.py add --type note --title "验收测试" --content "hello"
python cli.py search 验收
python cli.py show <返回的ID>
```
