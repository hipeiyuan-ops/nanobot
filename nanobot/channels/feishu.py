"""
飞书/Lark 频道实现，使用 lark-oapi SDK 和 WebSocket 长连接。

该模块实现了 nanobot 与飞书（Lark）的集成，支持：
- 通过 WebSocket 长连接接收消息（无需公网 IP）
- 支持文本、图片、文件、音频、富文本等多种消息类型
- 支持卡片消息和流式输出（CardKit streaming API）
- 支持群聊和私聊消息处理
- 支持 Markdown 转 Feishu 富文本格式

主要功能：
    - WebSocket 长连接消息接收
    - 多种消息格式智能选择（text/post/interactive）
    - 流式消息输出（打字机效果）
    - 媒体文件上传和下载
    - 表情反应和消息回复

依赖：
    - lark-oapi: 飞书开放平台 Python SDK

配置说明：
    - app_id: 飞书应用的 App ID
    - app_secret: 飞书应用的 App Secret
    - encrypt_key: 事件订阅加密密钥（可选）
    - verification_token: 事件订阅验证令牌（可选）
    - group_policy: 群聊响应策略 ("open" 或 "mention")
    - reply_to_message: 是否回复引用原消息
    - streaming: 是否启用流式输出
"""

import asyncio
import json
import os
import re
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.paths import get_media_dir
from nanobot.config.schema import Base
from pydantic import Field

import importlib.util

FEISHU_AVAILABLE = importlib.util.find_spec("lark_oapi") is not None

MSG_TYPE_MAP = {
    "image": "[image]",
    "audio": "[audio]",
    "file": "[file]",
    "sticker": "[sticker]",
}


def _extract_share_card_content(content_json: dict, msg_type: str) -> str:
    """
    从分享卡片和交互消息中提取文本表示。

    参数：
        content_json: 消息内容 JSON
        msg_type: 消息类型

    返回：
        文本表示字符串
    """
    parts = []

    if msg_type == "share_chat":
        parts.append(f"[shared chat: {content_json.get('chat_id', '')}]")
    elif msg_type == "share_user":
        parts.append(f"[shared user: {content_json.get('user_id', '')}]")
    elif msg_type == "interactive":
        parts.extend(_extract_interactive_content(content_json))
    elif msg_type == "share_calendar_event":
        parts.append(f"[shared calendar event: {content_json.get('event_key', '')}]")
    elif msg_type == "system":
        parts.append("[system message]")
    elif msg_type == "merge_forward":
        parts.append("[merged forward messages]")

    return "\n".join(parts) if parts else f"[{msg_type}]"


def _extract_interactive_content(content: dict) -> list[str]:
    """
    递归提取交互卡片内容中的文本和链接。

    参数：
        content: 卡片内容字典

    返回：
        提取的文本片段列表
    """
    parts = []

    if isinstance(content, str):
        try:
            content = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            return [content] if content.strip() else []

    if not isinstance(content, dict):
        return parts

    if "title" in content:
        title = content["title"]
        if isinstance(title, dict):
            title_content = title.get("content", "") or title.get("text", "")
            if title_content:
                parts.append(f"title: {title_content}")
        elif isinstance(title, str):
            parts.append(f"title: {title}")

    for elements in content.get("elements", []) if isinstance(content.get("elements"), list) else []:
        for element in elements:
            parts.extend(_extract_element_content(element))

    card = content.get("card", {})
    if card:
        parts.extend(_extract_interactive_content(card))

    header = content.get("header", {})
    if header:
        header_title = header.get("title", {})
        if isinstance(header_title, dict):
            header_text = header_title.get("content", "") or header_title.get("text", "")
            if header_text:
                parts.append(f"title: {header_text}")

    return parts


def _extract_element_content(element: dict) -> list[str]:
    """
    从单个卡片元素中提取内容。

    参数：
        element: 卡片元素字典

    返回：
        提取的文本片段列表
    """
    parts = []

    if not isinstance(element, dict):
        return parts

    tag = element.get("tag", "")

    if tag in ("markdown", "lark_md"):
        content = element.get("content", "")
        if content:
            parts.append(content)

    elif tag == "div":
        text = element.get("text", {})
        if isinstance(text, dict):
            text_content = text.get("content", "") or text.get("text", "")
            if text_content:
                parts.append(text_content)
        elif isinstance(text, str):
            parts.append(text)
        for field in element.get("fields", []):
            if isinstance(field, dict):
                field_text = field.get("text", {})
                if isinstance(field_text, dict):
                    c = field_text.get("content", "")
                    if c:
                        parts.append(c)

    elif tag == "a":
        href = element.get("href", "")
        text = element.get("text", "")
        if href:
            parts.append(f"link: {href}")
        if text:
            parts.append(text)

    elif tag == "button":
        text = element.get("text", {})
        if isinstance(text, dict):
            c = text.get("content", "")
            if c:
                parts.append(c)
        url = element.get("url", "") or element.get("multi_url", {}).get("url", "")
        if url:
            parts.append(f"link: {url}")

    elif tag == "img":
        alt = element.get("alt", {})
        parts.append(alt.get("content", "[image]") if isinstance(alt, dict) else "[image]")

    elif tag == "note":
        for ne in element.get("elements", []):
            parts.extend(_extract_element_content(ne))

    elif tag == "column_set":
        for col in element.get("columns", []):
            for ce in col.get("elements", []):
                parts.extend(_extract_element_content(ce))

    elif tag == "plain_text":
        content = element.get("content", "")
        if content:
            parts.append(content)

    else:
        for ne in element.get("elements", []):
            parts.extend(_extract_element_content(ne))

    return parts


