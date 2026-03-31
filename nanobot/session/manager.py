"""
会话管理模块，用于对话历史持久化。

该模块实现了 nanobot 的会话管理功能：
    - Session: 会话数据类，存储消息历史和元数据
    - SessionManager: 会话管理器，处理会话的加载、保存和缓存

会话特性：
    - 消息只追加，保证 LLM 缓存效率
    - 合并过程将摘要写入 MEMORY.md/HISTORY.md
    - 不修改 messages 列表或 get_history() 输出
    - 支持工具调用边界的合法性检查

存储格式：
    - JSONL 格式（每行一个 JSON 对象）
    - 第一行为元数据（_type: "metadata"）
    - 后续行为消息记录
"""

import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.config.paths import get_legacy_sessions_dir
from nanobot.utils.helpers import ensure_dir, safe_filename


@dataclass
class Session:
    """
    对话会话数据类。

    以 JSONL 格式存储消息，便于阅读和持久化。

    重要说明：
        消息只追加以保证 LLM 缓存效率。
        合并过程将摘要写入 MEMORY.md/HISTORY.md，
        但不修改 messages 列表或 get_history() 输出。

    属性：
        key: 会话键（格式：channel:chat_id）
        messages: 消息列表
        created_at: 创建时间
        updated_at: 更新时间
        metadata: 会话元数据
        last_consolidated: 已合并到文件的消息数量
    """

    key: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)
    last_consolidated: int = 0

    def add_message(self, role: str, content: str, **kwargs: Any) -> None:
        """
        添加消息到会话。

        参数：
            role: 消息角色（user/assistant/tool）
            content: 消息内容
            **kwargs: 额外的消息字段
        """
        msg = {
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
            **kwargs
        }
        self.messages.append(msg)
        self.updated_at = datetime.now()

    @staticmethod
    def _find_legal_start(messages: list[dict[str, Any]]) -> int:
        """
        查找第一个合法的起始索引，确保每个工具结果都有匹配的助手工具调用。

        某些 Provider 会拒绝孤立的工具结果（如果匹配的助手 tool_calls
        消息落在固定大小的历史窗口之外）。

        参数：
            messages: 消息列表

        返回：
            合法起始索引
        """
        declared: set[str] = set()
        start = 0
        for i, msg in enumerate(messages):
            role = msg.get("role")
            if role == "assistant":
                for tc in msg.get("tool_calls") or []:
                    if isinstance(tc, dict) and tc.get("id"):
                        declared.add(str(tc["id"]))
            elif role == "tool":
                tid = msg.get("tool_call_id")
                if tid and str(tid) not in declared:
                    start = i + 1
                    declared.clear()
                    for prev in messages[start:i + 1]:
                        if prev.get("role") == "assistant":
                            for tc in prev.get("tool_calls") or []:
                                if isinstance(tc, dict) and tc.get("id"):
                                    declared.add(str(tc["id"]))
        return start

    def get_history(self, max_messages: int = 500) -> list[dict[str, Any]]:
        """
        返回未合并的消息用于 LLM 输入，对齐到合法的工具调用边界。

        参数：
            max_messages: 最大返回消息数量

        返回：
            消息列表，每条消息包含 role、content 和可选的工具相关字段

        处理流程：
            1. 获取未合并的消息
            2. 截取最近的消息
            3. 跳过开头的非用户消息
            4. 确保工具调用边界合法
        """
        unconsolidated = self.messages[self.last_consolidated:]
        sliced = unconsolidated[-max_messages:]

        for i, message in enumerate(sliced):
            if message.get("role") == "user":
                sliced = sliced[i:]
                break

        start = self._find_legal_start(sliced)
        if start:
            sliced = sliced[start:]

        out: list[dict[str, Any]] = []
        for message in sliced:
            entry: dict[str, Any] = {"role": message["role"], "content": message.get("content", "")}
            for key in ("tool_calls", "tool_call_id", "name"):
                if key in message:
                    entry[key] = message[key]
            out.append(entry)
        return out

    def clear(self) -> None:
        """清空所有消息并重置会话到初始状态。"""
        self.messages = []
        self.last_consolidated = 0
        self.updated_at = datetime.now()

    def retain_recent_legal_suffix(self, max_messages: int) -> None:
        """
        保留合法的最近消息后缀，镜像 get_history 边界规则。

        参数：
            max_messages: 最大保留消息数量

        处理流程：
            1. 从末尾开始截取
            2. 如果截断点在对话中间，向后扩展到最近的用户轮次
            3. 确保工具调用边界合法
        """
        if max_messages <= 0:
            self.clear()
            return
        if len(self.messages) <= max_messages:
            return

        start_idx = max(0, len(self.messages) - max_messages)

        while start_idx > 0 and self.messages[start_idx].get("role") != "user":
            start_idx -= 1

        retained = self.messages[start_idx:]

        start = self._find_legal_start(retained)
        if start:
            retained = retained[start:]

        dropped = len(self.messages) - len(retained)
        self.messages = retained
        self.last_consolidated = max(0, self.last_consolidated - dropped)
        self.updated_at = datetime.now()


