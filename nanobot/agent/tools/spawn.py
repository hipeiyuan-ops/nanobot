"""
子代理生成工具模块，允许 agent 创建后台任务。

该模块实现了 spawn 工具，允许 agent 创建子代理来执行后台任务：
- 异步执行长时间运行的任务
- 任务完成后自动通知主代理
- 支持任务标签和描述

子代理是独立的 agent 实例，有自己的工具集和执行上下文。
"""

from typing import Any

from nanobot.agent.tools.base import Tool
from nanobot.agent.subagent import SubagentManager


class SpawnTool(Tool):
    """
    子代理生成工具，用于创建后台任务。

    该工具允许 agent 创建子代理来执行后台任务：
    - 异步执行长时间运行的任务
    - 任务完成后自动通知主代理
    - 支持任务标签和描述

    子代理是独立的 agent 实例，有自己的工具集和执行上下文。
    子代理的限制：
    - 不能使用消息工具（message）
    - 不能创建新的子代理（spawn）

    Attributes:
        _manager: 子代理管理器
        _channel: 当前频道
        _chat_id: 当前聊天 ID
        _session_key: 当前会话标识符
    """

    def __init__(
        self,
        manager: SubagentManager,
        channel: str = "cli",
        chat_id: str = "direct",
        session_key: str | None = None,
    ):
        """
        初始化子代理生成工具。

        Args:
            manager: 子代理管理器
            channel: 当前频道
            chat_id: 当前聊天 ID
            session_key: 当前会话标识符
        """
        self._manager = manager
        self._channel = channel
        self._chat_id = chat_id
        self._session_key = session_key

    def set_context(self, channel: str, chat_id: str, session_key: str | None = None) -> None:
        """
        设置当前会话上下文。

        Args:
            channel: 频道名称
            chat_id: 聊天 ID
            session_key: 会话标识符
        """
        self._channel = channel
        self._chat_id = chat_id
        self._session_key = session_key

    @property
    def name(self) -> str:
        """工具名称。"""
        return "spawn"

    @property
    def description(self) -> str:
        """工具描述。"""
        return (
            "Spawn a background subagent to execute a task asynchronously. "
            "Use for long-running operations that don't need immediate results. "
            "The subagent will notify you when it completes."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        """工具参数的 JSON Schema。"""
        return {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "The task description for the subagent to execute",
                },
                "label": {
                    "type": "string",
                    "description": "Optional short label for the task (shown in notifications)",
                },
            },
            "required": ["task"],
        }

    async def execute(self, task: str = "", label: str | None = None, **kwargs: Any) -> str:
        """
        执行子代理生成。

        Args:
            task: 任务描述
            label: 任务标签（可选）

        Returns:
            操作结果消息
        """
        if not task:
            return "Error: task description is required"

        return await self._manager.spawn(
            task=task,
            label=label,
            origin_channel=self._channel,
            origin_chat_id=self._chat_id,
            session_key=self._session_key,
        )
