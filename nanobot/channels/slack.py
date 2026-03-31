"""
Slack 频道实现，使用 Socket Mode。

该模块实现了 nanobot 与 Slack 的集成，支持：
- 通过 Socket Mode 实时接收消息（无需公网 IP）
- 支持私聊（DM）和频道消息
- 支持 @ 提及和消息回复
- 支持媒体文件上传
- 支持表情反应

主要功能：
    - Socket Mode 实时消息接收
    - Markdown 转 Slack mrkdwn 格式
    - 表格转换为 Slack 可读格式
    - 线程消息回复
    - 表情反应（处理中/完成）

依赖：
    - slack-sdk: Slack Python SDK
    - slackify-markdown: Markdown 转 mrkdwn 库

配置说明：
    - bot_token: Slack 机器人令牌 (xoxb-...)
    - app_token: Slack 应用令牌 (xapp-...)
    - reply_in_thread: 是否在线程中回复
    - react_emoji: 处理中表情
    - done_emoji: 完成表情
    - group_policy: 群聊响应策略
    - dm: 私聊配置
"""

import asyncio
import re
from typing import Any

from loguru import logger
from pydantic import Field
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.socket_mode.websockets import SocketModeClient
from slack_sdk.web.async_client import AsyncWebClient
from slackify_markdown import slackify_markdown

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import Base


class SlackDMConfig(Base):
    """
    Slack 私聊策略配置。

    属性：
        enabled: 是否启用私聊
        policy: 私聊策略
            - "open": 允许所有用户
            - "allowlist": 仅允许白名单用户
        allow_from: 允许的用户 ID 白名单
    """

    enabled: bool = True
    policy: str = "open"
    allow_from: list[str] = Field(default_factory=list)


class SlackConfig(Base):
    """
    Slack 频道配置模型。

    属性：
        enabled: 是否启用此频道
        mode: 运行模式（目前仅支持 "socket"）
        webhook_path: Webhook 路径（保留用于未来扩展）
        bot_token: Slack 机器人令牌 (xoxb-...)
        app_token: Slack 应用令牌 (xapp-...)
        user_token_read_only: 用户令牌是否只读
        reply_in_thread: 是否在线程中回复频道消息
        react_emoji: 处理中表情符号
        done_emoji: 完成表情符号
        allow_from: 允许访问的用户 ID 列表
        group_policy: 群聊响应策略
            - "open": 响应所有消息
            - "mention": 仅在被 @ 提及时响应
            - "allowlist": 仅响应白名单频道
        group_allow_from: 群聊白名单频道 ID 列表
        dm: 私聊配置
    """

    enabled: bool = False
    mode: str = "socket"
    webhook_path: str = "/slack/events"
    bot_token: str = ""
    app_token: str = ""
    user_token_read_only: bool = True
    reply_in_thread: bool = True
    react_emoji: str = "eyes"
    done_emoji: str = "white_check_mark"
    allow_from: list[str] = Field(default_factory=list)
    group_policy: str = "mention"
    group_allow_from: list[str] = Field(default_factory=list)
    dm: SlackDMConfig = Field(default_factory=SlackDMConfig)