class SessionManager:
    """
    会话管理器，处理会话的加载、保存和缓存。

    会话以 JSONL 文件格式存储在 sessions 目录中。

    属性：
        workspace: 工作区路径
        sessions_dir: 会话存储目录
        legacy_sessions_dir: 旧版会话目录（用于迁移）
        _cache: 内存会话缓存
    """

    def __init__(self, workspace: Path):
        """
        初始化会话管理器。

        参数：
            workspace: 工作区根目录路径
        """
        self.workspace = workspace
        self.sessions_dir = ensure_dir(self.workspace / "sessions")
        self.legacy_sessions_dir = get_legacy_sessions_dir()
        self._cache: dict[str, Session] = {}

    def _get_session_path(self, key: str) -> Path:
        """
        获取会话文件路径。

        参数：
            key: 会话键

        返回：
            会话文件路径
        """
        safe_key = safe_filename(key.replace(":", "_"))
        return self.sessions_dir / f"{safe_key}.jsonl"

    def _get_legacy_session_path(self, key: str) -> Path:
        """
        获取旧版全局会话路径（~/.nanobot/sessions/）。

        参数：
            key: 会话键

        返回：
            旧版会话文件路径
        """
        safe_key = safe_filename(key.replace(":", "_"))
        return self.legacy_sessions_dir / f"{safe_key}.jsonl"

    def get_or_create(self, key: str) -> Session:
        """
        获取现有会话或创建新会话。

        参数：
            key: 会话键（通常是 channel:chat_id）

        返回：
            会话对象
        """
        if key in self._cache:
            return self._cache[key]

        session = self._load(key)
        if session is None:
            session = Session(key=key)

        self._cache[key] = session
        return session

    def _load(self, key: str) -> Session | None:
        """
        从磁盘加载会话。

        参数：
            key: 会话键

        返回：
            会话对象，不存在返回 None

        处理流程：
            1. 检查新版路径
            2. 如果不存在，尝试从旧版路径迁移
            3. 解析 JSONL 文件
        """
        path = self._get_session_path(key)
        if not path.exists():
            legacy_path = self._get_legacy_session_path(key)
            if legacy_path.exists():
                try:
                    shutil.move(str(legacy_path), str(path))
                    logger.info("Migrated session {} from legacy path", key)
                except Exception:
                    logger.exception("Failed to migrate session {}", key)

        if not path.exists():
            return None

        try:
            messages = []
            metadata = {}
            created_at = None
            last_consolidated = 0

            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue

                    data = json.loads(line)

                    if data.get("_type") == "metadata":
                        metadata = data.get("metadata", {})
                        created_at = datetime.fromisoformat(data["created_at"]) if data.get("created_at") else None
                        last_consolidated = data.get("last_consolidated", 0)
                    else:
                        messages.append(data)

            return Session(
                key=key,
                messages=messages,
                created_at=created_at or datetime.now(),
                metadata=metadata,
                last_consolidated=last_consolidated
            )
        except Exception as e:
            logger.warning("Failed to load session {}: {}", key, e)
            return None

    def save(self, session: Session) -> None:
        """
        保存会话到磁盘。

        参数：
            session: 要保存的会话对象

        存储格式：
            第一行为元数据，后续行为消息记录
        """
        path = self._get_session_path(session.key)

        with open(path, "w", encoding="utf-8") as f:
            metadata_line = {
                "_type": "metadata",
                "key": session.key,
                "created_at": session.created_at.isoformat(),
                "updated_at": session.updated_at.isoformat(),
                "metadata": session.metadata,
                "last_consolidated": session.last_consolidated
            }
            f.write(json.dumps(metadata_line, ensure_ascii=False) + "\n")
            for msg in session.messages:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")

        self._cache[session.key] = session

    def invalidate(self, key: str) -> None:
        """
        从内存缓存中移除会话。

        参数：
            key: 会话键
        """
        self._cache.pop(key, None)

    def list_sessions(self) -> list[dict[str, Any]]:
        """
        列出所有会话。

        返回：
            会话信息字典列表，按更新时间降序排列
        """
        sessions = []

        for path in self.sessions_dir.glob("*.jsonl"):
            try:
                with open(path, encoding="utf-8") as f:
                    first_line = f.readline().strip()
                    if first_line:
                        data = json.loads(first_line)
                        if data.get("_type") == "metadata":
                            key = data.get("key") or path.stem.replace("_", ":", 1)
                            sessions.append({
                                "key": key,
                                "created_at": data.get("created_at"),
                                "updated_at": data.get("updated_at"),
                                "path": str(path)
                            })
            except Exception:
                continue

        return sorted(sessions, key=lambda x: x.get("updated_at", ""), reverse=True)
