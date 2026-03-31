"""
邮件频道实现，使用 IMAP 轮询接收邮件和 SMTP 发送回复。

该模块实现了 nanobot 与电子邮件系统的集成，支持：
- 通过 IMAP 协议轮询邮箱获取新邮件
- 通过 SMTP 协议发送回复邮件
- 支持 DKIM/SPF 验证防止邮件伪造
- 支持历史邮件查询（按日期范围）

主要功能：
    - 定期轮询 IMAP 邮箱获取未读邮件
    - 解析邮件内容（支持纯文本和 HTML）
    - 发送回复邮件（自动添加 Re: 前缀）
    - 支持 In-Reply-To 和 References 邮件头

安全特性：
    - DKIM 验证：检查邮件签名有效性
    - SPF 验证：检查发件人服务器授权

配置说明：
    - IMAP 配置：主机、端口、用户名、密码、邮箱名
    - SMTP 配置：主机、端口、用户名、密码、发件地址
    - 安全配置：verify_dkim、verify_spf
"""

import asyncio
import html
import imaplib
import re
import smtplib
import ssl
from datetime import date
from email import policy
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parseaddr
from typing import Any

from loguru import logger
from pydantic import Field

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import Base


class EmailConfig(Base):
    """
    邮件频道配置模型。

    包含 IMAP（接收）和 SMTP（发送）两部分配置。

    属性：
        enabled: 是否启用此频道
        consent_granted: 用户是否已授权（必须为 True 才能使用）

        IMAP 配置：
        imap_host: IMAP 服务器地址
        imap_port: IMAP 服务器端口（默认 993）
        imap_username: IMAP 用户名
        imap_password: IMAP 密码
        imap_mailbox: 邮箱名称（默认 INBOX）
        imap_use_ssl: 是否使用 SSL 连接

        SMTP 配置：
        smtp_host: SMTP 服务器地址
        smtp_port: SMTP 服务器端口（默认 587）
        smtp_username: SMTP 用户名
        smtp_password: SMTP 密码
        smtp_use_tls: 是否使用 STARTTLS
        smtp_use_ssl: 是否使用 SSL/TLS
        from_address: 发件人地址

        行为配置：
        auto_reply_enabled: 是否启用自动回复
        poll_interval_seconds: 轮询间隔（秒）
        mark_seen: 是否标记已读
        max_body_chars: 邮件正文最大字符数
        subject_prefix: 回复主题前缀

        安全配置：
        verify_dkim: 是否验证 DKIM 签名
        verify_spf: 是否验证 SPF 记录
    """

    enabled: bool = False
    consent_granted: bool = False

    imap_host: str = ""
    imap_port: int = 993
    imap_username: str = ""
    imap_password: str = ""
    imap_mailbox: str = "INBOX"
    imap_use_ssl: bool = True

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    smtp_use_ssl: bool = False
    from_address: str = ""

    auto_reply_enabled: bool = True
    poll_interval_seconds: int = 30
    mark_seen: bool = True
    max_body_chars: int = 12000
    subject_prefix: str = "Re: "
    allow_from: list[str] = Field(default_factory=list)

    verify_dkim: bool = True
    verify_spf: bool = True


