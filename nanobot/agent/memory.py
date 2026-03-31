"""
Agent 内存系统模块，提供持久化的长期记忆和历史记录功能。

该模块实现了 agent 的双层内存系统：
1. MEMORY.md - 长期记忆，存储重要的事实和知识
2. HISTORY.md - 历史记录，存储可搜索的对话日志

主要功能：
- 内存整合：将对话压缩为摘要并存储
- Token 管理：当上下文窗口不足时自动归档旧消息
- 会话管理：跟踪每个会话的整合进度
"""

from __future__ import annotations

import asyncio
import json
import weakref
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from loguru import logger

from nanobot.utils.helpers import ensure_dir, estimate_message_tokens, estimate_prompt_tokens_chain

if TYPE_CHECKING:
    from nanobot.providers.base import LLMProvider
    from nanobot.session.manager import Session, SessionManager


# save_memory 工具的定义，用于 LLM 调用以保存内存
_SAVE_MEMORY_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "save_memory",
            "description": "Save the memory consolidation result to persistent storage.",
            "parameters": {
                "type": "object",
                "properties": {
                    "history_entry": {
                        "type": "string",
                        "description": "A paragraph summarizing key events/decisions/topics. "
                        "Start with [YYYY-MM-DD HH:MM]. Include detail useful for grep search.",
                    },
                    "memory_update": {
                        "type": "string",
                        "description": "Full updated long-term memory as markdown. Include all existing "
                        "facts plus new ones. Return unchanged if nothing new.",
                    },
                },
                "required": ["history_entry", "memory_update"],
            },
        },
    }
]


def _ensure_text(value: Any) -> str:
    """
    将工具调用参数值规范化为文本格式，用于文件存储。

    Args:
        value: 原始值（可能是字符串或其他类型）

    Returns:
        规范化后的字符串
    """
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _normalize_save_memory_args(args: Any) -> dict[str, Any] | None:
    """
    将不同 LLM 提供商的工具调用参数规范化为统一的字典格式。

    不同提供商可能返回字符串、列表或字典格式的参数，
    该函数将其统一转换为字典。

    Args:
        args: 原始参数

    Returns:
        规范化后的参数字典，如果无法解析则返回 None
    """
    if isinstance(args, str):
        args = json.loads(args)
    if isinstance(args, list):
        return args[0] if args and isinstance(args[0], dict) else None
    return args if isinstance(args, dict) else None

# 用于检测 tool_choice 不支持的错误标记
_TOOL_CHOICE_ERROR_MARKERS = (
    "tool_choice",
    "toolchoice",
    "does not support",
    'should be ["none", "auto"]',
)


def _is_tool_choice_unsupported(content: str | None) -> bool:
    """
    检测是否是由于强制 tool_choice 不支持导致的错误。

    某些 LLM 提供商不支持强制调用特定工具，
    该函数用于检测这种情况以便回退到自动模式。

    Args:
        content: 错误消息内容

    Returns:
        如果是 tool_choice 不支持的错误则返回 True
    """
    text = (content or "").lower()
    return any(m in text for m in _TOOL_CHOICE_ERROR_MARKERS)