def _extract_post_content(content_json: dict) -> tuple[str, list[str]]:
    """
    从飞书富文本消息中提取文本和图片键。

    支持三种载荷格式：
    - 直接格式: {"title": "...", "content": [[...]]}
    - 本地化格式: {"zh_cn": {"title": "...", "content": [...]}}
    - 包装格式: {"post": {"zh_cn": {"title": "...", "content": [...]}}}

    参数：
        content_json: 富文本消息内容 JSON

    返回：
        元组 (提取的文本, 图片键列表)
    """

    def _parse_block(block: dict) -> tuple[str | None, list[str]]:
        if not isinstance(block, dict) or not isinstance(block.get("content"), list):
            return None, []
        texts, images = [], []
        if title := block.get("title"):
            texts.append(title)
        for row in block["content"]:
            if not isinstance(row, list):
                continue
            for el in row:
                if not isinstance(el, dict):
                    continue
                tag = el.get("tag")
                if tag in ("text", "a"):
                    texts.append(el.get("text", ""))
                elif tag == "at":
                    texts.append(f"@{el.get('user_name', 'user')}")
                elif tag == "code_block":
                    lang = el.get("language", "")
                    code_text = el.get("text", "")
                    texts.append(f"\n```{lang}\n{code_text}\n```\n")
                elif tag == "img" and (key := el.get("image_key")):
                    images.append(key)
        return (" ".join(texts).strip() or None), images

    root = content_json
    if isinstance(root, dict) and isinstance(root.get("post"), dict):
        root = root["post"]
    if not isinstance(root, dict):
        return "", []

    if "content" in root:
        text, imgs = _parse_block(root)
        if text or imgs:
            return text or "", imgs

    for key in ("zh_cn", "en_us", "ja_jp"):
        if key in root:
            text, imgs = _parse_block(root[key])
            if text or imgs:
                return text or "", imgs
    for val in root.values():
        if isinstance(val, dict):
            text, imgs = _parse_block(val)
            if text or imgs:
                return text or "", imgs

    return "", []


def _extract_post_text(content_json: dict) -> str:
    """
    从飞书富文本消息中提取纯文本。

    _extract_post_content 的遗留包装器，仅返回文本。

    参数：
        content_json: 富文本消息内容 JSON

    返回：
        提取的文本
    """
    text, _ = _extract_post_content(content_json)
    return text


class FeishuConfig(Base):
    """
    飞书/Lark 频道配置模型，使用 WebSocket 长连接。

    属性：
        enabled: 是否启用此频道
        app_id: 飞书应用的 App ID
        app_secret: 飞书应用的 App Secret
        encrypt_key: 事件订阅加密密钥（可选）
        verification_token: 事件订阅验证令牌（可选）
        allow_from: 允许访问的用户 ID 列表
        react_emoji: 收到消息时的表情反应
        group_policy: 群聊响应策略
            - "open": 响应所有消息
            - "mention": 仅在被 @ 提及时响应
        reply_to_message: 是否回复引用原消息
        streaming: 是否启用流式输出
    """

    enabled: bool = False
    app_id: str = ""
    app_secret: str = ""
    encrypt_key: str = ""
    verification_token: str = ""
    allow_from: list[str] = Field(default_factory=list)
    react_emoji: str = "THUMBSUP"
    group_policy: Literal["open", "mention"] = "mention"
    reply_to_message: bool = False
    streaming: bool = True


_STREAM_ELEMENT_ID = "streaming_md"


@dataclass
class _FeishuStreamBuf:
    """
    每个聊天的流式累积缓冲区，使用 CardKit streaming API。

    属性：
        text: 累积的文本内容
        card_id: CardKit 卡片 ID
        sequence: 更新序列号
        last_edit: 上次编辑时间戳
    """
    text: str = ""
    card_id: str | None = None
    sequence: int = 0
    last_edit: float = 0.0


