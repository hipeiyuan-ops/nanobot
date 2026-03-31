"""
消息发送工具模块，允许 agent 向用户发送消息和媒体文件。

该模块实现了 message 工具，是 agent 与用户通信的主要方式。
支持：
- 发送文本消息到指定频道
- 发送媒体文件（图片、文档、音频、视频）
- 指定消息格式（markdown、html 等）

注意：这是 agent 主动发送消息的唯一方式。
"""

from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool
from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus


class MessageTool(Tool):
    """
    消息发送工具，用于向用户发送消息和媒体文件。

    该工具是 agent 与用户通信的主要方式，支持：
    - 发送文本消息
    - 发送媒体文件（图片、文档、音频、视频）
    - 指定消息格式

    Attributes:
        _bus: 消息总线
        _channel: 当前频道
        _chat_id: 当前聊天 ID
    """

    def __init__(self, bus: MessageBus, channel: str = "cli", chat_id: str = "direct"):
        """
        初始化消息工具。

        Args:
            bus: 消息总线，用于发送消息
            channel: 当前频道名称
            chat_id: 当前聊天 ID
        """
        self._bus = bus
        self._channel = channel
        self._chat_id = chat_id

    def set_context(self, channel: str, chat_id: str) -> None:
        """
        设置当前会话上下文。

        Args:
            channel: 频道名称
            chat_id: 聊天 ID
        """
        self._channel = channel
        self._chat_id = chat_id

    @property
    def name(self) -> str:
        """工具名称。"""
        return "message"

    @property
    def description(self) -> str:
        """工具描述。"""
        return (
            "Send a message to the user. Use this to send files (images, documents, audio, video). "
            "For plain text replies, respond directly without calling this tool."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        """工具参数的 JSON Schema。"""
        return {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The message content (markdown supported)",
                },
                "media": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of media file paths to send (images, documents, audio, video)",
                },
                "format": {
                    "type": "string",
                    "enum": ["markdown", "html", "text"],
                    "description": "Message format (default: markdown)",
                },
            },
            "required": ["content"],
        }

    async def execute(
        self,
        content: str = "",
        media: list[str] | None = None,
        format: str = "markdown",
        **kwargs: Any,
    ) -> str:
        """
        执行消息发送。

        Args:
            content: 消息内容
            media: 媒体文件路径列表
            format: 消息格式（markdown、html、text）

        Returns:
            操作结果消息
        """
        if not content and not media:
            return "Error: message content or media is required"

        # 验证媒体文件是否存在
        valid_media: list[str] | None = None
        if media:
            valid_media = []
            for path_str in media:
                p = Path(path_str).expanduser()
                if not p.is_file():
                    return f"Error: media file not found: {path_str}"
                valid_media.append(str(p))

        # 构建并发送消息
        msg = OutboundMessage(
            channel=self._channel,
            chat_id=self._chat_id,
            content=content,
            media=valid_media,
            format=format,
        )
        await self._bus.publish_outbound(msg)
        return "Message sent."