class MemoryStore:
    """
    双层内存存储：MEMORY.md（长期事实）+ HISTORY.md（可搜索日志）。

    该类负责管理 agent 的持久化内存，包括：
    - 长期记忆：存储重要的事实、知识和偏好
    - 历史记录：存储对话摘要，支持 grep 搜索

    内存整合流程：
    1. 收集需要整合的消息
    2. 调用 LLM 生成摘要和更新
    3. 将结果写入 MEMORY.md 和 HISTORY.md

    Attributes:
        memory_dir: 内存目录路径
        memory_file: 长期记忆文件路径
        history_file: 历史记录文件路径
        _consecutive_failures: 连续失败次数，用于触发降级策略
    """

    # 连续失败多少次后降级为原始归档
    _MAX_FAILURES_BEFORE_RAW_ARCHIVE = 3

    def __init__(self, workspace: Path):
        """
        初始化内存存储。

        Args:
            workspace: 工作空间路径，内存文件会存储在 workspace/memory/ 目录下
        """
        self.memory_dir = ensure_dir(workspace / "memory")
        self.memory_file = self.memory_dir / "MEMORY.md"
        self.history_file = self.memory_dir / "HISTORY.md"
        self._consecutive_failures = 0

    def read_long_term(self) -> str:
        """
        读取长期记忆内容。

        Returns:
            长期记忆的文本内容，如果文件不存在则返回空字符串
        """
        if self.memory_file.exists():
            return self.memory_file.read_text(encoding="utf-8")
        return ""

    def write_long_term(self, content: str) -> None:
        """
        写入长期记忆内容。

        Args:
            content: 要写入的内容
        """
        self.memory_file.write_text(content, encoding="utf-8")

    def append_history(self, entry: str) -> None:
        """
        追加历史记录条目。

        每个条目会以两个换行符分隔，便于阅读。

        Args:
            entry: 历史记录条目（通常以 [YYYY-MM-DD HH:MM] 开头）
        """
        with open(self.history_file, "a", encoding="utf-8") as f:
            f.write(entry.rstrip() + "\n\n")

    def get_memory_context(self) -> str:
        """
        获取用于注入到系统提示的内存上下文。

        Returns:
            格式化的内存上下文字符串
        """
        long_term = self.read_long_term()
        return f"## Long-term Memory\n{long_term}" if long_term else ""

    @staticmethod
    def _format_messages(messages: list[dict]) -> str:
        """
        将消息列表格式化为文本，用于 LLM 处理。

        格式：[时间] 角色 [工具]: 内容

        Args:
            messages: 消息列表

        Returns:
            格式化后的文本
        """
        lines = []
        for message in messages:
            if not message.get("content"):
                continue
            tools = f" [tools: {', '.join(message['tools_used'])}]" if message.get("tools_used") else ""
            lines.append(
                f"[{message.get('timestamp', '?')[:16]}] {message['role'].upper()}{tools}: {message['content']}"
            )
        return "\n".join(lines)

    async def consolidate(
        self,
        messages: list[dict],
        provider: LLMProvider,
        model: str,
    ) -> bool:
        """
        整合消息到 MEMORY.md 和 HISTORY.md。

        该方法会调用 LLM 来生成摘要和更新内存。
        如果 LLM 调用失败，会触发降级策略。

        Args:
            messages: 要整合的消息列表
            provider: LLM 提供商
            model: 使用的模型名称

        Returns:
            整合是否成功
        """
        if not messages:
            return True

        current_memory = self.read_long_term()
        prompt = f"""Process this conversation and call the save_memory tool with your consolidation.

## Current Long-term Memory
{current_memory or "(empty)"}

## Conversation to Process
{self._format_messages(messages)}"""

        chat_messages = [
            {"role": "system", "content": "You are a memory consolidation agent. Call the save_memory tool with your consolidation of the conversation."},
            {"role": "user", "content": prompt},
        ]

        try:
            # 尝试强制调用 save_memory 工具
            forced = {"type": "function", "function": {"name": "save_memory"}}
            response = await provider.chat_with_retry(
                messages=chat_messages,
                tools=_SAVE_MEMORY_TOOL,
                model=model,
                tool_choice=forced,
            )

            # 如果提供商不支持强制 tool_choice，回退到自动模式
            if response.finish_reason == "error" and _is_tool_choice_unsupported(
                response.content
            ):
                logger.warning("Forced tool_choice unsupported, retrying with auto")
                response = await provider.chat_with_retry(
                    messages=chat_messages,
                    tools=_SAVE_MEMORY_TOOL,
                    model=model,
                    tool_choice="auto",
                )

            # 检查 LLM 是否正确调用了 save_memory 工具
            if not response.has_tool_calls:
                logger.warning(
                    "Memory consolidation: LLM did not call save_memory "
                    "(finish_reason={}, content_len={}, content_preview={})",
                    response.finish_reason,
                    len(response.content or ""),
                    (response.content or "")[:200],
                )
                return self._fail_or_raw_archive(messages)

            # 解析工具调用参数
            args = _normalize_save_memory_args(response.tool_calls[0].arguments)
            if args is None:
                logger.warning("Memory consolidation: unexpected save_memory arguments")
                return self._fail_or_raw_archive(messages)

            # 验证必需字段
            if "history_entry" not in args or "memory_update" not in args:
                logger.warning("Memory consolidation: save_memory payload missing required fields")
                return self._fail_or_raw_archive(messages)

            entry = args["history_entry"]
            update = args["memory_update"]

            if entry is None or update is None:
                logger.warning("Memory consolidation: save_memory payload contains null required fields")
                return self._fail_or_raw_archive(messages)

            # 规范化并写入
            entry = _ensure_text(entry).strip()
            if not entry:
                logger.warning("Memory consolidation: history_entry is empty after normalization")
                return self._fail_or_raw_archive(messages)

            self.append_history(entry)
            update = _ensure_text(update)
            if update != current_memory:
                self.write_long_term(update)

            self._consecutive_failures = 0
            logger.info("Memory consolidation done for {} messages", len(messages))
            return True
        except Exception:
            logger.exception("Memory consolidation failed")
            return self._fail_or_raw_archive(messages)

    def _fail_or_raw_archive(self, messages: list[dict]) -> bool:
        """
        处理整合失败：增加失败计数，达到阈值后降级为原始归档。

        降级策略：直接将原始消息写入 HISTORY.md，不经过 LLM 摘要。

        Args:
            messages: 要归档的消息列表

        Returns:
            False 表示失败但未降级，True 表示已降级归档
        """
        self._consecutive_failures += 1
        if self._consecutive_failures < self._MAX_FAILURES_BEFORE_RAW_ARCHIVE:
            return False
        self._raw_archive(messages)
        self._consecutive_failures = 0
        return True

    def _raw_archive(self, messages: list[dict]) -> None:
        """
        降级归档：直接将原始消息写入 HISTORY.md。

        当 LLM 整合多次失败后使用此方法作为后备。

        Args:
            messages: 要归档的消息列表
        """
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        self.append_history(
            f"[{ts}] [RAW] {len(messages)} messages\n"
            f"{self._format_messages(messages)}"
        )
        logger.warning(
            "Memory consolidation degraded: raw-archived {} messages", len(messages)
        )


