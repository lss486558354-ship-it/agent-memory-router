"""memory_core 的行为测试。

这些用例守的是**系统的核心承诺**：
1. 索引只返回摘要，原文必须按 ID 单独取
2. 记录 ID 不能变成文件系统路径（目录穿越）
3. 检索通配符必须被转义（否则 `50%` 会搜出整个库）
4. 越界参数被夹住，而不是悄悄放大
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import memory_core as core  # noqa: E402


@pytest.fixture()
def memory_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把存储根目录指到临时目录。

    memory_core 的路径是模块级常量，所以必须同时改四个，
    并重置 `_schema_ready` —— 否则第二个用例会以为表已经建好了。
    """
    data_dir = tmp_path / "data"
    raw_dir = tmp_path / "raw_data"
    monkeypatch.setattr(core, "ROOT", tmp_path)
    monkeypatch.setattr(core, "DATA_DIR", data_dir)
    monkeypatch.setattr(core, "RAW_DIR", raw_dir)
    monkeypatch.setattr(core, "DB_PATH", data_dir / "memory.db")
    monkeypatch.setattr(core, "_schema_ready", False)
    core.init_db()
    return tmp_path


def add(**overrides: object) -> dict:
    payload = {
        "type": "decision",
        "title": "默认摘要",
        "content": "默认正文",
        "keywords": "alpha,beta",
    }
    payload.update(overrides)
    return core.add_entity(**payload)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 写入与读取
# ---------------------------------------------------------------------------


def test_add_writes_index_and_raw_file(memory_home: Path) -> None:
    record = add(title="选用 SQLite", content="完整背景在这里")

    assert record["id"] == "D_0001"
    assert record["type"] == "decision"
    assert record["update_version"] == 1

    raw_file = memory_home / "raw_data" / "D_0001" / "raw.txt"
    assert raw_file.exists()
    assert raw_file.read_text(encoding="utf-8") == "完整背景在这里"


def test_ids_increment_per_type(memory_home: Path) -> None:
    assert add(type="decision")["id"] == "D_0001"
    assert add(type="decision")["id"] == "D_0002"
    # 不同类型各自计数，互不干扰
    assert add(type="code_change")["id"] == "C_0001"
    assert add(type="issue")["id"] == "I_0001"
    # 未知类型走 DEFAULT_PREFIX
    assert add(type="weird-type")["id"] == "M_0001"


def test_unicode_round_trip(memory_home: Path) -> None:
    text = "中文、emoji ✅、以及 Windows 路径 D:\\gywlw\\x"
    record = add(content=text)

    fetched = core.read_raw_data(record["id"])
    assert fetched["content"] == text


def test_empty_title_is_rejected(memory_home: Path) -> None:
    with pytest.raises(core.MemoryRouterError):
        add(title="   ")


def test_duplicate_explicit_id_is_rejected(memory_home: Path) -> None:
    add(record_id="D_0001")
    with pytest.raises(core.DuplicateRecord):
        add(record_id="D_0001")


# ---------------------------------------------------------------------------
# 检索：只返回摘要
# ---------------------------------------------------------------------------


def test_search_does_not_return_raw_content(memory_home: Path) -> None:
    """这是系统的核心承诺：检索结果里不能夹带正文。"""
    add(title="关于缓存的决定", content="这是一段很长的正文" * 100)

    results = core.search_memory("缓存")

    assert len(results) == 1
    assert "content" not in results[0]
    assert "file_path" in results[0]


def test_search_matches_title_keywords_and_id(memory_home: Path) -> None:
    add(title="缓存策略", keywords="redis,ttl")
    add(title="完全不相关的内容", keywords="other")

    assert len(core.search_memory("缓存")) == 1
    assert len(core.search_memory("redis")) == 1
    assert len(core.search_memory("D_0001")) == 1
    assert len(core.search_memory("找不到的词")) == 0


def test_search_filters_by_type(memory_home: Path) -> None:
    add(type="decision", title="共同关键词")
    add(type="issue", title="共同关键词")

    assert len(core.search_memory("共同")) == 2
    assert len(core.search_memory("共同", type="decision")) == 1


