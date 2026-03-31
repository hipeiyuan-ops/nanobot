"""
钉钉频道实现，使用 Stream Mode 进行消息收发。

该模块实现了 nanobot 与钉钉机器人的集成，支持单聊和群聊消息。
使用 dingtalk-stream SDK 通过 WebSocket 接收消息，使用 HTTP API 发送消息。

主要功能：
    - 支持文本、图片、文件、富文本等多种消息类型
    - 支持单聊和群聊消息处理
    - 支持媒体文件上传和下载
    - 自动管理 Access Token 的获取和刷新

依赖：
    - dingtalk-stream: 钉钉 Stream Mode SDK
    - httpx: 异步 HTTP 客户端

配置说明：
    - client_id: 钉钉应用的 Client ID (AppKey)
    - client_secret: 钉钉应用的 Client Secret (AppSecret)
    - allow_from: 允许访问的用户 ID 列表
"""

import asyncio
import json
import mimetypes
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx
from loguru import logger
from pydantic import Field

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import Base

try:
    from dingtalk_stream import (
        AckMessage,
        CallbackHandler,
        CallbackMessage,
        Credential,
        DingTalkStreamClient,
    )
    from dingtalk_stream.chatbot import ChatbotMessage

    DINGTALK_AVAILABLE = True
except ImportError:
    DINGTALK_AVAILABLE = False
    CallbackHandler = object  # type: ignore[assignment,misc]
    CallbackMessage = None  # type: ignore[assignment,misc]
    AckMessage = None  # type: ignore[assignment,misc]
    ChatbotMessage = None  # type: ignore[assignment,misc]


class NanobotDingTalkHandler(CallbackHandler):
    """
    钉钉 Stream SDK 回调处理器。

    解析传入的消息并转发给 Nanobot 频道进行处理。
    支持处理文本、图片、文件、富文本等多种消息类型。

    属性：
        channel: 关联的 DingTalkChannel 实例
    """

    def __init__(self, channel: "DingTalkChannel"):
        """初始化回调处理器。"""
        super().__init__()
        self.channel = channel

    async def process(self, message: CallbackMessage):
        """
        处理传入的流消息。

        参数：
            message: 钉钉 Stream SDK 的回调消息对象

        返回：
            确认消息元组 (status, message)

        处理流程：
            1. 使用 SDK 的 ChatbotMessage 解析消息
            2. 提取文本内容（支持语音识别结果）
            3. 处理图片、文件、富文本等媒体消息
            4. 下载媒体文件到本地
            5. 将消息转发给频道处理
        """
        try:
            chatbot_msg = ChatbotMessage.from_dict(message.data)

            content = ""
            if chatbot_msg.text:
                content = chatbot_msg.text.content.strip()
            elif chatbot_msg.extensions.get("content", {}).get("recognition"):
                content = chatbot_msg.extensions["content"]["recognition"].strip()
            if not content:
                content = message.data.get("text", {}).get("content", "").strip()

            file_paths = []
            if chatbot_msg.message_type == "picture" and chatbot_msg.image_content:
                download_code = chatbot_msg.image_content.download_code
                if download_code:
                    sender_uid = chatbot_msg.sender_staff_id or chatbot_msg.sender_id or "unknown"
                    fp = await self.channel._download_dingtalk_file(download_code, "image.jpg", sender_uid)
                    if fp:
                        file_paths.append(fp)
                        content = content or "[Image]"

            elif chatbot_msg.message_type == "file":
                download_code = message.data.get("content", {}).get("downloadCode") or message.data.get("downloadCode")
                fname = message.data.get("content", {}).get("fileName") or message.data.get("fileName") or "file"
                if download_code:
                    sender_uid = chatbot_msg.sender_staff_id or chatbot_msg.sender_id or "unknown"
                    fp = await self.channel._download_dingtalk_file(download_code, fname, sender_uid)
                    if fp:
                        file_paths.append(fp)
                        content = content or "[File]"

            elif chatbot_msg.message_type == "richText" and chatbot_msg.rich_text_content:
                rich_list = chatbot_msg.rich_text_content.rich_text_list or []
                for item in rich_list:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") == "text":
                        t = item.get("text", "").strip()
                        if t:
                            content = (content + " " + t).strip() if content else t
                    elif item.get("downloadCode"):
                        dc = item["downloadCode"]
                        fname = item.get("fileName") or "file"
                        sender_uid = chatbot_msg.sender_staff_id or chatbot_msg.sender_id or "unknown"
                        fp = await self.channel._download_dingtalk_file(dc, fname, sender_uid)
                        if fp:
                            file_paths.append(fp)
                            content = content or "[File]"

            if file_paths:
                file_list = "\n".join("- " + p for p in file_paths)
                content = content + "\n\nReceived files:\n" + file_list

            if not content:
                logger.warning(
                    "Received empty or unsupported message type: {}",
                    chatbot_msg.message_type,
                )
                return AckMessage.STATUS_OK, "OK"

            sender_id = chatbot_msg.sender_staff_id or chatbot_msg.sender_id
            sender_name = chatbot_msg.sender_nick or "Unknown"

            conversation_type = message.data.get("conversationType")
            conversation_id = (
                message.data.get("conversationId")
                or message.data.get("openConversationId")
            )

            logger.info("Received DingTalk message from {} ({}): {}", sender_name, sender_id, content)

            task = asyncio.create_task(
                self.channel._on_message(
                    content,
                    sender_id,
                    sender_name,
                    conversation_type,
                    conversation_id,
                )
            )
            self.channel._background_tasks.add(task)
            task.add_done_callback(self.channel._background_tasks.discard)

            return AckMessage.STATUS_OK, "OK"

        except Exception as e:
            logger.error("Error processing DingTalk message: {}", e)
            return AckMessage.STATUS_OK, "Error"


