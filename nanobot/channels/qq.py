"""
QQ 频道实现，使用 botpy SDK。

该模块实现了 nanobot 与 QQ 机器人的集成，支持：
- 私聊（C2C）和群聊消息
- 媒体附件下载和上传
- 富媒体消息发送（图片、文件）

入站流程：
    - 解析 QQ botpy 消息（私聊/群聊）
    - 使用分块流式写入下载附件（内存安全）
    - 通过 BaseChannel._handle_message() 发布到消息总线
    - 内容包含清晰的"已接收文件"列表和本地路径

出站流程：
    - 先发送附件（msg.media）通过 QQ 富媒体 API（base64 上传 + msg_type=7）
    - 然后发送文本（纯文本或 Markdown）
    - msg.media 支持本地路径、file:// 路径和 http(s) URL

注意事项：
    - QQ 限制多种音频/视频格式，保守分类为图片或文件
    - 附件结构在 botpy 版本间有所不同，尝试多个字段候选

依赖：
    - qq-botpy: QQ 机器人 Python SDK

配置说明：
    - app_id: QQ 机器人应用 ID
    - secret: QQ 机器人应用密钥
    - allow_from: 允许访问的用户 ID 列表
    - msg_format: 消息格式 ("plain" 或 "markdown")
    - media_dir: 入站附件保存目录
"""

from __future__ import annotations

import asyncio
import base64
import mimetypes
import os
import re
import time
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import unquote, urlparse

import aiohttp
from loguru import logger
from pydantic import Field

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import Base
from nanobot.security.network import validate_url_target

try:
    from nanobot.config.paths import get_media_dir
except Exception:
    get_media_dir = None

try:
    import botpy
    from botpy.http import Route

    QQ_AVAILABLE = True
except ImportError:
    QQ_AVAILABLE = False
    botpy = None
    Route = None

if TYPE_CHECKING:
    from botpy.message import BaseMessage, C2CMessage, GroupMessage
    from botpy.types.message import Media


QQ_FILE_TYPE_IMAGE = 1
QQ_FILE_TYPE_FILE = 4

_IMAGE_EXTS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".bmp",
    ".webp",
    ".tif",
    ".tiff",
    ".ico",
    ".svg",
}

_SAFE_NAME_RE = re.compile(r"[^\w.\-()\[\]（）【】\u4e00-\u9fff]+", re.UNICODE)


def _sanitize_filename(name: str) -> str:
    """
    清理文件名，避免路径遍历和问题字符。

    参数：
        name: 原始文件名

    返回：
        清理后的安全文件名
    """
    name = (name or "").strip()
    name = Path(name).name
    name = _SAFE_NAME_RE.sub("_", name).strip("._ ")
    return name


def _is_image_name(name: str) -> bool:
    """检查文件名是否为图片类型。"""
    return Path(name).suffix.lower() in _IMAGE_EXTS


def _guess_send_file_type(filename: str) -> int:
    """
    保守估计发送类型：图片返回 1，其他返回 4。

    参数：
        filename: 文件名

    返回：
        QQ 文件类型常量
    """
    ext = Path(filename).suffix.lower()
    mime, _ = mimetypes.guess_type(filename)
    if ext in _IMAGE_EXTS or (mime and mime.startswith("image/")):
        return QQ_FILE_TYPE_IMAGE
    return QQ_FILE_TYPE_FILE


def _make_bot_class(channel: QQChannel) -> type[botpy.Client]:
    """
    创建绑定到指定频道的 botpy Client 子类。

    参数：
        channel: QQChannel 实例

    返回：
        botpy.Client 子类
    """
    intents = botpy.Intents(public_messages=True, direct_message=True)

    class _Bot(botpy.Client):
        def __init__(self):
            super().__init__(intents=intents, ext_handlers=False)

        async def on_ready(self):
            """机器人就绪回调。"""
            logger.info("QQ bot ready: {}", self.robot.name)

        async def on_c2c_message_create(self, message: C2CMessage):
            """私聊消息回调。"""
            await channel._on_message(message, is_group=False)

        async def on_group_at_message_create(self, message: GroupMessage):
            """群聊 @ 消息回调。"""
            await channel._on_message(message, is_group=True)

        async def on_direct_message_create(self, message):
            """直接消息回调。"""
            await channel._on_message(message, is_group=False)

    return _Bot