def test_like_wildcards_are_escaped(memory_home: Path) -> None:
    """不转义的话，搜 `%` 会返回整个库 —— 搜索结果看起来"很全"，其实全错。"""
    add(title="折扣 50%")
    add(title="完全无关")

    results = core.search_memory("50%")

    assert len(results) == 1
    assert results[0]["title"] == "折扣 50%"


def test_underscore_is_escaped(memory_home: Path) -> None:
    add(title="a_b")
    add(title="axb")

    results = core.search_memory("a_b")

    assert len(results) == 1
    assert results[0]["title"] == "a_b"


def test_search_limit_is_clamped(memory_home: Path) -> None:
    for i in range(5):
        add(title=f"记录 {i}")

    assert len(core.search_memory("", limit=2)) == 2
    # 超过上限也不会真的返回一万条
    assert len(core.search_memory("", limit=10_000)) == 5


def test_empty_keyword_returns_recent(memory_home: Path) -> None:
    add(title="第一条")
    add(title="第二条")

    results = core.search_memory("")

    assert {r["title"] for r in results} == {"第一条", "第二条"}


# ---------------------------------------------------------------------------
# 安全：目录穿越
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_id",
    [
        "../../etc/passwd",
        "D_0001/../../secret",
        "..\\..\\windows\\win.ini",
        "D_0001/raw.txt",
        "a b",
        "1leading_digit",
        "",
        "A" * 101,
    ],
)
def test_path_traversal_ids_are_rejected(memory_home: Path, bad_id: str) -> None:
    add()  # 确保库里有记录，排除「因为库空才失败」

    with pytest.raises(core.MemoryRouterError):
        core.read_raw_data(bad_id)


def test_valid_id_still_works(memory_home: Path) -> None:
    record = add()

    assert core.read_raw_data(record["id"])["id"] == record["id"]


# ---------------------------------------------------------------------------
# 大小上限
# ---------------------------------------------------------------------------


def test_oversized_raw_is_refused(
    memory_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """超过上限就拒绝整篇加载 —— 否则等于把上下文炸弹放进来了。"""
    record = add(content="x" * 500)
    monkeypatch.setattr(core, "MAX_RAW_BYTES", 100)

    with pytest.raises(core.MemoryRouterError):
        core.read_raw_data(record["id"])


def test_max_chars_truncates(memory_home: Path) -> None:
    record = add(content="0123456789")

    result = core.read_raw_data(record["id"], max_chars=4)

    assert result["content"] == "0123"
    assert result["truncated"] is True


# ---------------------------------------------------------------------------
# 更新与关系
# ---------------------------------------------------------------------------


def test_update_increments_version_and_rewrites_raw(memory_home: Path) -> None:
    record = add(content="旧内容")

    updated = core.update_entity(
        record["id"], title="新摘要", content="新内容"
    )

    assert updated["update_version"] == 2
    assert updated["title"] == "新摘要"
    assert core.read_raw_data(record["id"])["content"] == "新内容"


def test_update_without_content_keeps_raw(memory_home: Path) -> None:
    record = add(content="原始内容")

    core.update_entity(record["id"], title="只改标题")

    assert core.read_raw_data(record["id"])["content"] == "原始内容"


def test_relations_round_trip(memory_home: Path) -> None:
    first = add(title="旧方案")
    second = add(title="新方案")

    core.add_relation(second["id"], first["id"], "supersedes")

    outgoing = core.relations_for(second["id"])["outgoing"]
    incoming = core.relations_for(first["id"])["incoming"]
    assert outgoing == [
        {"target_id": first["id"], "relation_type": "supersedes"}
    ]
    assert incoming == [
        {"source_id": second["id"], "relation_type": "supersedes"}
    ]


def test_relation_to_missing_record_is_rejected(memory_home: Path) -> None:
    record = add()

    with pytest.raises(core.RecordNotFound):
        core.add_relation(record["id"], "D_9999", "related_to")


def test_self_relation_is_rejected(memory_home: Path) -> None:
    record = add()

    with pytest.raises(core.MemoryRouterError):
        core.add_relation(record["id"], record["id"], "related_to")


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------


def test_stats_counts(memory_home: Path) -> None:
    first = add(type="decision")
    second = add(type="issue")
    core.add_relation(second["id"], first["id"], "caused_by")

    info = core.stats()

    assert info["entities"] == 2
    assert info["relations"] == 1
    assert info["by_type"] == {"decision": 1, "issue": 1}
