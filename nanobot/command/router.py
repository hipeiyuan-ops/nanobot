"""
斜杠命令路由表模块。

该模块提供了轻量级的命令路由机制，支持多种匹配策略：
    - 优先级命令：在调度锁之前处理的精确匹配命令
    - 精确匹配：完整匹配命令字符串
    - 前缀匹配：最长前缀优先匹配
    - 拦截器：回退谓词处理

路由优先级：
    1. priority（优先级）— 精确匹配，在调度锁之前处理（如 /stop, /restart）
    2. exact（精确）— 精确匹配，在调度锁内处理
    3. prefix（前缀）— 最长前缀优先匹配（如 "/team "）
    4. interceptors（拦截器）— 回退谓词处理（如团队模式激活检查）
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:
    from nanobot.bus.events import InboundMessage, OutboundMessage
    from nanobot.session.manager import Session

Handler = Callable[["CommandContext"], Awaitable["OutboundMessage | None"]]


@dataclass
class CommandContext:
    """
    命令处理器生成响应所需的所有上下文信息。

    属性：
        msg: 入站消息对象
        session: 当前会话对象（可能为 None）
        key: 会话键（通常是 channel:chat_id）
        raw: 原始命令文本
        args: 命令参数（前缀匹配时自动填充）
        loop: AgentLoop 实例引用
    """

    msg: InboundMessage
    session: Session | None
    key: str
    raw: str
    args: str = ""
    loop: Any = None


class CommandRouter:
    """
    纯字典驱动的命令调度器。

    支持三层匹配策略，按顺序检查：
        1. priority — 精确匹配命令，在调度锁之前处理
           （如 /stop, /restart）
        2. exact — 精确匹配命令，在调度锁内处理
        3. prefix — 最长前缀优先匹配（如 "/team "）
        4. interceptors — 回退谓词（如团队模式激活检查）

    属性：
        _priority: 优先级命令字典
        _exact: 精确匹配命令字典
        _prefix: 前缀匹配列表（按长度降序排列）
        _interceptors: 拦截器列表
    """

    def __init__(self) -> None:
        """初始化命令路由器。"""
        self._priority: dict[str, Handler] = {}
        self._exact: dict[str, Handler] = {}
        self._prefix: list[tuple[str, Handler]] = []
        self._interceptors: list[Handler] = []

    def priority(self, cmd: str, handler: Handler) -> None:
        """
        注册优先级命令。

        优先级命令在调度锁之前处理，用于需要立即响应的命令。

        参数：
            cmd: 命令字符串（如 "/stop"）
            handler: 异步处理函数
        """
        self._priority[cmd] = handler

    def exact(self, cmd: str, handler: Handler) -> None:
        """
        注册精确匹配命令。

        参数：
            cmd: 命令字符串（如 "/new"）
            handler: 异步处理函数
        """
        self._exact[cmd] = handler

    def prefix(self, pfx: str, handler: Handler) -> None:
        """
        注册前缀匹配命令。

        前缀按长度降序排列，确保最长前缀优先匹配。

        参数：
            pfx: 命令前缀（如 "/team "）
            handler: 异步处理函数
        """
        self._prefix.append((pfx, handler))
        self._prefix.sort(key=lambda p: len(p[0]), reverse=True)

    def intercept(self, handler: Handler) -> None:
        """
        注册拦截器。

        拦截器在所有命令都不匹配时调用，用于实现回退逻辑。

        参数：
            handler: 异步处理函数，返回 None 表示不处理
        """
        self._interceptors.append(handler)

    def is_priority(self, text: str) -> bool:
        """
        检查文本是否为优先级命令。

        参数：
            text: 待检查的文本

        返回：
            是优先级命令返回 True
        """
        return text.strip().lower() in self._priority

    async def dispatch_priority(self, ctx: CommandContext) -> OutboundMessage | None:
        """
        调度优先级命令。

        从 run() 方法调用，不持有锁。

        参数：
            ctx: 命令上下文对象

        返回：
            命令处理结果，未匹配返回 None
        """
        handler = self._priority.get(ctx.raw.lower())
        if handler:
            return await handler(ctx)
        return None

    async def dispatch(self, ctx: CommandContext) -> OutboundMessage | None:
        """
        调度命令：依次尝试精确匹配、前缀匹配、拦截器。

        参数：
            ctx: 命令上下文对象

        返回：
            命令处理结果，未处理返回 None

        处理流程：
            1. 尝试精确匹配
            2. 尝试前缀匹配（最长优先）
            3. 尝试拦截器
        """
        cmd = ctx.raw.lower()

        if handler := self._exact.get(cmd):
            return await handler(ctx)

        for pfx, handler in self._prefix:
            if cmd.startswith(pfx):
                ctx.args = ctx.raw[len(pfx):]
                return await handler(ctx)

        for interceptor in self._interceptors:
            result = await interceptor(ctx)
            if result is not None:
                return result

        return None
