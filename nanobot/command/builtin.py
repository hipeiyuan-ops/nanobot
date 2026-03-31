"""
内置斜杠命令处理器模块。

该模块实现了 nanobot 的内置命令处理逻辑：
    - /stop: 取消当前会话的所有活动任务和子代理
    - /restart: 原地重启进程
    - /status: 显示机器人状态信息
    - /new: 开始新的对话会话
    - /help: 显示可用命令列表

命令处理流程：
    1. 命令路由器匹配命令字符串
    2. 创建 CommandContext 上下文对象
    3. 调用对应的命令处理函数
    4. 返回 OutboundMessage 响应
"""

from __future__ import annotations

import asyncio
import os
import sys

from nanobot import __version__
from nanobot.bus.events import OutboundMessage
from nanobot.command.router import CommandContext, CommandRouter
from nanobot.utils.helpers import build_status_content


async def cmd_stop(ctx: CommandContext) -> OutboundMessage:
    """
    取消会话的所有活动任务和子代理。

    参数：
        ctx: 命令上下文对象

    返回：
        包含取消任务数量的出站消息

    处理流程：
        1. 从活动任务列表获取会话任务
        2. 取消所有未完成的任务
        3. 取消会话关联的子代理
        4. 返回取消统计
    """
    loop = ctx.loop
    msg = ctx.msg
    tasks = loop._active_tasks.pop(msg.session_key, [])
    cancelled = sum(1 for t in tasks if not t.done() and t.cancel())
    for t in tasks:
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass
    sub_cancelled = await loop.subagents.cancel_by_session(msg.session_key)
    total = cancelled + sub_cancelled
    content = f"Stopped {total} task(s)." if total else "No active task to stop."
    return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content=content)


async def cmd_restart(ctx: CommandContext) -> OutboundMessage:
    """
    通过 os.execv 原地重启进程。

    参数：
        ctx: 命令上下文对象

    返回：
        重启提示消息

    注意：
        使用 os.execv 实现原地重启，保持相同的命令行参数。
        延迟 1 秒后执行，以便先返回响应消息。
    """
    msg = ctx.msg

    async def _do_restart():
        await asyncio.sleep(1)
        os.execv(sys.executable, [sys.executable, "-m", "nanobot"] + sys.argv[1:])

    asyncio.create_task(_do_restart())
    return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content="Restarting...")


async def cmd_status(ctx: CommandContext) -> OutboundMessage:
    """
    构建会话的状态消息。

    参数：
        ctx: 命令上下文对象

    返回：
        包含状态信息的出站消息

    状态信息包括：
        - 版本号
        - 当前模型
        - 运行时间
        - Token 使用量
        - 上下文窗口大小
        - 会话消息数量
        - 上下文 Token 估算
    """
    loop = ctx.loop
    session = ctx.session or loop.sessions.get_or_create(ctx.key)
    ctx_est = 0
    try:
        ctx_est, _ = loop.memory_consolidator.estimate_session_prompt_tokens(session)
    except Exception:
        pass
    if ctx_est <= 0:
        ctx_est = loop._last_usage.get("prompt_tokens", 0)
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=build_status_content(
            version=__version__, model=loop.model,
            start_time=loop._start_time, last_usage=loop._last_usage,
            context_window_tokens=loop.context_window_tokens,
            session_msg_count=len(session.get_history(max_messages=0)),
            context_tokens_estimate=ctx_est,
        ),
        metadata={"render_as": "text"},
    )


async def cmd_new(ctx: CommandContext) -> OutboundMessage:
    """
    开始新的对话会话。

    参数：
        ctx: 命令上下文对象

    返回：
        新会话开始提示消息

    处理流程：
        1. 获取当前会话
        2. 提取未合并的消息快照
        3. 清空会话
        4. 保存并使缓存失效
        5. 后台归档旧消息
    """
    loop = ctx.loop
    session = ctx.session or loop.sessions.get_or_create(ctx.key)
    snapshot = session.messages[session.last_consolidated:]
    session.clear()
    loop.sessions.save(session)
    loop.sessions.invalidate(session.key)
    if snapshot:
        loop._schedule_background(loop.memory_consolidator.archive_messages(snapshot))
    return OutboundMessage(
        channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
        content="New session started.",
    )


async def cmd_help(ctx: CommandContext) -> OutboundMessage:
    """
    返回可用的斜杠命令列表。

    参数：
        ctx: 命令上下文对象

    返回：
        包含命令列表的出站消息
    """
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=build_help_text(),
        metadata={"render_as": "text"},
    )


def build_help_text() -> str:
    """
    构建跨频道共享的标准帮助文本。

    返回：
        格式化的帮助文本字符串
    """
    lines = [
        "🐈 nanobot commands:",
        "/new — Start a new conversation",
        "/stop — Stop the current task",
        "/restart — Restart the bot",
        "/status — Show bot status",
        "/help — Show available commands",
    ]
    return "\n".join(lines)


def register_builtin_commands(router: CommandRouter) -> None:
    """
    注册默认的斜杠命令集。

    参数：
        router: 命令路由器实例

    注册的命令：
        - 优先级命令（在调度锁之前处理）：
            - /stop, /restart, /status
        - 精确匹配命令：
            - /new, /status, /help
    """
    router.priority("/stop", cmd_stop)
    router.priority("/restart", cmd_restart)
    router.priority("/status", cmd_status)
    router.exact("/new", cmd_new)
    router.exact("/status", cmd_status)
    router.exact("/help", cmd_help)