class MemoryConsolidator:
    """
    内存整合器，负责整合策略、锁定和会话偏移更新。

    该类是内存系统的高级接口，负责：
    1. 决定何时触发内存整合
    2. 管理整合锁（防止并发整合）
    3. 跟踪会话的整合进度
    4. 基于 token 预算自动归档旧消息

    整合策略：
    - 当预估 prompt token 超过预算时触发整合
    - 整合会从会话的 last_consolidated 位置开始
    - 找到合适的用户消息边界进行切割

    Attributes:
        store: 内存存储实例
        provider: LLM 提供商
        model: 使用的模型名称
        sessions: 会话管理器
        context_window_tokens: 上下文窗口大小
        max_completion_tokens: 最大完成 token 数
        _locks: 会话锁字典（使用弱引用避免内存泄漏）
    """

    # 最大整合轮次（防止无限循环）
    _MAX_CONSOLIDATION_ROUNDS = 5

    # 安全缓冲区（用于 tokenizer 估算的误差）
    _SAFETY_BUFFER = 1024

    def __init__(
        self,
        workspace: Path,
        provider: LLMProvider,
        model: str,
        sessions: SessionManager,
        context_window_tokens: int,
        build_messages: Callable[..., list[dict[str, Any]]],
        get_tool_definitions: Callable[[], list[dict[str, Any]]],
        max_completion_tokens: int = 4096,
    ):
        """
        初始化内存整合器。

        Args:
            workspace: 工作空间路径
            provider: LLM 提供商
            model: 使用的模型名称
            sessions: 会话管理器
            context_window_tokens: 上下文窗口大小（token 数）
            build_messages: 构建消息列表的回调函数
            get_tool_definitions: 获取工具定义的回调函数
            max_completion_tokens: 最大完成 token 数
        """
        self.store = MemoryStore(workspace)
        self.provider = provider
        self.model = model
        self.sessions = sessions
        self.context_window_tokens = context_window_tokens
        self.max_completion_tokens = max_completion_tokens
        self._build_messages = build_messages
        self._get_tool_definitions = get_tool_definitions
        # 使用弱引用字典存储锁，避免内存泄漏
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

    def get_lock(self, session_key: str) -> asyncio.Lock:
        """
        获取指定会话的整合锁。

        使用锁可以防止同一会话的并发整合。

        Args:
            session_key: 会话标识符

        Returns:
            该会话的异步锁
        """
        return self._locks.setdefault(session_key, asyncio.Lock())

    async def consolidate_messages(self, messages: list[dict[str, object]]) -> bool:
        """
        归档选定的消息块到持久化内存。

        Args:
            messages: 要归档的消息列表

        Returns:
            归档是否成功
        """
        return await self.store.consolidate(messages, self.provider, self.model)

    def pick_consolidation_boundary(
        self,
        session: Session,
        tokens_to_remove: int,
    ) -> tuple[int, int] | None:
        """
        选择一个用户消息边界作为整合切割点。

        整合必须在用户消息边界进行，以保持对话的完整性。
        该方法会找到第一个能够移除足够 token 的边界。

        Args:
            session: 会话对象
            tokens_to_remove: 需要移除的 token 数

        Returns:
            (边界索引, 已移除的 token 数) 或 None（如果没有合适的边界）
        """
        start = session.last_consolidated
        if start >= len(session.messages) or tokens_to_remove <= 0:
            return None

        removed_tokens = 0
        last_boundary: tuple[int, int] | None = None
        for idx in range(start, len(session.messages)):
            message = session.messages[idx]
            # 用户消息是一个新的对话轮次的开始
            if idx > start and message.get("role") == "user":
                last_boundary = (idx, removed_tokens)
                if removed_tokens >= tokens_to_remove:
                    return last_boundary
            removed_tokens += estimate_message_tokens(message)

        return last_boundary

    def estimate_session_prompt_tokens(self, session: Session) -> tuple[int, str]:
        """
        估算当前会话的 prompt token 数。

        该方法会构建完整的消息列表并估算 token 数。

        Args:
            session: 会话对象

        Returns:
            (估算的 token 数, 来源描述)
        """
        history = session.get_history(max_messages=0)
        channel, chat_id = (session.key.split(":", 1) if ":" in session.key else (None, None))
        probe_messages = self._build_messages(
            history=history,
            current_message="[token-probe]",
            channel=channel,
            chat_id=chat_id,
        )
        return estimate_prompt_tokens_chain(
            self.provider,
            self.model,
            probe_messages,
            self._get_tool_definitions(),
        )

    async def archive_messages(self, messages: list[dict[str, object]]) -> bool:
        """
        归档消息，保证持久化（会重试直到成功或降级）。

        Args:
            messages: 要归档的消息列表

        Returns:
            总是返回 True（因为最终会降级为原始归档）
        """
        if not messages:
            return True
        for _ in range(self.store._MAX_FAILURES_BEFORE_RAW_ARCHIVE):
            if await self.consolidate_messages(messages):
                return True
        return True

    async def maybe_consolidate_by_tokens(self, session: Session) -> None:
        """
        基于 token 预算自动整合：归档旧消息直到 prompt 符合预算。

        该方法会循环检查当前 prompt 的 token 数，
        如果超过预算则触发整合，直到符合要求。

        预算计算：
        budget = context_window - max_completion - safety_buffer
        target = budget / 2  # 目标是使用一半的预算

        Args:
            session: 会话对象
        """
        if not session.messages or self.context_window_tokens <= 0:
            return

        lock = self.get_lock(session.key)
        async with lock:
            # 计算预算：上下文窗口 - 完成预留 - 安全缓冲
            budget = self.context_window_tokens - self.max_completion_tokens - self._SAFETY_BUFFER
            target = budget // 2
            estimated, source = self.estimate_session_prompt_tokens(session)
            if estimated <= 0:
                return
            # 如果当前 token 数在预算内，无需整合
            if estimated < budget:
                logger.debug(
                    "Token consolidation idle {}: {}/{} via {}",
                    session.key,
                    estimated,
                    self.context_window_tokens,
                    source,
                )
                return

            # 循环整合直到符合目标
            for round_num in range(self._MAX_CONSOLIDATION_ROUNDS):
                if estimated <= target:
                    return

                # 找到合适的整合边界
                boundary = self.pick_consolidation_boundary(session, max(1, estimated - target))
                if boundary is None:
                    logger.debug(
                        "Token consolidation: no safe boundary for {} (round {})",
                        session.key,
                        round_num,
                    )
                    return

                # 提取要整合的消息块
                end_idx = boundary[0]
                chunk = session.messages[session.last_consolidated:end_idx]
                if not chunk:
                    return

                logger.info(
                    "Token consolidation round {} for {}: {}/{} via {}, chunk={} msgs",
                    round_num,
                    session.key,
                    estimated,
                    self.context_window_tokens,
                    source,
                    len(chunk),
                )
                # 执行整合
                if not await self.consolidate_messages(chunk):
                    return
                # 更新会话的整合进度
                session.last_consolidated = end_idx
                self.sessions.save(session)

                # 重新估算 token 数
                estimated, source = self.estimate_session_prompt_tokens(session)
                if estimated <= 0:
                    return
