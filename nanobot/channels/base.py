"""
频道基类模块，定义所有聊天频道实现的通用接口。

该模块提供了 BaseChannel 抽象基类，所有频道实现（如 Telegram、Discord、微信等）
都必须继承此类并实现其抽象方法。基类提供了消息处理、权限检查、音频转录等通用功能。

主要功能：
    - 定义频道生命周期方法（start/stop）
    - 提供消息发送和流式传输接口
    - 实现发送者权限验证
    - 支持音频文件转录（通过 Groq Whisper）
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus


class BaseChannel(ABC):
    """
    聊天频道实现的抽象基类。

    每个频道（Telegram、Discord、微信等）都应该实现此接口，
    以便与 nanobot 消息总线集成。基类提供了通用的消息处理逻辑，
    子类只需实现特定平台的连接和消息收发逻辑。

    属性：
        name: 频道内部标识符，用于配置和日志
        display_name: 频道显示名称，用于用户界面
        transcription_api_key: 音频转录 API 密钥

    生命周期：
        1. __init__: 初始化频道配置和消息总线引用
        2. start: 启动频道，开始监听消息
        3. stop: 停止频道，清理资源
    """

    name: str = "base"
    display_name: str = "Base"
    transcription_api_key: str = ""

    def __init__(self, config: Any, bus: MessageBus):
        """
        初始化频道实例。

        参数：
            config: 频道特定配置，通常是一个 Pydantic 模型或字典
            bus: 消息总线实例，用于与 Agent 通信
        """
        self.config = config
        self.bus = bus
        self._running = False

    async def transcribe_audio(self, file_path: str | Path) -> str:
        """
        通过 Groq Whisper API 转录音频文件。

        参数：
            file_path: 音频文件路径

        返回：
            转录文本，失败时返回空字符串

        注意：
            需要配置 transcription_api_key 才能使用此功能
        """
        if not self.transcription_api_key:
            return ""
        try:
            from nanobot.providers.transcription import GroqTranscriptionProvider

            provider = GroqTranscriptionProvider(api_key=self.transcription_api_key)
            return await provider.transcribe(file_path)
        except Exception as e:
            logger.warning("{}: audio transcription failed: {}", self.name, e)
            return ""

    async def login(self, force: bool = False) -> bool:
        """
        执行频道特定的交互式登录（如二维码扫描）。

        参数：
            force: 是否强制重新认证，忽略现有凭据

        返回：
            已认证或登录成功返回 True

        注意：
            支持交互式登录的子类应重写此方法
        """
        return True

    @abstractmethod
    async def start(self) -> None:
        """
        启动频道并开始监听消息。

        这应该是一个长时间运行的异步任务，负责：
        1. 连接到聊天平台
        2. 监听传入消息
        3. 通过 _handle_message() 将消息转发到消息总线

        实现说明：
            - 方法应持续运行直到 stop() 被调用
            - 应妥善处理连接断开和重连逻辑
            - 发生错误时应记录日志而不是静默失败
        """
        pass

    @abstractmethod
    async def stop(self) -> None:
        """
        停止频道并清理资源。

        实现说明：
            - 应取消所有正在进行的异步任务
            - 关闭网络连接
            - 释放占用的资源
        """
        pass

    @abstractmethod
    async def send(self, msg: OutboundMessage) -> None:
        """
        通过此频道发送消息。

        参数：
            msg: 要发送的出站消息对象

        实现说明：
            - 发送失败时应抛出异常，以便频道管理器应用重试策略
            - 应处理消息内容和媒体附件
        """
        pass

    async def send_delta(self, chat_id: str, delta: str, metadata: dict[str, Any] | None = None) -> None:
        """
        发送流式文本块。

        参数：
            chat_id: 目标聊天 ID
            delta: 增量文本内容
            metadata: 可选的元数据

        实现说明：
            - 子类应重写此方法以启用流式传输
            - 发送失败时应抛出异常以便重试

        流式传输协议：
            - _stream_delta: 流式文本块
            - _stream_end: 结束当前流式段落
            - 有状态的实现应使用 _stream_id 而非仅 chat_id 来标识缓冲区
        """
        pass

    @property
    def supports_streaming(self) -> bool:
        """
        检查频道是否支持流式传输。

        返回：
            当配置启用流式传输且子类实现了 send_delta 方法时返回 True
        """
        cfg = self.config
        streaming = cfg.get("streaming", False) if isinstance(cfg, dict) else getattr(cfg, "streaming", False)
        return bool(streaming) and type(self).send_delta is not BaseChannel.send_delta

    def is_allowed(self, sender_id: str) -> bool:
        """
        检查发送者是否有权限访问此频道。

        参数：
            sender_id: 发送者标识符

        返回：
            允许访问返回 True，否则返回 False

        权限规则：
            - allow_from 列表为空：拒绝所有访问
            - allow_from 包含 "*"：允许所有访问
            - 否则：仅允许列表中的发送者访问
        """
        allow_list = getattr(self.config, "allow_from", [])
        if not allow_list:
            logger.warning("{}: allow_from is empty — all access denied", self.name)
            return False
        if "*" in allow_list:
            return True
        return str(sender_id) in allow_list

    async def _handle_message(
        self,
        sender_id: str,
        chat_id: str,
        content: str,
        media: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        session_key: str | None = None,
    ) -> None:
        """
        处理来自聊天平台的传入消息。

        此方法检查权限并将消息转发到消息总线。

        参数：
            sender_id: 发送者标识符
            chat_id: 聊天/频道标识符
            content: 消息文本内容
            media: 可选的媒体文件路径列表
            metadata: 可选的频道特定元数据
            session_key: 可选的会话键覆盖（用于线程级会话）

        处理流程：
            1. 检查发送者权限
            2. 如果支持流式传输，添加 _wants_stream 标记
            3. 创建 InboundMessage 并发布到消息总线
        """
        if not self.is_allowed(sender_id):
            logger.warning(
                "Access denied for sender {} on channel {}. "
                "Add them to allowFrom list in config to grant access.",
                sender_id, self.name,
            )
            return

        meta = metadata or {}
        if self.supports_streaming:
            meta = {**meta, "_wants_stream": True}

        msg = InboundMessage(
            channel=self.name,
            sender_id=str(sender_id),
            chat_id=str(chat_id),
            content=content,
            media=media or [],
            metadata=meta,
            session_key_override=session_key,
        )

        await self.bus.publish_inbound(msg)

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        """
        返回频道的默认配置。

        用于初始化配置向导自动填充配置文件。

        返回：
            默认配置字典，子类应重写以提供特定配置
        """
        return {"enabled": False}

    @property
    def is_running(self) -> bool:
        """
        检查频道是否正在运行。

        返回：
            频道已启动且未停止返回 True
        """
        return self._running