class SlackChannel(BaseChannel):
    """
    Slack 频道实现，使用 Socket Mode。

    通过 WebSocket 连接接收 Slack 事件，无需公网 IP 或 Webhook。

    属性：
        name: 频道标识符
        display_name: 频道显示名称
    """

    name = "slack"
    display_name = "Slack"

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        """返回默认配置字典。"""
        return SlackConfig().model_dump(by_alias=True)

    def __init__(self, config: Any, bus: MessageBus):
        """
        初始化 Slack 频道实例。

        参数：
            config: 频道配置
            bus: 消息总线实例
        """
        if isinstance(config, dict):
            config = SlackConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: SlackConfig = config
        self._web_client: AsyncWebClient | None = None
        self._socket_client: SocketModeClient | None = None
        self._bot_user_id: str | None = None

    async def start(self) -> None:
        """
        启动 Slack Socket Mode 客户端。

        启动流程：
            1. 检查令牌配置
            2. 创建 Web 客户端和 Socket 客户端
            3. 获取机器人用户 ID
            4. 连接 WebSocket
        """
        if not self.config.bot_token or not self.config.app_token:
            logger.error("Slack bot/app token not configured")
            return
        if self.config.mode != "socket":
            logger.error("Unsupported Slack mode: {}", self.config.mode)
            return

        self._running = True

        self._web_client = AsyncWebClient(token=self.config.bot_token)
        self._socket_client = SocketModeClient(
            app_token=self.config.app_token,
            web_client=self._web_client,
        )

        self._socket_client.socket_mode_request_listeners.append(self._on_socket_request)

        try:
            auth = await self._web_client.auth_test()
            self._bot_user_id = auth.get("user_id")
            logger.info("Slack bot connected as {}", self._bot_user_id)
        except Exception as e:
            logger.warning("Slack auth_test failed: {}", e)

        logger.info("Starting Slack Socket Mode client...")
        await self._socket_client.connect()

        while self._running:
            await asyncio.sleep(1)

    async def stop(self) -> None:
        """停止 Slack 客户端。"""
        self._running = False
        if self._socket_client:
            try:
                await self._socket_client.close()
            except Exception as e:
                logger.warning("Slack socket close failed: {}", e)
            self._socket_client = None

    async def send(self, msg: OutboundMessage) -> None:
        """
        发送消息通过 Slack。

        参数：
            msg: 出站消息对象

        处理流程：
            1. 发送文本内容（转换为 mrkdwn 格式）
            2. 上传媒体文件
            3. 更新表情反应
        """
        if not self._web_client:
            logger.warning("Slack client not running")
            return
        try:
            slack_meta = msg.metadata.get("slack", {}) if msg.metadata else {}
            thread_ts = slack_meta.get("thread_ts")
            channel_type = slack_meta.get("channel_type")

            thread_ts_param = thread_ts if thread_ts and channel_type != "im" else None

            if msg.content or not (msg.media or []):
                await self._web_client.chat_postMessage(
                    channel=msg.chat_id,
                    text=self._to_mrkdwn(msg.content) if msg.content else " ",
                    thread_ts=thread_ts_param,
                )

            for media_path in msg.media or []:
                try:
                    await self._web_client.files_upload_v2(
                        channel=msg.chat_id,
                        file=media_path,
                        thread_ts=thread_ts_param,
                    )
                except Exception as e:
                    logger.error("Failed to upload file {}: {}", media_path, e)

            if not (msg.metadata or {}).get("_progress"):
                event = slack_meta.get("event", {})
                await self._update_react_emoji(msg.chat_id, event.get("ts"))

        except Exception as e:
            logger.error("Error sending Slack message: {}", e)
            raise

    async def _on_socket_request(
        self,
        client: SocketModeClient,
        req: SocketModeRequest,
    ) -> None:
        """
        处理传入的 Socket Mode 请求。

        参数：
            client: Socket Mode 客户端
            req: 请求对象
        """
        if req.type != "events_api":
            return

        await client.send_socket_mode_response(
            SocketModeResponse(envelope_id=req.envelope_id)
        )

        payload = req.payload or {}
        event = payload.get("event") or {}
        event_type = event.get("type")

        if event_type not in ("message", "app_mention"):
            return

        sender_id = event.get("user")
        chat_id = event.get("channel")

        if event.get("subtype"):
            return
        if self._bot_user_id and sender_id == self._bot_user_id:
            return

        text = event.get("text") or ""
        if event_type == "message" and self._bot_user_id and f"<@{self._bot_user_id}>" in text:
            return

        logger.debug(
            "Slack event: type={} subtype={} user={} channel={} channel_type={} text={}",
            event_type,
            event.get("subtype"),
            sender_id,
            chat_id,
            event.get("channel_type"),
            text[:80],
        )
        if not sender_id or not chat_id:
            return

        channel_type = event.get("channel_type") or ""

        if not self._is_allowed(sender_id, chat_id, channel_type):
            return

        if channel_type != "im" and not self._should_respond_in_channel(event_type, text, chat_id):
            return

        text = self._strip_bot_mention(text)

        thread_ts = event.get("thread_ts")
        if self.config.reply_in_thread and not thread_ts:
            thread_ts = event.get("ts")

        try:
            if self._web_client and event.get("ts"):
                await self._web_client.reactions_add(
                    channel=chat_id,
                    name=self.config.react_emoji,
                    timestamp=event.get("ts"),
                )
        except Exception as e:
            logger.debug("Slack reactions_add failed: {}", e)

        session_key = f"slack:{chat_id}:{thread_ts}" if thread_ts and channel_type != "im" else None

        try:
            await self._handle_message(
                sender_id=sender_id,
                chat_id=chat_id,
                content=text,
                metadata={
                    "slack": {
                        "event": event,
                        "thread_ts": thread_ts,
                        "channel_type": channel_type,
                    },
                },
                session_key=session_key,
            )
        except Exception:
            logger.exception("Error handling Slack message from {}", sender_id)

    async def _update_react_emoji(self, chat_id: str, ts: str | None) -> None:
        """
        移除处理中表情并添加完成表情。

        参数：
            chat_id: 频道 ID
            ts: 消息时间戳
        """
        if not self._web_client or not ts:
            return
        try:
            await self._web_client.reactions_remove(
                channel=chat_id,
                name=self.config.react_emoji,
                timestamp=ts,
            )
        except Exception as e:
            logger.debug("Slack reactions_remove failed: {}", e)
        if self.config.done_emoji:
            try:
                await self._web_client.reactions_add(
                    channel=chat_id,
                    name=self.config.done_emoji,
                    timestamp=ts,
                )
            except Exception as e:
                logger.debug("Slack done reaction failed: {}", e)

    def _is_allowed(self, sender_id: str, chat_id: str, channel_type: str) -> bool:
        """
        检查发送者是否有权限访问。

        参数：
            sender_id: 发送者 ID
            chat_id: 频道 ID
            channel_type: 频道类型

        返回：
            允许访问返回 True
        """
        if channel_type == "im":
            if not self.config.dm.enabled:
                return False
            if self.config.dm.policy == "allowlist":
                return sender_id in self.config.dm.allow_from
            return True

        if self.config.group_policy == "allowlist":
            return chat_id in self.config.group_allow_from
        return True

    def _should_respond_in_channel(self, event_type: str, text: str, chat_id: str) -> bool:
        """
        检查是否应在频道中响应。

        参数：
            event_type: 事件类型
            text: 消息文本
            chat_id: 频道 ID

        返回：
            应响应返回 True
        """
        if self.config.group_policy == "open":
            return True
        if self.config.group_policy == "mention":
            if event_type == "app_mention":
                return True
            return self._bot_user_id is not None and f"<@{self._bot_user_id}>" in text
        if self.config.group_policy == "allowlist":
            return chat_id in self.config.group_allow_from
        return False

    def _strip_bot_mention(self, text: str) -> str:
        """
        移除消息中的机器人 @ 提及。

        参数：
            text: 原始文本

        返回：
            移除提及后的文本
        """
        if not text or not self._bot_user_id:
            return text
        return re.sub(rf"<@{re.escape(self._bot_user_id)}>\s*", "", text).strip()

    _TABLE_RE = re.compile(r"(?m)^\|.*\|$(?:\n\|[\s:|-]*\|$)(?:\n\|.*\|$)*")
    _CODE_FENCE_RE = re.compile(r"```[\s\S]*?```")
    _INLINE_CODE_RE = re.compile(r"`[^`]+`")
    _LEFTOVER_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
    _LEFTOVER_HEADER_RE = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)
    _BARE_URL_RE = re.compile(r"(?<![|<])(https?://\S+)")

    @classmethod
    def _to_mrkdwn(cls, text: str) -> str:
        """
        将 Markdown 转换为 Slack mrkdwn 格式，包括表格。

        参数：
            text: Markdown 文本

        返回：
            mrkdwn 格式文本
        """
        if not text:
            return ""
        text = cls._TABLE_RE.sub(cls._convert_table, text)
        return cls._fixup_mrkdwn(slackify_markdown(text))

    @classmethod
    def _fixup_mrkdwn(cls, text: str) -> str:
        """
        修复 slackify_markdown 遗漏的 Markdown 瑕疵。

        参数：
            text: 转换后的文本

        返回：
            修复后的文本
        """
        code_blocks: list[str] = []

        def _save_code(m: re.Match) -> str:
            code_blocks.append(m.group(0))
            return f"\x00CB{len(code_blocks) - 1}\x00"

        text = cls._CODE_FENCE_RE.sub(_save_code, text)
        text = cls._INLINE_CODE_RE.sub(_save_code, text)
        text = cls._LEFTOVER_BOLD_RE.sub(r"*\1*", text)
        text = cls._LEFTOVER_HEADER_RE.sub(r"*\1*", text)
        text = cls._BARE_URL_RE.sub(lambda m: m.group(0).replace("&amp;", "&"), text)

        for i, block in enumerate(code_blocks):
            text = text.replace(f"\x00CB{i}\x00", block)
        return text

    @staticmethod
    def _convert_table(match: re.Match) -> str:
        """
        将 Markdown 表格转换为 Slack 可读的列表格式。

        参数：
            match: 正则匹配对象

        返回：
            转换后的文本
        """
        lines = [ln.strip() for ln in match.group(0).strip().splitlines() if ln.strip()]
        if len(lines) < 2:
            return match.group(0)
        headers = [h.strip() for h in lines[0].strip("|").split("|")]
        start = 2 if re.fullmatch(r"[|\s:\-]+", lines[1]) else 1
        rows: list[str] = []
        for line in lines[start:]:
            cells = [c.strip() for c in line.strip("|").split("|")]
            cells = (cells + [""] * len(headers))[: len(headers)]
            parts = [f"**{headers[i]}**: {cells[i]}" for i in range(len(headers)) if cells[i]]
            if parts:
                rows.append(" · ".join(parts))
        return "\n".join(rows)