class FeishuChannel(BaseChannel):
    """
    飞书/Lark 频道实现，使用 WebSocket 长连接。

    通过 WebSocket 接收事件消息，无需公网 IP 或 Webhook。

    要求：
        - 飞书开放平台的 App ID 和 App Secret
        - 启用机器人能力
        - 启用事件订阅 (im.message.receive_v1)

    属性：
        name: 频道标识符
        display_name: 频道显示名称
    """

    name = "feishu"
    display_name = "Feishu"

    _STREAM_EDIT_INTERVAL = 0.5

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        """返回默认配置字典。"""
        return FeishuConfig().model_dump(by_alias=True)

    def __init__(self, config: Any, bus: MessageBus):
        """
        初始化飞书频道实例。

        参数：
            config: 频道配置
            bus: 消息总线实例
        """
        if isinstance(config, dict):
            config = FeishuConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: FeishuConfig = config
        self._client: Any = None
        self._ws_client: Any = None
        self._ws_thread: threading.Thread | None = None
        self._processed_message_ids: OrderedDict[str, None] = OrderedDict()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stream_bufs: dict[str, _FeishuStreamBuf] = {}

    @staticmethod
    def _register_optional_event(builder: Any, method_name: str, handler: Any) -> Any:
        """
        仅当 SDK 支持时注册事件处理器。

        参数：
            builder: 事件分发器构建器
            method_name: 注册方法名
            handler: 事件处理函数

        返回：
            更新后的构建器
        """
        method = getattr(builder, method_name, None)
        return method(handler) if callable(method) else builder

    async def start(self) -> None:
        """
        启动飞书机器人，使用 WebSocket 长连接。

        初始化 Lark 客户端和 WebSocket 连接，开始接收消息事件。

        启动流程：
            1. 检查 SDK 可用性和配置完整性
            2. 创建 Lark 客户端用于发送消息
            3. 创建事件分发器并注册消息处理器
            4. 在独立线程中启动 WebSocket 客户端
            5. 进入主循环等待停止信号
        """
        if not FEISHU_AVAILABLE:
            logger.error("Feishu SDK not installed. Run: pip install lark-oapi")
            return

        if not self.config.app_id or not self.config.app_secret:
            logger.error("Feishu app_id and app_secret not configured")
            return

        import lark_oapi as lark
        self._running = True
        self._loop = asyncio.get_running_loop()

        self._client = lark.Client.builder() \
            .app_id(self.config.app_id) \
            .app_secret(self.config.app_secret) \
            .log_level(lark.LogLevel.INFO) \
            .build()
        builder = lark.EventDispatcherHandler.builder(
            self.config.encrypt_key or "",
            self.config.verification_token or "",
        ).register_p2_im_message_receive_v1(
            self._on_message_sync
        )
        builder = self._register_optional_event(
            builder, "register_p2_im_message_reaction_created_v1", self._on_reaction_created
        )
        builder = self._register_optional_event(
            builder, "register_p2_im_message_message_read_v1", self._on_message_read
        )
        builder = self._register_optional_event(
            builder,
            "register_p2_im_chat_access_event_bot_p2p_chat_entered_v1",
            self._on_bot_p2p_chat_entered,
        )
        event_handler = builder.build()

        self._ws_client = lark.ws.Client(
            self.config.app_id,
            self.config.app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.INFO
        )

        def run_ws():
            import time
            import lark_oapi.ws.client as _lark_ws_client
            ws_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(ws_loop)
            _lark_ws_client.loop = ws_loop
            try:
                while self._running:
                    try:
                        self._ws_client.start()
                    except Exception as e:
                        logger.warning("Feishu WebSocket error: {}", e)
                    if self._running:
                        time.sleep(5)
            finally:
                ws_loop.close()

        self._ws_thread = threading.Thread(target=run_ws, daemon=True)
        self._ws_thread.start()

        logger.info("Feishu bot started with WebSocket long connection")
        logger.info("No public IP required - using WebSocket to receive events")

        while self._running:
            await asyncio.sleep(1)

    async def stop(self) -> None:
        """
        停止飞书机器人。

        注意：lark.ws.Client 没有暴露 stop 方法，直接退出程序即可关闭客户端。
        参考：https://github.com/larksuite/oapi-sdk-python/blob/v2_main/lark_oapi/ws/client.py#L86
        """
        self._running = False
        logger.info("Feishu bot stopped")

    def _is_bot_mentioned(self, message: Any) -> bool:
        """
        检查机器人是否在消息中被 @ 提及。

        参数：
            message: 飞书消息对象

        返回：
            被 @ 提及返回 True
        """
        raw_content = message.content or ""
        if "@_all" in raw_content:
            return True

        for mention in getattr(message, "mentions", None) or []:
            mid = getattr(mention, "id", None)
            if not mid:
                continue
            if not getattr(mid, "user_id", None) and (getattr(mid, "open_id", None) or "").startswith("ou_"):
                return True
        return False

    def _is_group_message_for_bot(self, message: Any) -> bool:
        """
        根据策略判断是否应处理群聊消息。

        参数：
            message: 飞书消息对象

        返回：
            应处理返回 True
        """
        if self.config.group_policy == "open":
            return True
        return self._is_bot_mentioned(message)

    def _add_reaction_sync(self, message_id: str, emoji_type: str) -> None:
        """
        添加表情反应的同步辅助方法（在线程池中运行）。

        参数：
            message_id: 消息 ID
            emoji_type: 表情类型
        """
        from lark_oapi.api.im.v1 import CreateMessageReactionRequest, CreateMessageReactionRequestBody, Emoji
        try:
            request = CreateMessageReactionRequest.builder() \
                .message_id(message_id) \
                .request_body(
                    CreateMessageReactionRequestBody.builder()
                    .reaction_type(Emoji.builder().emoji_type(emoji_type).build())
                    .build()
                ).build()

            response = self._client.im.v1.message_reaction.create(request)

            if not response.success():
                logger.warning("Failed to add reaction: code={}, msg={}", response.code, response.msg)
            else:
                logger.debug("Added {} reaction to message {}", emoji_type, message_id)
        except Exception as e:
            logger.warning("Error adding reaction: {}", e)

    async def _add_reaction(self, message_id: str, emoji_type: str = "THUMBSUP") -> None:
        """
        添加表情反应到消息（非阻塞）。

        参数：
            message_id: 消息 ID
            emoji_type: 表情类型，常用值：THUMBSUP, OK, EYES, DONE, OnIt, HEART
        """
        if not self._client:
            return

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._add_reaction_sync, message_id, emoji_type)

    _TABLE_RE = re.compile(
        r"((?:^[ \t]*\|.+\|[ \t]*\n)(?:^[ \t]*\|[-:\s|]+\|[ \t]*\n)(?:^[ \t]*\|.+\|[ \t]*\n?)+)",
        re.MULTILINE,
    )

    _HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)

    _CODE_BLOCK_RE = re.compile(r"(```[\s\S]*?```)", re.MULTILINE)

    _MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
    _MD_BOLD_UNDERSCORE_RE = re.compile(r"__(.+?)__")
    _MD_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
    _MD_STRIKE_RE = re.compile(r"~~(.+?)~~")

    @classmethod
    def _strip_md_formatting(cls, text: str) -> str:
        """
        移除 Markdown 格式标记，用于纯文本显示。

        飞书表格单元格不支持 Markdown 渲染，需要移除格式标记保持可读性。

        参数：
            text: 包含 Markdown 格式的文本

        返回：
            移除格式标记后的文本
        """
        text = cls._MD_BOLD_RE.sub(r"\1", text)
        text = cls._MD_BOLD_UNDERSCORE_RE.sub(r"\1", text)
        text = cls._MD_ITALIC_RE.sub(r"\1", text)
        text = cls._MD_STRIKE_RE.sub(r"\1", text)
        return text

    @classmethod
    def _parse_md_table(cls, table_text: str) -> dict | None:
        """
        将 Markdown 表格解析为飞书表格元素。

        参数：
            table_text: Markdown 表格文本

        返回：
            飞书表格元素字典，解析失败返回 None
        """
        lines = [_line.strip() for _line in table_text.strip().split("\n") if _line.strip()]
        if len(lines) < 3:
            return None
        def split(_line: str) -> list[str]:
            return [c.strip() for c in _line.strip("|").split("|")]
        headers = [cls._strip_md_formatting(h) for h in split(lines[0])]
        rows = [[cls._strip_md_formatting(c) for c in split(_line)] for _line in lines[2:]]
        columns = [{"tag": "column", "name": f"c{i}", "display_name": h, "width": "auto"}
                   for i, h in enumerate(headers)]
        return {
            "tag": "table",
            "page_size": len(rows) + 1,
            "columns": columns,
            "rows": [{f"c{i}": r[i] if i < len(r) else "" for i in range(len(headers))} for r in rows],
        }

    def _build_card_elements(self, content: str) -> list[dict]:
        """
        将内容分割为 div/markdown 和表格元素，用于飞书卡片。

        参数：
            content: Markdown 内容

        返回：
            卡片元素列表
        """
        elements, last_end = [], 0
        for m in self._TABLE_RE.finditer(content):
            before = content[last_end:m.start()]
            if before.strip():
                elements.extend(self._split_headings(before))
            elements.append(self._parse_md_table(m.group(1)) or {"tag": "markdown", "content": m.group(1)})
            last_end = m.end()
        remaining = content[last_end:]
        if remaining.strip():
            elements.extend(self._split_headings(remaining))
        return elements or [{"tag": "markdown", "content": content}]

    @staticmethod
    def _split_elements_by_table_limit(elements: list[dict], max_tables: int = 1) -> list[list[dict]]:
        """
        将卡片元素分组，每组最多包含指定数量的表格元素。

        飞书卡片有硬性限制：每张卡片最多一个表格（API 错误 11310）。
        当渲染内容包含多个 Markdown 表格时，每个表格放在单独的卡片消息中。

        参数：
            elements: 卡片元素列表
            max_tables: 每组最大表格数

        返回：
            分组后的元素列表
        """
        if not elements:
            return [[]]
        groups: list[list[dict]] = []
        current: list[dict] = []
        table_count = 0
        for el in elements:
            if el.get("tag") == "table":
                if table_count >= max_tables:
                    if current:
                        groups.append(current)
                    current = []
                    table_count = 0
                current.append(el)
                table_count += 1
            else:
                current.append(el)
        if current:
            groups.append(current)
        return groups or [[]]

    def _split_headings(self, content: str) -> list[dict]:
        """
        按标题分割内容，将标题转换为 div 元素。

        参数：
            content: Markdown 内容

        返回：
            卡片元素列表
        """
        protected = content
        code_blocks = []
        for m in self._CODE_BLOCK_RE.finditer(content):
            code_blocks.append(m.group(1))
            protected = protected.replace(m.group(1), f"\x00CODE{len(code_blocks)-1}\x00", 1)

        elements = []
        last_end = 0
        for m in self._HEADING_RE.finditer(protected):
            before = protected[last_end:m.start()].strip()
            if before:
                elements.append({"tag": "markdown", "content": before})
            text = self._strip_md_formatting(m.group(2).strip())
            display_text = f"**{text}**" if text else ""
            elements.append({
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": display_text,
                },
            })
            last_end = m.end()
        remaining = protected[last_end:].strip()
        if remaining:
            elements.append({"tag": "markdown", "content": remaining})

        for i, cb in enumerate(code_blocks):
            for el in elements:
                if el.get("tag") == "markdown":
                    el["content"] = el["content"].replace(f"\x00CODE{i}\x00", cb)

        return elements or [{"tag": "markdown", "content": content}]

    _COMPLEX_MD_RE = re.compile(
        r"```"
        r"|^\|.+\|.*\n\s*\|[-:\s|]+\|"
        r"|^#{1,6}\s+",
        re.MULTILINE,
    )

    _SIMPLE_MD_RE = re.compile(
        r"\*\*.+?\*\*"
        r"|__.+?__"
        r"|(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)"
        r"|~~.+?~~",
        re.DOTALL,
    )

    _MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\)]+)\)")

    _LIST_RE = re.compile(r"^[\s]*[-*+]\s+", re.MULTILINE)

    _OLIST_RE = re.compile(r"^[\s]*\d+\.\s+", re.MULTILINE)

    _TEXT_MAX_LEN = 200

    _POST_MAX_LEN = 2000

    @classmethod
    def _detect_msg_format(cls, content: str) -> str:
        """
        检测内容的最佳飞书消息格式。

        返回值：
            - "text": 纯文本，短且无 Markdown
            - "post": 富文本，仅链接，中等长度
            - "interactive": 卡片，完整 Markdown 渲染

        参数：
            content: 消息内容

        返回：
            格式类型字符串
        """
        stripped = content.strip()

        if cls._COMPLEX_MD_RE.search(stripped):
            return "interactive"

        if len(stripped) > cls._POST_MAX_LEN:
            return "interactive"

        if cls._SIMPLE_MD_RE.search(stripped):
            return "interactive"

        if cls._LIST_RE.search(stripped) or cls._OLIST_RE.search(stripped):
            return "interactive"

        if cls._MD_LINK_RE.search(stripped):
            return "post"

        if len(stripped) <= cls._TEXT_MAX_LEN:
            return "text"

        return "post"

    @classmethod
    def _markdown_to_post(cls, content: str) -> str:
        """
        将 Markdown 内容转换为飞书富文本消息 JSON。

        处理链接 [text](url) 为 a 标签，其他为 text 标签。
        每行成为富文本中的一个段落（行）。

        参数：
            content: Markdown 内容

        返回：
            飞书富文本 JSON 字符串
        """
        lines = content.strip().split("\n")
        paragraphs: list[list[dict]] = []

        for line in lines:
            elements: list[dict] = []
            last_end = 0

            for m in cls._MD_LINK_RE.finditer(line):
                before = line[last_end:m.start()]
                if before:
                    elements.append({"tag": "text", "text": before})
                elements.append({
                    "tag": "a",
                    "text": m.group(1),
                    "href": m.group(2),
                })
                last_end = m.end()

            remaining = line[last_end:]
            if remaining:
                elements.append({"tag": "text", "text": remaining})

            if not elements:
                elements.append({"tag": "text", "text": ""})

            paragraphs.append(elements)

        post_body = {
            "zh_cn": {
                "content": paragraphs,
            }
        }
        return json.dumps(post_body, ensure_ascii=False)

    _IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico", ".tiff", ".tif"}
    _AUDIO_EXTS = {".opus"}
    _VIDEO_EXTS = {".mp4", ".mov", ".avi"}
    _FILE_TYPE_MAP = {
        ".opus": "opus", ".mp4": "mp4", ".pdf": "pdf", ".doc": "doc", ".docx": "doc",
        ".xls": "xls", ".xlsx": "xls", ".ppt": "ppt", ".pptx": "ppt",
    }

    def _upload_image_sync(self, file_path: str) -> str | None:
        """
        上传图片到飞书并返回 image_key。

        参数：
            file_path: 本地图片路径

        返回：
            图片 key，失败返回 None
        """
        from lark_oapi.api.im.v1 import CreateImageRequest, CreateImageRequestBody
        try:
            with open(file_path, "rb") as f:
                request = CreateImageRequest.builder() \
                    .request_body(
                        CreateImageRequestBody.builder()
                        .image_type("message")
                        .image(f)
                        .build()
                    ).build()
                response = self._client.im.v1.image.create(request)
                if response.success():
                    image_key = response.data.image_key
                    logger.debug("Uploaded image {}: {}", os.path.basename(file_path), image_key)
                    return image_key
                else:
                    logger.error("Failed to upload image: code={}, msg={}", response.code, response.msg)
                    return None
        except Exception as e:
            logger.error("Error uploading image {}: {}", file_path, e)
            return None

    def _upload_file_sync(self, file_path: str) -> str | None:
        """
        上传文件到飞书并返回 file_key。

        参数：
            file_path: 本地文件路径

        返回：
            文件 key，失败返回 None
        """
        from lark_oapi.api.im.v1 import CreateFileRequest, CreateFileRequestBody
        ext = os.path.splitext(file_path)[1].lower()
        file_type = self._FILE_TYPE_MAP.get(ext, "stream")
        file_name = os.path.basename(file_path)
        try:
            with open(file_path, "rb") as f:
                request = CreateFileRequest.builder() \
                    .request_body(
                        CreateFileRequestBody.builder()
                        .file_type(file_type)
                        .file_name(file_name)
                        .file(f)
                        .build()
                    ).build()
                response = self._client.im.v1.file.create(request)
                if response.success():
                    file_key = response.data.file_key
                    logger.debug("Uploaded file {}: {}", file_name, file_key)
                    return file_key
                else:
                    logger.error("Failed to upload file: code={}, msg={}", response.code, response.msg)
                    return None
        except Exception as e:
            logger.error("Error uploading file {}: {}", file_path, e)
            return None

    def _download_image_sync(self, message_id: str, image_key: str) -> tuple[bytes | None, str | None]:
        """
        从飞书消息下载图片。

        参数：
            message_id: 消息 ID
            image_key: 图片 key

        返回：
            元组 (图片数据, 文件名)，失败返回 None
        """
        from lark_oapi.api.im.v1 import GetMessageResourceRequest
        try:
            request = GetMessageResourceRequest.builder() \
                .message_id(message_id) \
                .file_key(image_key) \
                .type("image") \
                .build()
            response = self._client.im.v1.message_resource.get(request)
            if response.success():
                file_data = response.file
                if hasattr(file_data, 'read'):
                    file_data = file_data.read()
                return file_data, response.file_name
            else:
                logger.error("Failed to download image: code={}, msg={}", response.code, response.msg)
                return None, None
        except Exception as e:
            logger.error("Error downloading image {}: {}", image_key, e)
            return None, None

    def _download_file_sync(
        self, message_id: str, file_key: str, resource_type: str = "file"
    ) -> tuple[bytes | None, str | None]:
        """
        从飞书消息下载文件/音频/媒体。

        参数：
            message_id: 消息 ID
            file_key: 文件 key
            resource_type: 资源类型

        返回：
            元组 (文件数据, 文件名)，失败返回 None
        """
        from lark_oapi.api.im.v1 import GetMessageResourceRequest

        if resource_type == "audio":
            resource_type = "file"

        try:
            request = (
                GetMessageResourceRequest.builder()
                .message_id(message_id)
                .file_key(file_key)
                .type(resource_type)
                .build()
            )
            response = self._client.im.v1.message_resource.get(request)
            if response.success():
                file_data = response.file
                if hasattr(file_data, "read"):
                    file_data = file_data.read()
                return file_data, response.file_name
            else:
                logger.error("Failed to download {}: code={}, msg={}", resource_type, response.code, response.msg)
                return None, None
        except Exception:
            logger.exception("Error downloading {} {}", resource_type, file_key)
            return None, None

    async def _download_and_save_media(
        self,
        msg_type: str,
        content_json: dict,
        message_id: str | None = None
    ) -> tuple[str | None, str]:
        """
        从飞书下载媒体并保存到本地磁盘。

        参数：
            msg_type: 消息类型
            content_json: 消息内容 JSON
            message_id: 消息 ID

        返回：
            元组 (文件路径, 内容文本)，下载失败时文件路径为 None
        """
        loop = asyncio.get_running_loop()
        media_dir = get_media_dir("feishu")

        data, filename = None, None

        if msg_type == "image":
            image_key = content_json.get("image_key")
            if image_key and message_id:
                data, filename = await loop.run_in_executor(
                    None, self._download_image_sync, message_id, image_key
                )
                if not filename:
                    filename = f"{image_key[:16]}.jpg"

        elif msg_type in ("audio", "file", "media"):
            file_key = content_json.get("file_key")
            if file_key and message_id:
                data, filename = await loop.run_in_executor(
                    None, self._download_file_sync, message_id, file_key, msg_type
                )
                if not filename:
                    filename = file_key[:16]
                if msg_type == "audio" and not filename.endswith(".opus"):
                    filename = f"{filename}.opus"

        if data and filename:
            file_path = media_dir / filename
            file_path.write_bytes(data)
            logger.debug("Downloaded {} to {}", msg_type, file_path)
            return str(file_path), f"[{msg_type}: {filename}]"

        return None, f"[{msg_type}: download failed]"

    _REPLY_CONTEXT_MAX_LEN = 200

    def _get_message_content_sync(self, message_id: str) -> str | None:
        """
        获取飞书消息的文本内容（同步方法）。

        参数：
            message_id: 消息 ID

        返回：
            "[Reply to: ...]" 格式的上下文字符串，失败返回 None
        """
        from lark_oapi.api.im.v1 import GetMessageRequest
        try:
            request = GetMessageRequest.builder().message_id(message_id).build()
            response = self._client.im.v1.message.get(request)
            if not response.success():
                logger.debug(
                    "Feishu: could not fetch parent message {}: code={}, msg={}",
                    message_id, response.code, response.msg,
                )
                return None
            items = getattr(response.data, "items", None)
            if not items:
                return None
            msg_obj = items[0]
            raw_content = getattr(msg_obj, "body", None)
            raw_content = getattr(raw_content, "content", None) if raw_content else None
            if not raw_content:
                return None
            try:
                content_json = json.loads(raw_content)
            except (json.JSONDecodeError, TypeError):
                return None
            msg_type = getattr(msg_obj, "msg_type", "")
            if msg_type == "text":
                text = content_json.get("text", "").strip()
            elif msg_type == "post":
                text, _ = _extract_post_content(content_json)
                text = text.strip()
            else:
                text = ""
            if not text:
                return None
            if len(text) > self._REPLY_CONTEXT_MAX_LEN:
                text = text[: self._REPLY_CONTEXT_MAX_LEN] + "..."
            return f"[Reply to: {text}]"
        except Exception as e:
            logger.debug("Feishu: error fetching parent message {}: {}", message_id, e)
            return None

    def _reply_message_sync(self, parent_message_id: str, msg_type: str, content: str) -> bool:
        """
        回复飞书消息（同步方法）。

        参数：
            parent_message_id: 父消息 ID
            msg_type: 消息类型
            content: 消息内容

        返回：
            成功返回 True
        """
        from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody
        try:
            request = ReplyMessageRequest.builder() \
                .message_id(parent_message_id) \
                .request_body(
                    ReplyMessageRequestBody.builder()
                    .msg_type(msg_type)
                    .content(content)
                    .build()
                ).build()
            response = self._client.im.v1.message.reply(request)
            if not response.success():
                logger.error(
                    "Failed to reply to Feishu message {}: code={}, msg={}, log_id={}",
                    parent_message_id, response.code, response.msg, response.get_log_id()
                )
                return False
            logger.debug("Feishu reply sent to message {}", parent_message_id)
            return True
        except Exception as e:
            logger.error("Error replying to Feishu message {}: {}", parent_message_id, e)
            return False

    def _send_message_sync(self, receive_id_type: str, receive_id: str, msg_type: str, content: str) -> str | None:
        """
        发送单条消息并返回消息 ID。

        参数：
            receive_id_type: 接收者 ID 类型
            receive_id: 接收者 ID
            msg_type: 消息类型
            content: 消息内容

        返回：
            消息 ID，失败返回 None
        """
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
        try:
            request = CreateMessageRequest.builder() \
                .receive_id_type(receive_id_type) \
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(receive_id)
                    .msg_type(msg_type)
                    .content(content)
                    .build()
                ).build()
            response = self._client.im.v1.message.create(request)
            if not response.success():
                logger.error(
                    "Failed to send Feishu {} message: code={}, msg={}, log_id={}",
                    msg_type, response.code, response.msg, response.get_log_id()
                )
                return None
            msg_id = getattr(response.data, "message_id", None)
            logger.debug("Feishu {} message sent to {}: {}", msg_type, receive_id, msg_id)
            return msg_id
        except Exception as e:
            logger.error("Error sending Feishu {} message: {}", msg_type, e)
            return None

    def _create_streaming_card_sync(self, receive_id_type: str, chat_id: str) -> str | None:
        """
        创建 CardKit 流式卡片并发送到聊天。

        参数：
            receive_id_type: 接收者 ID 类型
            chat_id: 聊天 ID

        返回：
            卡片 ID，失败返回 None
        """
        from lark_oapi.api.cardkit.v1 import CreateCardRequest, CreateCardRequestBody
        card_json = {
            "schema": "2.0",
            "config": {"wide_screen_mode": True, "update_multi": True, "streaming_mode": True},
            "body": {"elements": [{"tag": "markdown", "content": "", "element_id": _STREAM_ELEMENT_ID}]},
        }
        try:
            request = CreateCardRequest.builder().request_body(
                CreateCardRequestBody.builder()
                .type("card_json")
                .data(json.dumps(card_json, ensure_ascii=False))
                .build()
            ).build()
            response = self._client.cardkit.v1.card.create(request)
            if not response.success():
                logger.warning("Failed to create streaming card: code={}, msg={}", response.code, response.msg)
                return None
            card_id = getattr(response.data, "card_id", None)
            if card_id:
                message_id = self._send_message_sync(
                    receive_id_type, chat_id, "interactive",
                    json.dumps({"type": "card", "data": {"card_id": card_id}}),
                )
                if message_id:
                    return card_id
                logger.warning("Created streaming card {} but failed to send it to {}", card_id, chat_id)
            return None
        except Exception as e:
            logger.warning("Error creating streaming card: {}", e)
            return None

    def _stream_update_text_sync(self, card_id: str, content: str, sequence: int) -> bool:
        """
        流式更新卡片上的 Markdown 元素（打字机效果）。

        参数：
            card_id: 卡片 ID
            content: 新内容
            sequence: 序列号

        返回：
            成功返回 True
        """
        from lark_oapi.api.cardkit.v1 import ContentCardElementRequest, ContentCardElementRequestBody
        try:
            request = ContentCardElementRequest.builder() \
                .card_id(card_id) \
                .element_id(_STREAM_ELEMENT_ID) \
                .request_body(
                    ContentCardElementRequestBody.builder()
                    .content(content).sequence(sequence).build()
                ).build()
            response = self._client.cardkit.v1.card_element.content(request)
            if not response.success():
                logger.warning("Failed to stream-update card {}: code={}, msg={}", card_id, response.code, response.msg)
                return False
            return True
        except Exception as e:
            logger.warning("Error stream-updating card {}: {}", card_id, e)
            return False

    def _close_streaming_mode_sync(self, card_id: str, sequence: int) -> bool:
        """
        关闭 CardKit 流式模式，使聊天列表预览退出流式占位符状态。

        根据飞书文档，流式卡片在会话列表中保持生成样式的摘要，
        直到通过卡片设置将 streaming_mode 设为 false（在最终内容更新后）。
        序列号必须严格大于此实体上的上一个卡片 OpenAPI 操作。

        参数：
            card_id: 卡片 ID
            sequence: 序列号

        返回：
            成功返回 True
        """
        from lark_oapi.api.cardkit.v1 import SettingsCardRequest, SettingsCardRequestBody
        settings_payload = json.dumps({"config": {"streaming_mode": False}}, ensure_ascii=False)
        try:
            request = SettingsCardRequest.builder() \
                .card_id(card_id) \
                .request_body(
                    SettingsCardRequestBody.builder()
                    .settings(settings_payload)
                    .sequence(sequence)
                    .uuid(str(uuid.uuid4()))
                    .build()
                ).build()
            response = self._client.cardkit.v1.card.settings(request)
            if not response.success():
                logger.warning(
                    "Failed to close streaming on card {}: code={}, msg={}",
                    card_id, response.code, response.msg,
                )
                return False
            return True
        except Exception as e:
            logger.warning("Error closing streaming on card {}: {}", card_id, e)
            return False

    async def send_delta(self, chat_id: str, delta: str, metadata: dict[str, Any] | None = None) -> None:
        """
        通过 CardKit 进行渐进式流式传输：首次 delta 创建卡片，后续更新内容。

        参数：
            chat_id: 聊天 ID
            delta: 增量文本内容
            metadata: 元数据（包含 _stream_end 标记）
        """
        if not self._client:
            return
        meta = metadata or {}
        loop = asyncio.get_running_loop()
        rid_type = "chat_id" if chat_id.startswith("oc_") else "open_id"

        if meta.get("_stream_end"):
            buf = self._stream_bufs.pop(chat_id, None)
            if not buf or not buf.text:
                return
            if buf.card_id:
                buf.sequence += 1
                await loop.run_in_executor(
                    None, self._stream_update_text_sync, buf.card_id, buf.text, buf.sequence,
                )
                buf.sequence += 1
                await loop.run_in_executor(
                    None, self._close_streaming_mode_sync, buf.card_id, buf.sequence,
                )
            else:
                for chunk in self._split_elements_by_table_limit(self._build_card_elements(buf.text)):
                    card = json.dumps({"config": {"wide_screen_mode": True}, "elements": chunk}, ensure_ascii=False)
                    await loop.run_in_executor(None, self._send_message_sync, rid_type, chat_id, "interactive", card)
            return

        buf = self._stream_bufs.get(chat_id)
        if buf is None:
            buf = _FeishuStreamBuf()
            self._stream_bufs[chat_id] = buf
        buf.text += delta
        if not buf.text.strip():
            return

        now = time.monotonic()
        if buf.card_id is None:
            card_id = await loop.run_in_executor(None, self._create_streaming_card_sync, rid_type, chat_id)
            if card_id:
                buf.card_id = card_id
                buf.sequence = 1
                await loop.run_in_executor(None, self._stream_update_text_sync, card_id, buf.text, 1)
                buf.last_edit = now
        elif (now - buf.last_edit) >= self._STREAM_EDIT_INTERVAL:
            buf.sequence += 1
            await loop.run_in_executor(None, self._stream_update_text_sync, buf.card_id, buf.text, buf.sequence)
            buf.last_edit = now

    async def send(self, msg: OutboundMessage) -> None:
        """
        通过飞书发送消息，包括媒体（图片/文件）。

        参数：
            msg: 出站消息对象

        处理流程：
            1. 确定接收者 ID 类型
            2. 处理工具提示消息
            3. 发送媒体附件
            4. 根据内容格式发送文本消息
        """
        if not self._client:
            logger.warning("Feishu client not initialized")
            return

        try:
            receive_id_type = "chat_id" if msg.chat_id.startswith("oc_") else "open_id"
            loop = asyncio.get_running_loop()

            if msg.metadata.get("_tool_hint"):
                if msg.content and msg.content.strip():
                    await self._send_tool_hint_card(
                        receive_id_type, msg.chat_id, msg.content.strip()
                    )
                return

            reply_message_id: str | None = None
            if (
                self.config.reply_to_message
                and not msg.metadata.get("_progress", False)
            ):
                reply_message_id = msg.metadata.get("message_id") or None
            elif msg.metadata.get("thread_id"):
                reply_message_id = msg.metadata.get("root_id") or msg.metadata.get("message_id") or None

            first_send = True

            def _do_send(m_type: str, content: str) -> None:
                """通过回复（首条消息）或创建（后续消息）发送。"""
                nonlocal first_send
                if reply_message_id and first_send:
                    first_send = False
                    ok = self._reply_message_sync(reply_message_id, m_type, content)
                    if ok:
                        return
                self._send_message_sync(receive_id_type, msg.chat_id, m_type, content)

            for file_path in msg.media:
                if not os.path.isfile(file_path):
                    logger.warning("Media file not found: {}", file_path)
                    continue
                ext = os.path.splitext(file_path)[1].lower()
                if ext in self._IMAGE_EXTS:
                    key = await loop.run_in_executor(None, self._upload_image_sync, file_path)
                    if key:
                        await loop.run_in_executor(
                            None, _do_send,
                            "image", json.dumps({"image_key": key}, ensure_ascii=False),
                        )
                else:
                    key = await loop.run_in_executor(None, self._upload_file_sync, file_path)
                    if key:
                        if ext in self._AUDIO_EXTS:
                            media_type = "audio"
                        elif ext in self._VIDEO_EXTS:
                            media_type = "video"
                        else:
                            media_type = "file"
                        await loop.run_in_executor(
                            None, _do_send,
                            media_type, json.dumps({"file_key": key}, ensure_ascii=False),
                        )

            if msg.content and msg.content.strip():
                fmt = self._detect_msg_format(msg.content)

                if fmt == "text":
                    text_body = json.dumps({"text": msg.content.strip()}, ensure_ascii=False)
                    await loop.run_in_executor(None, _do_send, "text", text_body)

                elif fmt == "post":
                    post_body = self._markdown_to_post(msg.content)
                    await loop.run_in_executor(None, _do_send, "post", post_body)

                else:
                    elements = self._build_card_elements(msg.content)
                    for chunk in self._split_elements_by_table_limit(elements):
                        card = {"config": {"wide_screen_mode": True}, "elements": chunk}
                        await loop.run_in_executor(
                            None, _do_send,
                            "interactive", json.dumps(card, ensure_ascii=False),
                        )

        except Exception as e:
            logger.error("Error sending Feishu message: {}", e)
            raise

    def _on_message_sync(self, data: Any) -> None:
        """
        传入消息的同步处理器（从 WebSocket 线程调用）。

        在主事件循环中调度异步处理。
        """
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._on_message(data), self._loop)

    async def _on_message(self, data: Any) -> None:
        """
        处理来自飞书的传入消息。

        参数：
            data: 飞书事件数据

        处理流程：
            1. 消息去重
            2. 过滤机器人消息
            3. 检查群聊策略
            4. 添加表情反应
            5. 解析消息内容
            6. 下载媒体文件
            7. 转发到消息总线
        """
        try:
            event = data.event
            message = event.message
            sender = event.sender

            message_id = message.message_id
            if message_id in self._processed_message_ids:
                return
            self._processed_message_ids[message_id] = None

            while len(self._processed_message_ids) > 1000:
                self._processed_message_ids.popitem(last=False)

            if sender.sender_type == "bot":
                return

            sender_id = sender.sender_id.open_id if sender.sender_id else "unknown"
            chat_id = message.chat_id
            chat_type = message.chat_type
            msg_type = message.message_type

            if chat_type == "group" and not self._is_group_message_for_bot(message):
                logger.debug("Feishu: skipping group message (not mentioned)")
                return

            await self._add_reaction(message_id, self.config.react_emoji)

            content_parts = []
            media_paths = []

            try:
                content_json = json.loads(message.content) if message.content else {}
            except json.JSONDecodeError:
                content_json = {}

            if msg_type == "text":
                text = content_json.get("text", "")
                if text:
                    content_parts.append(text)

            elif msg_type == "post":
                text, image_keys = _extract_post_content(content_json)
                if text:
                    content_parts.append(text)
                for img_key in image_keys:
                    file_path, content_text = await self._download_and_save_media(
                        "image", {"image_key": img_key}, message_id
                    )
                    if file_path:
                        media_paths.append(file_path)
                    content_parts.append(content_text)

            elif msg_type in ("image", "audio", "file", "media"):
                file_path, content_text = await self._download_and_save_media(msg_type, content_json, message_id)
                if file_path:
                    media_paths.append(file_path)

                if msg_type == "audio" and file_path:
                    transcription = await self.transcribe_audio(file_path)
                    if transcription:
                        content_text = f"[transcription: {transcription}]"

                content_parts.append(content_text)

            elif msg_type in ("share_chat", "share_user", "interactive", "share_calendar_event", "system", "merge_forward"):
                text = _extract_share_card_content(content_json, msg_type)
                if text:
                    content_parts.append(text)

            else:
                content_parts.append(MSG_TYPE_MAP.get(msg_type, f"[{msg_type}]"))

            parent_id = getattr(message, "parent_id", None) or None
            root_id = getattr(message, "root_id", None) or None
            thread_id = getattr(message, "thread_id", None) or None

            if parent_id and self._client:
                loop = asyncio.get_running_loop()
                reply_ctx = await loop.run_in_executor(
                    None, self._get_message_content_sync, parent_id
                )
                if reply_ctx:
                    content_parts.insert(0, reply_ctx)

            content = "\n".join(content_parts) if content_parts else ""

            if not content and not media_paths:
                return

            reply_to = chat_id if chat_type == "group" else sender_id
            await self._handle_message(
                sender_id=sender_id,
                chat_id=reply_to,
                content=content,
                media=media_paths,
                metadata={
                    "message_id": message_id,
                    "chat_type": chat_type,
                    "msg_type": msg_type,
                    "parent_id": parent_id,
                    "root_id": root_id,
                    "thread_id": thread_id,
                }
            )

        except Exception as e:
            logger.error("Error processing Feishu message: {}", e)

    def _on_reaction_created(self, data: Any) -> None:
        """忽略表情反应事件，避免产生 SDK 噪音。"""
        pass

    def _on_message_read(self, data: Any) -> None:
        """忽略已读事件，避免产生 SDK 噪音。"""
        pass

    def _on_bot_p2p_chat_entered(self, data: Any) -> None:
        """忽略用户打开机器人聊天窗口时的 p2p-enter 事件。"""
        logger.debug("Bot entered p2p chat (user opened chat window)")
        pass

    @staticmethod
    def _format_tool_hint_lines(tool_hint: str) -> str:
        """
        将工具提示按顶级调用分隔符分行显示。

        参数：
            tool_hint: 工具提示字符串

        返回：
            格式化后的多行字符串
        """
        parts: list[str] = []
        buf: list[str] = []
        depth = 0
        in_string = False
        quote_char = ""
        escaped = False

        for i, ch in enumerate(tool_hint):
            buf.append(ch)

            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == quote_char:
                    in_string = False
                continue

            if ch in {'"', "'"}:
                in_string = True
                quote_char = ch
                continue

            if ch == "(":
                depth += 1
                continue

            if ch == ")" and depth > 0:
                depth -= 1
                continue

            if ch == "," and depth == 0:
                next_char = tool_hint[i + 1] if i + 1 < len(tool_hint) else ""
                if next_char == " ":
                    parts.append("".join(buf).rstrip())
                    buf = []

        if buf:
            parts.append("".join(buf).strip())

        return "\n".join(part for part in parts if part)

    async def _send_tool_hint_card(self, receive_id_type: str, receive_id: str, tool_hint: str) -> None:
        """
        将工具提示作为交互卡片发送，包含格式化的代码块。

        参数：
            receive_id_type: "chat_id" 或 "open_id"
            receive_id: 目标聊天或用户 ID
            tool_hint: 格式化的工具提示字符串
        """
        loop = asyncio.get_running_loop()

        formatted_code = self._format_tool_hint_lines(tool_hint)

        card = {
            "config": {"wide_screen_mode": True},
            "elements": [
                {
                    "tag": "markdown",
                    "content": f"**Tool Calls**\n\n```text\n{formatted_code}\n```"
                }
            ]
        }

        await loop.run_in_executor(
            None, self._send_message_sync,
            receive_id_type, receive_id, "interactive",
            json.dumps(card, ensure_ascii=False),
        )