class EmailChannel(BaseChannel):
    """
    邮件频道实现。

    入站流程：
        - 定期轮询 IMAP 邮箱获取未读邮件
        - 将每封邮件转换为入站事件

    出站流程：
        - 通过 SMTP 发送回复邮件到发件人地址

    安全特性：
        - DKIM 验证：防止邮件内容被篡改
        - SPF 验证：防止发件人地址伪造

    属性：
        name: 频道标识符
        display_name: 频道显示名称
    """

    name = "email"
    display_name = "Email"
    _IMAP_MONTHS = (
        "Jan",
        "Feb",
        "Mar",
        "Apr",
        "May",
        "Jun",
        "Jul",
        "Aug",
        "Sep",
        "Oct",
        "Nov",
        "Dec",
    )
    _IMAP_RECONNECT_MARKERS = (
        "disconnected for inactivity",
        "eof occurred in violation of protocol",
        "socket error",
        "connection reset",
        "broken pipe",
        "bye",
    )
    _IMAP_MISSING_MAILBOX_MARKERS = (
        "mailbox doesn't exist",
        "select failed",
        "no such mailbox",
        "can't open mailbox",
        "does not exist",
    )

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        """返回默认配置字典。"""
        return EmailConfig().model_dump(by_alias=True)

    def __init__(self, config: Any, bus: MessageBus):
        """
        初始化邮件频道实例。

        参数：
            config: 频道配置
            bus: 消息总线实例
        """
        if isinstance(config, dict):
            config = EmailConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: EmailConfig = config
        self._last_subject_by_chat: dict[str, str] = {}
        self._last_message_id_by_chat: dict[str, str] = {}
        self._processed_uids: set[str] = set()
        self._MAX_PROCESSED_UIDS = 100000

    async def start(self) -> None:
        """
        启动 IMAP 轮询获取入站邮件。

        检查用户授权后开始定期轮询 IMAP 邮箱。
        """
        if not self.config.consent_granted:
            logger.warning(
                "Email channel disabled: consent_granted is false. "
                "Set channels.email.consentGranted=true after explicit user permission."
            )
            return

        if not self._validate_config():
            return

        self._running = True
        if not self.config.verify_dkim and not self.config.verify_spf:
            logger.warning(
                "Email channel: DKIM and SPF verification are both DISABLED. "
                "Emails with spoofed From headers will be accepted. "
                "Set verify_dkim=true and verify_spf=true for anti-spoofing protection."
            )
        logger.info("Starting Email channel (IMAP polling mode)...")

        poll_seconds = max(5, int(self.config.poll_interval_seconds))
        while self._running:
            try:
                inbound_items = await asyncio.to_thread(self._fetch_new_messages)
                for item in inbound_items:
                    sender = item["sender"]
                    subject = item.get("subject", "")
                    message_id = item.get("message_id", "")

                    if subject:
                        self._last_subject_by_chat[sender] = subject
                    if message_id:
                        self._last_message_id_by_chat[sender] = message_id

                    await self._handle_message(
                        sender_id=sender,
                        chat_id=sender,
                        content=item["content"],
                        metadata=item.get("metadata", {}),
                    )
            except Exception as e:
                logger.error("Email polling error: {}", e)

            await asyncio.sleep(poll_seconds)

    async def stop(self) -> None:
        """停止轮询循环。"""
        self._running = False

    async def send(self, msg: OutboundMessage) -> None:
        """
        通过 SMTP 发送邮件。

        参数：
            msg: 出站消息对象

        处理流程：
            1. 检查授权和配置
            2. 确定是否为回复邮件
            3. 构建邮件（包含 In-Reply-To 和 References）
            4. 发送邮件
        """
        if not self.config.consent_granted:
            logger.warning("Skip email send: consent_granted is false")
            return

        if not self.config.smtp_host:
            logger.warning("Email channel SMTP host not configured")
            return

        to_addr = msg.chat_id.strip()
        if not to_addr:
            logger.warning("Email channel missing recipient address")
            return

        is_reply = to_addr in self._last_subject_by_chat
        force_send = bool((msg.metadata or {}).get("force_send"))

        if is_reply and not self.config.auto_reply_enabled and not force_send:
            logger.info("Skip automatic email reply to {}: auto_reply_enabled is false", to_addr)
            return

        base_subject = self._last_subject_by_chat.get(to_addr, "nanobot reply")
        subject = self._reply_subject(base_subject)
        if msg.metadata and isinstance(msg.metadata.get("subject"), str):
            override = msg.metadata["subject"].strip()
            if override:
                subject = override

        email_msg = EmailMessage()
        email_msg["From"] = self.config.from_address or self.config.smtp_username or self.config.imap_username
        email_msg["To"] = to_addr
        email_msg["Subject"] = subject
        email_msg.set_content(msg.content or "")

        in_reply_to = self._last_message_id_by_chat.get(to_addr)
        if in_reply_to:
            email_msg["In-Reply-To"] = in_reply_to
            email_msg["References"] = in_reply_to

        try:
            await asyncio.to_thread(self._smtp_send, email_msg)
        except Exception as e:
            logger.error("Error sending email to {}: {}", to_addr, e)
            raise

    def _validate_config(self) -> bool:
        """
        验证配置完整性。

        返回：
            配置有效返回 True
        """
        missing = []
        if not self.config.imap_host:
            missing.append("imap_host")
        if not self.config.imap_username:
            missing.append("imap_username")
        if not self.config.imap_password:
            missing.append("imap_password")
        if not self.config.smtp_host:
            missing.append("smtp_host")
        if not self.config.smtp_username:
            missing.append("smtp_username")
        if not self.config.smtp_password:
            missing.append("smtp_password")

        if missing:
            logger.error("Email channel not configured, missing: {}", ', '.join(missing))
            return False
        return True

    def _smtp_send(self, msg: EmailMessage) -> None:
        """
        通过 SMTP 发送邮件。

        参数：
            msg: 要发送的邮件消息对象
        """
        timeout = 30
        if self.config.smtp_use_ssl:
            with smtplib.SMTP_SSL(
                self.config.smtp_host,
                self.config.smtp_port,
                timeout=timeout,
            ) as smtp:
                smtp.login(self.config.smtp_username, self.config.smtp_password)
                smtp.send_message(msg)
            return

        with smtplib.SMTP(self.config.smtp_host, self.config.smtp_port, timeout=timeout) as smtp:
            if self.config.smtp_use_tls:
                smtp.starttls(context=ssl.create_default_context())
            smtp.login(self.config.smtp_username, self.config.smtp_password)
            smtp.send_message(msg)

    def _fetch_new_messages(self) -> list[dict[str, Any]]:
        """
        轮询 IMAP 获取未读邮件。

        返回：
            解析后的消息列表
        """
        return self._fetch_messages(
            search_criteria=("UNSEEN",),
            mark_seen=self.config.mark_seen,
            dedupe=True,
            limit=0,
        )

    def fetch_messages_between_dates(
        self,
        start_date: date,
        end_date: date,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """
        按日期范围获取邮件。

        用于历史邮件摘要任务（如"昨天的邮件"）。

        参数：
            start_date: 开始日期（包含）
            end_date: 结束日期（不包含）
            limit: 最大返回数量

        返回：
            解析后的消息列表
        """
        if end_date <= start_date:
            return []

        return self._fetch_messages(
            search_criteria=(
                "SINCE",
                self._format_imap_date(start_date),
                "BEFORE",
                self._format_imap_date(end_date),
            ),
            mark_seen=False,
            dedupe=False,
            limit=max(1, int(limit)),
        )

    def _fetch_messages(
        self,
        search_criteria: tuple[str, ...],
        mark_seen: bool,
        dedupe: bool,
        limit: int,
    ) -> list[dict[str, Any]]:
        """
        按条件获取邮件。

        参数：
            search_criteria: IMAP 搜索条件
            mark_seen: 是否标记已读
            dedupe: 是否去重
            limit: 最大返回数量（0 表示无限制）

        返回：
            解析后的消息列表
        """
        messages: list[dict[str, Any]] = []
        cycle_uids: set[str] = set()

        for attempt in range(2):
            try:
                self._fetch_messages_once(
                    search_criteria,
                    mark_seen,
                    dedupe,
                    limit,
                    messages,
                    cycle_uids,
                )
                return messages
            except Exception as exc:
                if attempt == 1 or not self._is_stale_imap_error(exc):
                    raise
                logger.warning("Email IMAP connection went stale, retrying once: {}", exc)

        return messages

    def _fetch_messages_once(
        self,
        search_criteria: tuple[str, ...],
        mark_seen: bool,
        dedupe: bool,
        limit: int,
        messages: list[dict[str, Any]],
        cycle_uids: set[str],
    ) -> None:
        """
        执行一次 IMAP 邮件获取。

        参数：
            search_criteria: IMAP 搜索条件
            mark_seen: 是否标记已读
            dedupe: 是否去重
            limit: 最大返回数量
            messages: 输出消息列表
            cycle_uids: 当前周期已处理的 UID 集合
        """
        mailbox = self.config.imap_mailbox or "INBOX"

        if self.config.imap_use_ssl:
            client = imaplib.IMAP4_SSL(self.config.imap_host, self.config.imap_port)
        else:
            client = imaplib.IMAP4(self.config.imap_host, self.config.imap_port)

        try:
            client.login(self.config.imap_username, self.config.imap_password)
            try:
                status, _ = client.select(mailbox)
            except Exception as exc:
                if self._is_missing_mailbox_error(exc):
                    logger.warning("Email mailbox unavailable, skipping poll for {}: {}", mailbox, exc)
                    return messages
                raise
            if status != "OK":
                logger.warning("Email mailbox select returned {}, skipping poll for {}", status, mailbox)
                return messages

            status, data = client.search(None, *search_criteria)
            if status != "OK" or not data:
                return messages

            ids = data[0].split()
            if limit > 0 and len(ids) > limit:
                ids = ids[-limit:]
            for imap_id in ids:
                status, fetched = client.fetch(imap_id, "(BODY.PEEK[] UID)")
                if status != "OK" or not fetched:
                    continue

                raw_bytes = self._extract_message_bytes(fetched)
                if raw_bytes is None:
                    continue

                uid = self._extract_uid(fetched)
                if uid and uid in cycle_uids:
                    continue
                if dedupe and uid and uid in self._processed_uids:
                    continue

                parsed = BytesParser(policy=policy.default).parsebytes(raw_bytes)
                sender = parseaddr(parsed.get("From", ""))[1].strip().lower()
                if not sender:
                    continue

                spf_pass, dkim_pass = self._check_authentication_results(parsed)
                if self.config.verify_spf and not spf_pass:
                    logger.warning(
                        "Email from {} rejected: SPF verification failed "
                        "(no 'spf=pass' in Authentication-Results header)",
                        sender,
                    )
                    continue
                if self.config.verify_dkim and not dkim_pass:
                    logger.warning(
                        "Email from {} rejected: DKIM verification failed "
                        "(no 'dkim=pass' in Authentication-Results header)",
                        sender,
                    )
                    continue

                subject = self._decode_header_value(parsed.get("Subject", ""))
                date_value = parsed.get("Date", "")
                message_id = parsed.get("Message-ID", "").strip()
                body = self._extract_text_body(parsed)

                if not body:
                    body = "(empty email body)"

                body = body[: self.config.max_body_chars]
                content = (
                    f"[EMAIL-CONTEXT] Email received.\n"
                    f"From: {sender}\n"
                    f"Subject: {subject}\n"
                    f"Date: {date_value}\n\n"
                    f"{body}"
                )

                metadata = {
                    "message_id": message_id,
                    "subject": subject,
                    "date": date_value,
                    "sender_email": sender,
                    "uid": uid,
                }
                messages.append(
                    {
                        "sender": sender,
                        "subject": subject,
                        "message_id": message_id,
                        "content": content,
                        "metadata": metadata,
                    }
                )

                if uid:
                    cycle_uids.add(uid)
                if dedupe and uid:
                    self._processed_uids.add(uid)
                    if len(self._processed_uids) > self._MAX_PROCESSED_UIDS:
                        self._processed_uids = set(list(self._processed_uids)[len(self._processed_uids) // 2:])

                if mark_seen:
                    client.store(imap_id, "+FLAGS", "\\Seen")
        finally:
            try:
                client.logout()
            except Exception:
                pass

    @classmethod
    def _is_stale_imap_error(cls, exc: Exception) -> bool:
        """检查是否为 IMAP 连接过期错误。"""
        message = str(exc).lower()
        return any(marker in message for marker in cls._IMAP_RECONNECT_MARKERS)

    @classmethod
    def _is_missing_mailbox_error(cls, exc: Exception) -> bool:
        """检查是否为邮箱不存在错误。"""
        message = str(exc).lower()
        return any(marker in message for marker in cls._IMAP_MISSING_MAILBOX_MARKERS)

    @classmethod
    def _format_imap_date(cls, value: date) -> str:
        """
        格式化日期为 IMAP 搜索格式。

        参数：
            value: 日期值

        返回：
            IMAP 格式的日期字符串（如 "01-Jan-2024"）
        """
        month = cls._IMAP_MONTHS[value.month - 1]
        return f"{value.day:02d}-{month}-{value.year}"

    @staticmethod
    def _extract_message_bytes(fetched: list[Any]) -> bytes | None:
        """
        从 IMAP 获取结果中提取邮件原始字节。

        参数：
            fetched: IMAP fetch 返回的数据

        返回：
            邮件原始字节，失败返回 None
        """
        for item in fetched:
            if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], (bytes, bytearray)):
                return bytes(item[1])
        return None

    @staticmethod
    def _extract_uid(fetched: list[Any]) -> str:
        """
        从 IMAP 获取结果中提取邮件 UID。

        参数：
            fetched: IMAP fetch 返回的数据

        返回：
            邮件 UID 字符串
        """
        for item in fetched:
            if isinstance(item, tuple) and item and isinstance(item[0], (bytes, bytearray)):
                head = bytes(item[0]).decode("utf-8", errors="ignore")
                m = re.search(r"UID\s+(\d+)", head)
                if m:
                    return m.group(1)
        return ""

    @staticmethod
    def _decode_header_value(value: str) -> str:
        """
        解码邮件头值（处理编码的标题等）。

        参数：
            value: 原始头值

        返回：
            解码后的字符串
        """
        if not value:
            return ""
        try:
            return str(make_header(decode_header(value)))
        except Exception:
            return value

    @classmethod
    def _extract_text_body(cls, msg: Any) -> str:
        """
        从邮件中提取可读正文文本。

        优先提取纯文本部分，回退到 HTML 转文本。

        参数：
            msg: 邮件消息对象

        返回：
            正文文本
        """
        if msg.is_multipart():
            plain_parts: list[str] = []
            html_parts: list[str] = []
            for part in msg.walk():
                if part.get_content_disposition() == "attachment":
                    continue
                content_type = part.get_content_type()
                try:
                    payload = part.get_content()
                except Exception:
                    payload_bytes = part.get_payload(decode=True) or b""
                    charset = part.get_content_charset() or "utf-8"
                    payload = payload_bytes.decode(charset, errors="replace")
                if not isinstance(payload, str):
                    continue
                if content_type == "text/plain":
                    plain_parts.append(payload)
                elif content_type == "text/html":
                    html_parts.append(payload)
            if plain_parts:
                return "\n\n".join(plain_parts).strip()
            if html_parts:
                return cls._html_to_text("\n\n".join(html_parts)).strip()
            return ""

        try:
            payload = msg.get_content()
        except Exception:
            payload_bytes = msg.get_payload(decode=True) or b""
            charset = msg.get_content_charset() or "utf-8"
            payload = payload_bytes.decode(charset, errors="replace")
        if not isinstance(payload, str):
            return ""
        if msg.get_content_type() == "text/html":
            return cls._html_to_text(payload).strip()
        return payload.strip()

    @staticmethod
    def _check_authentication_results(parsed_msg: Any) -> tuple[bool, bool]:
        """
        解析 Authentication-Results 头获取 SPF 和 DKIM 验证结果。

        参数：
            parsed_msg: 解析后的邮件消息对象

        返回：
            元组 (spf_pass, dkim_pass)
        """
        spf_pass = False
        dkim_pass = False
        for ar_header in parsed_msg.get_all("Authentication-Results") or []:
            ar_lower = ar_header.lower()
            if re.search(r"\bspf\s*=\s*pass\b", ar_lower):
                spf_pass = True
            if re.search(r"\bdkim\s*=\s*pass\b", ar_lower):
                dkim_pass = True
        return spf_pass, dkim_pass

    @staticmethod
    def _html_to_text(raw_html: str) -> str:
        """
        将 HTML 转换为纯文本。

        参数：
            raw_html: HTML 内容

        返回：
            纯文本内容
        """
        text = re.sub(r"<\s*br\s*/?>", "\n", raw_html, flags=re.IGNORECASE)
        text = re.sub(r"<\s*/\s*p\s*>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", "", text)
        return html.unescape(text)

    def _reply_subject(self, base_subject: str) -> str:
        """
        构建回复邮件主题。

        参数：
            base_subject: 原始主题

        返回：
            回复主题（添加 Re: 前缀）
        """
        subject = (base_subject or "").strip() or "nanobot reply"
        prefix = self.config.subject_prefix or "Re: "
        if subject.lower().startswith("re:"):
            return subject
        return f"{prefix}{subject}"
