"""
频道管理器模块，协调多个聊天频道的消息路由。

该模块提供 ChannelManager 类，负责：
- 初始化和启动所有已启用的频道
- 协调出站消息的分发
- 管理频道生命周期
- 提供重试机制和流式消息合并

主要功能：
    - 自动发现和初始化频道（通过 registry 模块）
    - 出站消息分发（支持重试和流式传输）
    - 流式消息块合并优化
    - 频道状态监控
"""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import Config

_SEND_RETRY_DELAYS = (1, 2, 4)


class ChannelManager:
    """
    频道管理器，协调聊天频道和消息路由。

    职责：
        - 初始化已启用的频道（Telegram、WhatsApp 等）
        - 启动/停止频道
        - 路由出站消息
        - 合并流式消息块

    属性：
        config: 全局配置对象
        bus: 消息总线实例
        channels: 已初始化的频道字典 {名称: 频道实例}
    """

    def __init__(self, config: Config, bus: MessageBus):
        """
        初始化频道管理器。

        参数：
            config: 全局配置对象
            bus: 消息总线实例
        """
        self.config = config
        self.bus = bus
        self.channels: dict[str, BaseChannel] = {}
        self._dispatch_task: asyncio.Task | None = None

        self._init_channels()

    def _init_channels(self) -> None:
        """
        初始化通过 pkgutil 扫描和 entry_points 插件发现的频道。

        遍历所有已发现的频道类，检查配置是否启用，
        创建频道实例并添加到 channels 字典。
        """
        from nanobot.channels.registry import discover_all

        groq_key = self.config.providers.groq.api_key

        for name, cls in discover_all().items():
            section = getattr(self.config.channels, name, None)
            if section is None:
                continue
            enabled = (
                section.get("enabled", False)
                if isinstance(section, dict)
                else getattr(section, "enabled", False)
            )
            if not enabled:
                continue
            try:
                channel = cls(section, self.bus)
                channel.transcription_api_key = groq_key
                self.channels[name] = channel
                logger.info("{} channel enabled", cls.display_name)
            except Exception as e:
                logger.warning("{} channel not available: {}", name, e)

        self._validate_allow_from()

    def _validate_allow_from(self) -> None:
        """
        验证所有频道的 allow_from 配置。

        如果任何频道的 allow_from 为空列表，则拒绝启动，
        因为这会拒绝所有访问（可能是配置错误）。
        """
        for name, ch in self.channels.items():
            if getattr(ch.config, "allow_from", None) == []:
                raise SystemExit(
                    f'Error: "{name}" has empty allowFrom (denies all). '
                    f'Set ["*"] to allow everyone, or add specific user IDs.'
                )

    async def _start_channel(self, name: str, channel: BaseChannel) -> None:
        """
        启动单个频道并记录异常。

        参数：
            name: 频道名称
            channel: 频道实例
        """
        try:
            await channel.start()
        except Exception as e:
            logger.error("Failed to start channel {}: {}", name, e)

    async def start_all(self) -> None:
        """
        启动所有频道和出站分发器。

        启动流程：
            1. 检查是否有已启用的频道
            2. 启动出站消息分发器
            3. 并行启动所有频道
            4. 等待所有频道完成（通常持续运行）
        """
        if not self.channels:
            logger.warning("No channels enabled")
            return

        self._dispatch_task = asyncio.create_task(self._dispatch_outbound())

        tasks = []
        for name, channel in self.channels.items():
            logger.info("Starting {} channel...", name)
            tasks.append(asyncio.create_task(self._start_channel(name, channel)))

        await asyncio.gather(*tasks, return_exceptions=True)

    async def stop_all(self) -> None:
        """
        停止所有频道和分发器。

        停止流程：
            1. 取消出站分发器任务
            2. 停止所有频道
        """
        logger.info("Stopping all channels...")

        if self._dispatch_task:
            self._dispatch_task.cancel()
            try:
                await self._dispatch_task
            except asyncio.CancelledError:
                pass

        for name, channel in self.channels.items():
            try:
                await channel.stop()
                logger.info("Stopped {} channel", name)
            except Exception as e:
                logger.error("Error stopping {}: {}", name, e)

    async def _dispatch_outbound(self) -> None:
        """
        分发出站消息到适当的频道。

        从消息总线消费出站消息，根据消息中的频道字段
        路由到对应的频道实例发送。

        处理流程：
            1. 从消息总线获取出站消息
            2. 过滤进度消息（根据配置）
            3. 合并连续的流式消息块
            4. 通过对应频道发送消息
        """
        logger.info("Outbound dispatcher started")

        pending: list[OutboundMessage] = []

        while True:
            try:
                if pending:
                    msg = pending.pop(0)
                else:
                    msg = await asyncio.wait_for(
                        self.bus.consume_outbound(),
                        timeout=1.0
                    )

                if msg.metadata.get("_progress"):
                    if msg.metadata.get("_tool_hint") and not self.config.channels.send_tool_hints:
                        continue
                    if not msg.metadata.get("_tool_hint") and not self.config.channels.send_progress:
                        continue

                if msg.metadata.get("_stream_delta") and not msg.metadata.get("_stream_end"):
                    msg, extra_pending = self._coalesce_stream_deltas(msg)
                    pending.extend(extra_pending)

                channel = self.channels.get(msg.channel)
                if channel:
                    await self._send_with_retry(channel, msg)
                else:
                    logger.warning("Unknown channel: {}", msg.channel)

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

    @staticmethod
    async def _send_once(channel: BaseChannel, msg: OutboundMessage) -> None:
        """
        发送单条出站消息，不应用重试策略。

        参数：
            channel: 目标频道
            msg: 出站消息对象

        处理逻辑：
            - 流式消息：调用 send_delta
            - 非流式消息：调用 send
        """
        if msg.metadata.get("_stream_delta") or msg.metadata.get("_stream_end"):
            await channel.send_delta(msg.chat_id, msg.content, msg.metadata)
        elif not msg.metadata.get("_streamed"):
            await channel.send(msg)

    def _coalesce_stream_deltas(
        self, first_msg: OutboundMessage
    ) -> tuple[OutboundMessage, list[OutboundMessage]]:
        """
        合并同一 (频道, 聊天ID) 的连续流式消息块。

        当队列中积累了多个消息块时（LLM 生成速度快于频道处理速度），
        合并可以减少 API 调用次数，改善流式传输延迟。

        参数：
            first_msg: 第一条流式消息

        返回：
            元组 (合并后的消息, 不匹配的消息列表)
        """
        target_key = (first_msg.channel, first_msg.chat_id)
        combined_content = first_msg.content
        final_metadata = dict(first_msg.metadata or {})
        non_matching: list[OutboundMessage] = []

        while True:
            try:
                next_msg = self.bus.outbound.get_nowait()
            except asyncio.QueueEmpty:
                break

            same_target = (next_msg.channel, next_msg.chat_id) == target_key
            is_delta = next_msg.metadata and next_msg.metadata.get("_stream_delta")
            is_end = next_msg.metadata and next_msg.metadata.get("_stream_end")

            if same_target and is_delta and not final_metadata.get("_stream_end"):
                combined_content += next_msg.content
                if is_end:
                    final_metadata["_stream_end"] = True
                    break
            else:
                non_matching.append(next_msg)
                break

        merged = OutboundMessage(
            channel=first_msg.channel,
            chat_id=first_msg.chat_id,
            content=combined_content,
            metadata=final_metadata,
        )
        return merged, non_matching

    async def _send_with_retry(self, channel: BaseChannel, msg: OutboundMessage) -> None:
        """
        发送消息，失败时使用指数退避重试。

        参数：
            channel: 目标频道
            msg: 出站消息对象

        重试策略：
            - 最大重试次数由配置决定
            - 延迟序列：1秒、2秒、4秒
            - CancelledError 会被重新抛出以支持优雅关闭
        """
        max_attempts = max(self.config.channels.send_max_retries, 1)

        for attempt in range(max_attempts):
            try:
                await self._send_once(channel, msg)
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if attempt == max_attempts - 1:
                    logger.error(
                        "Failed to send to {} after {} attempts: {} - {}",
                        msg.channel, max_attempts, type(e).__name__, e
                    )
                    return
                delay = _SEND_RETRY_DELAYS[min(attempt, len(_SEND_RETRY_DELAYS) - 1)]
                logger.warning(
                    "Send to {} failed (attempt {}/{}): {}, retrying in {}s",
                    msg.channel, attempt + 1, max_attempts, type(e).__name__, delay
                )
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    raise

    def get_channel(self, name: str) -> BaseChannel | None:
        """
        按名称获取频道实例。

        参数：
            name: 频道名称

        返回：
            频道实例，不存在返回 None
        """
        return self.channels.get(name)

    def get_status(self) -> dict[str, Any]:
        """
        获取所有频道的状态。

        返回：
            频道状态字典 {名称: {enabled, running}}
        """
        return {
            name: {
                "enabled": True,
                "running": channel.is_running
            }
            for name, channel in self.channels.items()
        }

    @property
    def enabled_channels(self) -> list[str]:
        """
        获取已启用的频道名称列表。

        返回：
            频道名称列表
        """
        return list(self.channels.keys())