class DingTalkConfig(Base):
    """
    钉钉频道配置模型。

    使用 Stream Mode 连接钉钉服务器，需要配置应用的 Client ID 和 Secret。

    属性：
        enabled: 是否启用此频道
        client_id: 钉钉应用的 Client ID (AppKey)
        client_secret: 钉钉应用的 Client Secret (AppSecret)
        allow_from: 允许访问的用户 ID 列表，空列表拒绝所有，["*"] 允许所有
    """

    enabled: bool = False
    client_id: str = ""
    client_secret: str = ""
    allow_from: list[str] = Field(default_factory=list)


class DingTalkChannel(BaseChannel):
    """
    钉钉频道实现，使用 Stream Mode。

    通过 dingtalk-stream SDK 的 WebSocket 接收事件消息，
    通过直接 HTTP API 发送消息。

    支持单聊和群聊：
        - 单聊：chat_id 为用户 ID
        - 群聊：chat_id 以 "group:" 前缀存储，用于路由回复

    属性：
        name: 频道标识符
        display_name: 频道显示名称
        _IMAGE_EXTS: 支持的图片扩展名集合
        _AUDIO_EXTS: 支持的音频扩展名集合
        _VIDEO_EXTS: 支持的视频扩展名集合
    """

    name = "dingtalk"
    display_name = "DingTalk"
    _IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}
    _AUDIO_EXTS = {".amr", ".mp3", ".wav", ".ogg", ".m4a", ".aac"}
    _VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        """返回默认配置字典。"""
        return DingTalkConfig().model_dump(by_alias=True)

    def __init__(self, config: Any, bus: MessageBus):
        """
        初始化钉钉频道实例。

        参数：
            config: 频道配置
            bus: 消息总线实例
        """
        if isinstance(config, dict):
            config = DingTalkConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: DingTalkConfig = config
        self._client: Any = None
        self._http: httpx.AsyncClient | None = None

        self._access_token: str | None = None
        self._token_expiry: float = 0

        self._background_tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        """
        启动钉钉机器人，使用 Stream Mode。

        初始化 Stream Client 并注册消息处理器，进入消息接收循环。
        支持自动重连机制。

        启动流程：
            1. 检查 SDK 可用性和配置完整性
            2. 创建 HTTP 客户端和 Stream Client
            3. 注册消息回调处理器
            4. 进入消息接收循环，支持断线重连
        """
        try:
            if not DINGTALK_AVAILABLE:
                logger.error(
                    "DingTalk Stream SDK not installed. Run: pip install dingtalk-stream"
                )
                return

            if not self.config.client_id or not self.config.client_secret:
                logger.error("DingTalk client_id and client_secret not configured")
                return

            self._running = True
            self._http = httpx.AsyncClient()

            logger.info(
                "Initializing DingTalk Stream Client with Client ID: {}...",
                self.config.client_id,
            )
            credential = Credential(self.config.client_id, self.config.client_secret)
            self._client = DingTalkStreamClient(credential)

            handler = NanobotDingTalkHandler(self)
            self._client.register_callback_handler(ChatbotMessage.TOPIC, handler)

            logger.info("DingTalk bot started with Stream Mode")

            while self._running:
                try:
                    await self._client.start()
                except Exception as e:
                    logger.warning("DingTalk stream error: {}", e)
                if self._running:
                    logger.info("Reconnecting DingTalk stream in 5 seconds...")
                    await asyncio.sleep(5)

        except Exception as e:
            logger.exception("Failed to start DingTalk channel: {}", e)

    async def stop(self) -> None:
        """
        停止钉钉机器人。

        关闭 HTTP 客户端并取消所有后台任务。
        """
        self._running = False
        if self._http:
            await self._http.aclose()
            self._http = None
        for task in self._background_tasks:
            task.cancel()
        self._background_tasks.clear()

    async def _get_access_token(self) -> str | None:
        """
        获取或刷新 Access Token。

        Access Token 用于调用钉钉 API 发送消息。
        Token 会在过期前 60 秒自动刷新。

        返回：
            有效的 Access Token，失败返回 None
        """
        if self._access_token and time.time() < self._token_expiry:
            return self._access_token

        url = "https://api.dingtalk.com/v1.0/oauth2/accessToken"
        data = {
            "appKey": self.config.client_id,
            "appSecret": self.config.client_secret,
        }

        if not self._http:
            logger.warning("DingTalk HTTP client not initialized, cannot refresh token")
            return None

        try:
            resp = await self._http.post(url, json=data)
            resp.raise_for_status()
            res_data = resp.json()
            self._access_token = res_data.get("accessToken")
            self._token_expiry = time.time() + int(res_data.get("expireIn", 7200)) - 60
            return self._access_token
        except Exception as e:
            logger.error("Failed to get DingTalk access token: {}", e)
            return None

    @staticmethod
    def _is_http_url(value: str) -> bool:
        """检查字符串是否为 HTTP/HTTPS URL。"""
        return urlparse(value).scheme in ("http", "https")

    def _guess_upload_type(self, media_ref: str) -> str:
        """
        根据文件扩展名猜测媒体上传类型。

        参数：
            media_ref: 媒体文件路径或 URL

        返回：
            媒体类型: "image", "voice", "video", 或 "file"
        """
        ext = Path(urlparse(media_ref).path).suffix.lower()
        if ext in self._IMAGE_EXTS: return "image"
        if ext in self._AUDIO_EXTS: return "voice"
        if ext in self._VIDEO_EXTS: return "video"
        return "file"

    def _guess_filename(self, media_ref: str, upload_type: str) -> str:
        """
        从媒体引用中提取或生成文件名。

        参数：
            media_ref: 媒体文件路径或 URL
            upload_type: 媒体类型

        返回：
            文件名字符串
        """
        name = os.path.basename(urlparse(media_ref).path)
        return name or {"image": "image.jpg", "voice": "audio.amr", "video": "video.mp4"}.get(upload_type, "file.bin")

    async def _read_media_bytes(
        self,
        media_ref: str,
    ) -> tuple[bytes | None, str | None, str | None]:
        """
        读取媒体文件内容。

        支持本地文件路径和 HTTP URL。

        参数：
            media_ref: 媒体文件路径或 URL

        返回：
            元组 (文件内容, 文件名, Content-Type)，失败返回 None
        """
        if not media_ref:
            return None, None, None

        if self._is_http_url(media_ref):
            if not self._http:
                return None, None, None
            try:
                resp = await self._http.get(media_ref, follow_redirects=True)
                if resp.status_code >= 400:
                    logger.warning(
                        "DingTalk media download failed status={} ref={}",
                        resp.status_code,
                        media_ref,
                    )
                    return None, None, None
                content_type = (resp.headers.get("content-type") or "").split(";")[0].strip()
                filename = self._guess_filename(media_ref, self._guess_upload_type(media_ref))
                return resp.content, filename, content_type or None
            except Exception as e:
                logger.error("DingTalk media download error ref={} err={}", media_ref, e)
                return None, None, None

        try:
            if media_ref.startswith("file://"):
                parsed = urlparse(media_ref)
                local_path = Path(unquote(parsed.path))
            else:
                local_path = Path(os.path.expanduser(media_ref))
            if not local_path.is_file():
                logger.warning("DingTalk media file not found: {}", local_path)
                return None, None, None
            data = await asyncio.to_thread(local_path.read_bytes)
            content_type = mimetypes.guess_type(local_path.name)[0]
            return data, local_path.name, content_type
        except Exception as e:
            logger.error("DingTalk media read error ref={} err={}", media_ref, e)
            return None, None, None

    async def _upload_media(
        self,
        token: str,
        data: bytes,
        media_type: str,
        filename: str,
        content_type: str | None,
    ) -> str | None:
        """
        上传媒体文件到钉钉服务器。

        参数：
            token: Access Token
            data: 文件内容字节
            media_type: 媒体类型 (image/voice/video/file)
            filename: 文件名
            content_type: MIME 类型

        返回：
            媒体 ID，失败返回 None
        """
        if not self._http:
            return None
        url = f"https://oapi.dingtalk.com/media/upload?access_token={token}&type={media_type}"
        mime = content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        files = {"media": (filename, data, mime)}

        try:
            resp = await self._http.post(url, files=files)
            text = resp.text
            result = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            if resp.status_code >= 400:
                logger.error("DingTalk media upload failed status={} type={} body={}", resp.status_code, media_type, text[:500])
                return None
            errcode = result.get("errcode", 0)
            if errcode != 0:
                logger.error("DingTalk media upload api error type={} errcode={} body={}", media_type, errcode, text[:500])
                return None
            sub = result.get("result") or {}
            media_id = result.get("media_id") or result.get("mediaId") or sub.get("media_id") or sub.get("mediaId")
            if not media_id:
                logger.error("DingTalk media upload missing media_id body={}", text[:500])
                return None
            return str(media_id)
        except Exception as e:
            logger.error("DingTalk media upload error type={} err={}", media_type, e)
            return None

    async def _send_batch_message(
        self,
        token: str,
        chat_id: str,
        msg_key: str,
        msg_param: dict[str, Any],
    ) -> bool:
        """
        发送批量消息（单聊或群聊）。

        参数：
            token: Access Token
            chat_id: 目标聊天 ID（群聊以 "group:" 前缀）
            msg_key: 消息类型键
            msg_param: 消息参数字典

        返回：
            发送成功返回 True
        """
        if not self._http:
            logger.warning("DingTalk HTTP client not initialized, cannot send")
            return False

        headers = {"x-acs-dingtalk-access-token": token}
        if chat_id.startswith("group:"):
            url = "https://api.dingtalk.com/v1.0/robot/groupMessages/send"
            payload = {
                "robotCode": self.config.client_id,
                "openConversationId": chat_id[6:],
                "msgKey": msg_key,
                "msgParam": json.dumps(msg_param, ensure_ascii=False),
            }
        else:
            url = "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"
            payload = {
                "robotCode": self.config.client_id,
                "userIds": [chat_id],
                "msgKey": msg_key,
                "msgParam": json.dumps(msg_param, ensure_ascii=False),
            }

        try:
            resp = await self._http.post(url, json=payload, headers=headers)
            body = resp.text
            if resp.status_code != 200:
                logger.error("DingTalk send failed msgKey={} status={} body={}", msg_key, resp.status_code, body[:500])
                return False
            try: result = resp.json()
            except Exception: result = {}
            errcode = result.get("errcode")
            if errcode not in (None, 0):
                logger.error("DingTalk send api error msgKey={} errcode={} body={}", msg_key, errcode, body[:500])
                return False
            logger.debug("DingTalk message sent to {} with msgKey={}", chat_id, msg_key)
            return True
        except Exception as e:
            logger.error("Error sending DingTalk message msgKey={} err={}", msg_key, e)
            return False

    async def _send_markdown_text(self, token: str, chat_id: str, content: str) -> bool:
        """
        发送 Markdown 格式文本消息。

        参数：
            token: Access Token
            chat_id: 目标聊天 ID
            content: Markdown 内容

        返回：
            发送成功返回 True
        """
        return await self._send_batch_message(
            token,
            chat_id,
            "sampleMarkdown",
            {"text": content, "title": "Nanobot Reply"},
        )

    async def _send_media_ref(self, token: str, chat_id: str, media_ref: str) -> bool:
        """
        发送媒体文件。

        支持直接发送图片 URL 或上传媒体文件后发送。

        参数：
            token: Access Token
            chat_id: 目标聊天 ID
            media_ref: 媒体文件路径或 URL

        返回：
            发送成功返回 True
        """
        media_ref = (media_ref or "").strip()
        if not media_ref:
            return True

        upload_type = self._guess_upload_type(media_ref)
        if upload_type == "image" and self._is_http_url(media_ref):
            ok = await self._send_batch_message(
                token,
                chat_id,
                "sampleImageMsg",
                {"photoURL": media_ref},
            )
            if ok:
                return True
            logger.warning("DingTalk image url send failed, trying upload fallback: {}", media_ref)

        data, filename, content_type = await self._read_media_bytes(media_ref)
        if not data:
            logger.error("DingTalk media read failed: {}", media_ref)
            return False

        filename = filename or self._guess_filename(media_ref, upload_type)
        file_type = Path(filename).suffix.lower().lstrip(".")
        if not file_type:
            guessed = mimetypes.guess_extension(content_type or "")
            file_type = (guessed or ".bin").lstrip(".")
        if file_type == "jpeg":
            file_type = "jpg"

        media_id = await self._upload_media(
            token=token,
            data=data,
            media_type=upload_type,
            filename=filename,
            content_type=content_type,
        )
        if not media_id:
            return False

        if upload_type == "image":
            ok = await self._send_batch_message(
                token,
                chat_id,
                "sampleImageMsg",
                {"photoURL": media_id},
            )
            if ok:
                return True
            logger.warning("DingTalk image media_id send failed, falling back to file: {}", media_ref)

        return await self._send_batch_message(
            token,
            chat_id,
            "sampleFile",
            {"mediaId": media_id, "fileName": filename, "fileType": file_type},
        )

    async def send(self, msg: OutboundMessage) -> None:
        """
        发送消息通过钉钉。

        参数：
            msg: 出站消息对象

        处理流程：
            1. 获取 Access Token
            2. 发送文本内容（Markdown 格式）
            3. 发送媒体附件
        """
        token = await self._get_access_token()
        if not token:
            return

        if msg.content and msg.content.strip():
            await self._send_markdown_text(token, msg.chat_id, msg.content.strip())

        for media_ref in msg.media or []:
            ok = await self._send_media_ref(token, msg.chat_id, media_ref)
            if ok:
                continue
            logger.error("DingTalk media send failed for {}", media_ref)
            filename = self._guess_filename(media_ref, self._guess_upload_type(media_ref))
            await self._send_markdown_text(
                token,
                msg.chat_id,
                f"[Attachment send failed: {filename}]",
            )

    async def _on_message(
        self,
        content: str,
        sender_id: str,
        sender_name: str,
        conversation_type: str | None = None,
        conversation_id: str | None = None,
    ) -> None:
        """
        处理传入消息（由 NanobotDingTalkHandler 调用）。

        委托给 BaseChannel._handle_message() 进行权限检查后发布到消息总线。

        参数：
            content: 消息内容
            sender_id: 发送者 ID
            sender_name: 发送者昵称
            conversation_type: 会话类型（"2" 表示群聊）
            conversation_id: 会话 ID
        """
        try:
            logger.info("DingTalk inbound: {} from {}", content, sender_name)
            is_group = conversation_type == "2" and conversation_id
            chat_id = f"group:{conversation_id}" if is_group else sender_id
            await self._handle_message(
                sender_id=sender_id,
                chat_id=chat_id,
                content=str(content),
                metadata={
                    "sender_name": sender_name,
                    "platform": "dingtalk",
                    "conversation_type": conversation_type,
                },
            )
        except Exception as e:
            logger.error("Error publishing DingTalk message: {}", e)

    async def _download_dingtalk_file(
        self,
        download_code: str,
        filename: str,
        sender_id: str,
    ) -> str | None:
        """
        下载钉钉文件到媒体目录。

        参数：
            download_code: 钉钉文件下载码
            filename: 保存的文件名
            sender_id: 发送者 ID（用于组织目录结构）

        返回：
            本地文件路径，失败返回 None

        处理流程：
            1. 使用 download_code 换取临时下载 URL
            2. 下载文件内容
            3. 保存到媒体目录
        """
        from nanobot.config.paths import get_media_dir

        try:
            token = await self._get_access_token()
            if not token or not self._http:
                logger.error("DingTalk file download: no token or http client")
                return None

            api_url = "https://api.dingtalk.com/v1.0/robot/messageFiles/download"
            headers = {"x-acs-dingtalk-access-token": token, "Content-Type": "application/json"}
            payload = {"downloadCode": download_code, "robotCode": self.config.client_id}
            resp = await self._http.post(api_url, json=payload, headers=headers)
            if resp.status_code != 200:
                logger.error("DingTalk get download URL failed: status={}, body={}", resp.status_code, resp.text)
                return None

            result = resp.json()
            download_url = result.get("downloadUrl")
            if not download_url:
                logger.error("DingTalk download URL not found in response: {}", result)
                return None

            file_resp = await self._http.get(download_url, follow_redirects=True)
            if file_resp.status_code != 200:
                logger.error("DingTalk file download failed: status={}", file_resp.status_code)
                return None

            download_dir = get_media_dir("dingtalk") / sender_id
            download_dir.mkdir(parents=True, exist_ok=True)
            file_path = download_dir / filename
            await asyncio.to_thread(file_path.write_bytes, file_resp.content)
            logger.info("DingTalk file saved: {}", file_path)
            return str(file_path)
        except Exception as e:
            logger.error("DingTalk file download error: {}", e)
            return None
