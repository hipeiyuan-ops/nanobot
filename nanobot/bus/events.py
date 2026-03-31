"""
消息总线的事件类型定义

这个模块定义了消息总线使用的核心事件类型：
- InboundMessage: 入站消息，从聊天通道接收的消息
- OutboundMessage: 出站消息，发送到聊天通道的响应

这些数据类用于在通道和代理之间传递消息，确保消息格式的一致性。
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class InboundMessage:
    """
    入站消息：从聊天通道接收的消息
    
    当用户在任何聊天平台（如 Telegram、Discord、Slack、WhatsApp 等）
    发送消息时，通道会将消息封装为 InboundMessage 对象，然后通过
    消息总线发送给代理进行处理。
    
    属性：
        channel: 通道名称（如 "telegram", "discord", "slack", "whatsapp"）
        sender_id: 发送者标识符，用于识别消息的发送者
        chat_id: 聊天/通道标识符，用于识别消息来源的聊天或通道
        content: 消息文本内容
        timestamp: 消息时间戳，默认为当前时间
        media: 媒体 URL 列表，包含消息中的图片、文件等媒体资源
        metadata: 通道特定数据字典，用于存储通道特有的元数据
        session_key_override: 可选的会话键覆盖，用于线程范围的会话
    
    示例：
        >>> msg = InboundMessage(
        ...     channel="telegram",
        ...     sender_id="user123",
        ...     chat_id="chat456",
        ...     content="你好，nanobot！"
        ... )
        >>> print(msg.session_key)  # "telegram:chat456"
    """

    channel: str  # 通道名称：telegram, discord, slack, whatsapp 等
    sender_id: str  # 发送者标识符
    chat_id: str  # 聊天/通道标识符
    content: str  # 消息文本内容
    timestamp: datetime = field(default_factory=datetime.now)  # 消息时间戳
    media: list[str] = field(default_factory=list)  # 媒体 URL 列表
    metadata: dict[str, Any] = field(default_factory=dict)  # 通道特定数据
    session_key_override: str | None = None  # 可选的会话键覆盖

    @property
    def session_key(self) -> str:
        """
        获取会话键：用于会话标识的唯一键
        
        会话键用于标识和区分不同的会话。默认情况下，会话键由通道名称
        和聊天 ID 组合而成（格式："channel:chat_id"）。
        
        如果设置了 session_key_override，则使用覆盖值作为会话键。
        这在某些场景下很有用，例如在线程中创建独立的会话。
        
        Returns:
            str: 会话键，格式为 "channel:chat_id" 或覆盖值
        """
        return self.session_key_override or f"{self.channel}:{self.chat_id}"


@dataclass
class OutboundMessage:
    """
    出站消息：发送到聊天通道的响应
    
    当代理处理完入站消息后，会生成 OutboundMessage 对象，然后通过
    消息总线发送到相应的通道，最终由通道将消息发送给用户。
    
    属性：
        channel: 目标通道名称（如 "telegram", "discord" 等）
        chat_id: 目标聊天/通道标识符
        content: 响应消息文本内容
        reply_to: 可选的回复消息 ID，用于回复特定消息
        media: 媒体 URL 列表，包含要发送的图片、文件等
        metadata: 元数据字典，用于存储额外的信息（如流式输出标记）
    
    示例：
        >>> response = OutboundMessage(
        ...     channel="telegram",
        ...     chat_id="chat456",
        ...     content="你好！我是 nanobot，有什么可以帮助你的吗？"
        ... )
    """

    channel: str  # 目标通道名称
    chat_id: str  # 目标聊天/通道标识符
    content: str  # 响应消息文本内容
    reply_to: str | None = None  # 可选的回复消息 ID
    media: list[str] = field(default_factory=list)  # 媒体 URL 列表
    metadata: dict[str, Any] = field(default_factory=dict)  # 元数据字典