class QQConfig(Base):
    """
    QQ 频道配置模型，使用 botpy SDK。

    属性：
        enabled: 是否启用此频道
        app_id: QQ 机器人应用 ID
        secret: QQ 机器人应用密钥
        allow_from: 允许访问的用户 ID 列表
        msg_format: 消息格式 ("plain" 或 "markdown")
        media_dir: 入站附件保存目录（为空则使用 nanobot 默认目录）
        download_chunk_size: 下载分块大小（字节）
        download_max_bytes: 最大下载大小（字节）
    """

    enabled: bool = False
    app_id: str = ""
    secret: str = ""
    allow_from: list[str] = Field(default_factory=list)
    msg_format: Literal["plain", "markdown"] = "plain"
    media_dir: str = ""
    download_chunk_size: int = 1024 * 256
    download_max_bytes: int = 1024 * 1024 * 200


class QQChannel(BaseChannel):
    """
    QQ 频道实现，使用 botpy SDK 和 WebSocket 连接。

    支持私聊（C2C）和群聊消息，支持媒体附件。

    属性：
        name: 频道标识符
        display_name: 频道显示名称
    """

    name = "qq"
    display_name = "QQ"

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        """返回默认配置字典。"""
        return QQConfig().model_dump(by_alias=True)

    def __init__(self, config: Any, bus: MessageBus):
        """
        初始化 QQ 频道实例。

        参数：
            config: 频道配置
            bus: 消息总线实例
        """
        if isinstance(config, dict):
            config = QQConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: QQConfig = config

        self._client: botpy.Client | None = None
        self._http: aiohttp.ClientSession | None = None

        self._processed_ids: deque[str] = deque(maxlen=1000)
        self._msg_seq: int = 1
        self._chat_type_cache: dict[str, str] = {}

        self._media_root: Path = self._init_media_root()

    def _init_media_root(self) -> Path:
        """
        选择入站附件保存目录。

        返回：
            媒体目录路径
        """
        if self.config.media_dir:
            root = Path(self.config.media_dir).expanduser()
        elif get_media_dir:
            try:
                root = Path(get_media_dir("qq"))
            except Exception:
                root = Path.home() / ".nanobot" / "media" / "qq"
        else:
            root = Path.home() / ".nanobot" / "media" / "qq"

        root.mkdir(parents=True, exist_ok=True)
        logger.info("QQ media directory: {}", str(root))
        return root

    async def start(self) -> None:
        """
        启动 QQ 机器人，使用自动重连循环。

        启动流程：
            1. 检查 SDK 可用性和配置完整性
            2. 创建 HTTP 客户端
            3. 创建 botpy 客户端
            4. 进入运行循环
        """
        if not QQ_AVAILABLE:
            logger.error("QQ SDK not installed. Run: pip install qq-botpy")
            return

        if not self.config.app_id or not self.config.secret:
            logger.error("QQ app_id and secret not configured")
            return

        self._running = True
        self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120))

        self._client = _make_bot_class(self)()
        logger.info("QQ bot started (C2C & Group supported)")
        await self._run_bot()

    async def _run_bot(self) -> None:
        """运行机器人连接，支持自动重连。"""
        while self._running:
            try:
                await self._client.start(appid=self.config.app_id, secret=self.config.secret)
            except Exception as e:
                logger.warning("QQ bot error: {}", e)
            if self._running:
                logger.info("Reconnecting QQ bot in 5 seconds...")
                await asyncio.sleep(5)

    async def stop(self) -> None:
        """停止机器人并清理资源。"""
        self._running = False
        if self._client:
            try:
                await self._client.close()
            except Exception:
                pass
        self._client = None

        if self._http:
            try:
                await self._http.close()
            except Exception:
                pass
        self._http = None

        logger.info("QQ bot stopped")

    async def send(self, msg: OutboundMessage) -> None:
        """
        发送消息，先发送附件再发送文本。

        参数：
            msg: 出站消息对象

        处理流程：
            1. 发送媒体附件
            2. 发送文本内容
        """
        if not self._client:
            logger.warning("QQ client not initialized")
            return

        msg_id = msg.metadata.get("message_id")
        chat_type = self._chat_type_cache.get(msg.chat_id, "c2c")
        is_group = chat_type == "group"

        for media_ref in msg.media or []:
            ok = await self._send_media(
                chat_id=msg.chat_id,
                media_ref=media_ref,
                msg_id=msg_id,
                is_group=is_group,
            )
            if not ok:
                filename = (
                    os.path.basename(urlparse(media_ref).path)
                    or os.path.basename(media_ref)
                    or "file"
                )
                await self._send_text_only(
                    chat_id=msg.chat_id,
                    is_group=is_group,
                    msg_id=msg_id,
                    content=f"[Attachment send failed: {filename}]",
                )

        if msg.content and msg.content.strip():
            await self._send_text_only(
                chat_id=msg.chat_id,
                is_group=is_group,
                msg_id=msg_id,
                content=msg.content.strip(),
            )

    async def _send_text_only(
        self,
        chat_id: str,
        is_group: bool,
        msg_id: str | None,
        content: str,
    ) -> None:
        """
        发送纯文本/Markdown 消息。

        参数：
            chat_id: 聊天 ID
            is_group: 是否为群聊
            msg_id: 消息 ID（用于回复）
            content: 消息内容
        """
        if not self._client:
            return

        self._msg_seq += 1
        use_markdown = self.config.msg_format == "markdown"
        payload: dict[str, Any] = {
            "msg_type": 2 if use_markdown else 0,
            "msg_id": msg_id,
            "msg_seq": self._msg_seq,
        }
        if use_markdown:
            payload["markdown"] = {"content": content}
        else:
            payload["content"] = content

        if is_group:
            await self._client.api.post_group_message(group_openid=chat_id, **payload)
        else:
            await self._client.api.post_c2c_message(openid=chat_id, **payload)

    async def _send_media(
        self,
        chat_id: str,
        media_ref: str,
        msg_id: str | None,
        is_group: bool,
    ) -> bool:
        """
        读取字节 -> base64 上传 -> msg_type=7 发送。

        参数：
            chat_id: 聊天 ID
            media_ref: 媒体引用（路径或 URL）
            msg_id: 消息 ID
            is_group: 是否为群聊

        返回：
            发送成功返回 True
        """
        if not self._client:
            return False

        data, filename = await self._read_media_bytes(media_ref)
        if not data or not filename:
            return False

        try:
            file_type = _guess_send_file_type(filename)
            file_data_b64 = base64.b64encode(data).decode()

            media_obj = await self._post_base64file(
                chat_id=chat_id,
                is_group=is_group,
                file_type=file_type,
                file_data=file_data_b64,
                file_name=filename,
                srv_send_msg=False,
            )
            if not media_obj:
                logger.error("QQ media upload failed: empty response")
                return False

            self._msg_seq += 1
            if is_group:
                await self._client.api.post_group_message(
                    group_openid=chat_id,
                    msg_type=7,
                    msg_id=msg_id,
                    msg_seq=self._msg_seq,
                    media=media_obj,
                )
            else:
                await self._client.api.post_c2c_message(
                    openid=chat_id,
                    msg_type=7,
                    msg_id=msg_id,
                    msg_seq=self._msg_seq,
                    media=media_obj,
                )

            logger.info("QQ media sent: {}", filename)
            return True
        except Exception as e:
            logger.error("QQ send media failed filename={} err={}", filename, e)
            return False

    async def _read_media_bytes(self, media_ref: str) -> tuple[bytes | None, str | None]:
        """
        从 http(s) 或本地文件路径读取字节。

        参数：
            media_ref: 媒体引用（路径或 URL）

        返回：
            元组 (数据, 文件名)
        """
        media_ref = (media_ref or "").strip()
        if not media_ref:
            return None, None

        if not media_ref.startswith("http://") and not media_ref.startswith("https://"):
            try:
                if media_ref.startswith("file://"):
                    parsed = urlparse(media_ref)
                    raw = parsed.path or parsed.netloc
                    local_path = Path(unquote(raw))
                else:
                    local_path = Path(os.path.expanduser(media_ref))

                if not local_path.is_file():
                    logger.warning("QQ outbound media file not found: {}", str(local_path))
                    return None, None

                data = await asyncio.to_thread(local_path.read_bytes)
                return data, local_path.name
            except Exception as e:
                logger.warning("QQ outbound media read error ref={} err={}", media_ref, e)
                return None, None

        ok, err = validate_url_target(media_ref)
        if not ok:
            logger.warning("QQ outbound media URL validation failed url={} err={}", media_ref, err)
            return None, None

        if not self._http:
            self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120))
        try:
            async with self._http.get(media_ref, allow_redirects=True) as resp:
                if resp.status >= 400:
                    logger.warning(
                        "QQ outbound media download failed status={} url={}",
                        resp.status,
                        media_ref,
                    )
                    return None, None
                data = await resp.read()
                if not data:
                    return None, None
                filename = os.path.basename(urlparse(media_ref).path) or "file.bin"
                return data, filename
        except Exception as e:
            logger.warning("QQ outbound media download error url={} err={}", media_ref, e)
            return None, None

    async def _post_base64file(
        self,
        chat_id: str,
        is_group: bool,
        file_type: int,
        file_data: str,
        file_name: str | None = None,
        srv_send_msg: bool = False,
    ) -> Media:
        """
        上传 base64 编码文件并返回 Media 对象。

        参数：
            chat_id: 聊天 ID
            is_group: 是否为群聊
            file_type: 文件类型常量
            file_data: base64 编码的文件数据
            file_name: 文件名
            srv_send_msg: 是否由服务器发送消息

        返回：
            Media 对象
        """
        if not self._client:
            raise RuntimeError("QQ client not initialized")

        if is_group:
            endpoint = "/v2/groups/{group_openid}/files"
            id_key = "group_openid"
        else:
            endpoint = "/v2/users/{openid}/files"
            id_key = "openid"

        payload = {
            id_key: chat_id,
            "file_type": file_type,
            "file_data": file_data,
            "file_name": file_name,
            "srv_send_msg": srv_send_msg,
        }
        route = Route("POST", endpoint, **{id_key: chat_id})
        return await self._client.api._http.request(route, json=payload)

    async def _on_message(self, data: C2CMessage | GroupMessage, is_group: bool = False) -> None:
        """
        解析入站消息，下载附件，并发布到消息总线。

        参数：
            data: 消息对象
            is_group: 是否为群聊消息
        """
        if data.id in self._processed_ids:
            return
        self._processed_ids.append(data.id)

        if is_group:
            chat_id = data.group_openid
            user_id = data.author.member_openid
            self._chat_type_cache[chat_id] = "group"
        else:
            chat_id = str(
                getattr(data.author, "id", None) or getattr(data.author, "user_openid", "unknown")
            )
            user_id = chat_id
            self._chat_type_cache[chat_id] = "c2c"

        content = (data.content or "").strip()

        attachments = getattr(data, "attachments", None) or []
        media_paths, recv_lines, att_meta = await self._handle_attachments(attachments)

        if recv_lines:
            tag = "[Image]" if any(_is_image_name(Path(p).name) for p in media_paths) else "[File]"
            file_block = "Received files:\n" + "\n".join(recv_lines)
            content = f"{content}\n\n{file_block}".strip() if content else f"{tag}\n{file_block}"

        if not content and not media_paths:
            return

        await self._handle_message(
            sender_id=user_id,
            chat_id=chat_id,
            content=content,
            media=media_paths if media_paths else None,
            metadata={
                "message_id": data.id,
                "attachments": att_meta,
            },
        )

    async def _handle_attachments(
        self,
        attachments: list[BaseMessage._Attachments],
    ) -> tuple[list[str], list[str], list[dict[str, Any]]]:
        """
        提取、下载（分块）并格式化附件供 Agent 使用。

        参数：
            attachments: 附件列表

        返回：
            元组 (媒体路径列表, 接收行列表, 附件元数据列表)
        """
        media_paths: list[str] = []
        recv_lines: list[str] = []
        att_meta: list[dict[str, Any]] = []

        if not attachments:
            return media_paths, recv_lines, att_meta

        for att in attachments:
            url, filename, ctype = att.url, att.filename, att.content_type

            logger.info("Downloading file from QQ: {}", filename or url)
            local_path = await self._download_to_media_dir_chunked(url, filename_hint=filename)

            att_meta.append(
                {
                    "url": url,
                    "filename": filename,
                    "content_type": ctype,
                    "saved_path": local_path,
                }
            )

            if local_path:
                media_paths.append(local_path)
                shown_name = filename or os.path.basename(local_path)
                recv_lines.append(f"- {shown_name}\n  saved: {local_path}")
            else:
                shown_name = filename or url
                recv_lines.append(f"- {shown_name}\n  saved: [download failed]")

        return media_paths, recv_lines, att_meta

    async def _download_to_media_dir_chunked(
        self,
        url: str,
        filename_hint: str = "",
    ) -> str | None:
        """
        使用流式分块写入下载入站附件。

        使用分块流式写入避免将大文件加载到内存。
        强制执行最大下载大小，写入 .part 临时文件，
        成功后原子重命名。

        参数：
            url: 下载 URL
            filename_hint: 文件名提示

        返回：
            本地文件路径，失败返回 None
        """
        if not self._http:
            self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120))

        safe = _sanitize_filename(filename_hint)
        ts = int(time.time() * 1000)
        tmp_path: Path | None = None

        try:
            async with self._http.get(
                url,
                timeout=aiohttp.ClientTimeout(total=120),
                allow_redirects=True,
            ) as resp:
                if resp.status != 200:
                    logger.warning("QQ download failed: status={} url={}", resp.status, url)
                    return None

                ctype = (resp.headers.get("Content-Type") or "").lower()

                ext = Path(urlparse(url).path).suffix
                if not ext:
                    ext = Path(filename_hint).suffix
                if not ext:
                    if "png" in ctype:
                        ext = ".png"
                    elif "jpeg" in ctype or "jpg" in ctype:
                        ext = ".jpg"
                    elif "gif" in ctype:
                        ext = ".gif"
                    elif "webp" in ctype:
                        ext = ".webp"
                    elif "pdf" in ctype:
                        ext = ".pdf"
                    else:
                        ext = ".bin"

                if safe:
                    if not Path(safe).suffix:
                        safe = safe + ext
                    filename = safe
                else:
                    filename = f"qq_file_{ts}{ext}"

                target = self._media_root / filename
                if target.exists():
                    target = self._media_root / f"{target.stem}_{ts}{target.suffix}"

                tmp_path = target.with_suffix(target.suffix + ".part")

                downloaded = 0
                chunk_size = max(1024, int(self.config.download_chunk_size or 262144))
                max_bytes = max(
                    1024 * 1024, int(self.config.download_max_bytes or (200 * 1024 * 1024))
                )

                def _open_tmp():
                    tmp_path.parent.mkdir(parents=True, exist_ok=True)
                    return open(tmp_path, "wb")

                f = await asyncio.to_thread(_open_tmp)
                try:
                    async for chunk in resp.content.iter_chunked(chunk_size):
                        if not chunk:
                            continue
                        downloaded += len(chunk)
                        if downloaded > max_bytes:
                            logger.warning(
                                "QQ download exceeded max_bytes={} url={} -> abort",
                                max_bytes,
                                url,
                            )
                            return None
                        await asyncio.to_thread(f.write, chunk)
                finally:
                    await asyncio.to_thread(f.close)

                await asyncio.to_thread(os.replace, tmp_path, target)
                tmp_path = None
                logger.info("QQ file saved: {}", str(target))
                return str(target)

        except Exception as e:
            logger.error("QQ download error: {}", e)
            return None
        finally:
            if tmp_path is not None:
                try:
                    tmp_path.unlink(missing_ok=True)
                except Exception:
                    pass
