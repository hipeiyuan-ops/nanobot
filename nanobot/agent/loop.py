"""
代理循环：核心处理引擎

AgentLoop 是 nanobot 的核心处理引擎，负责：
1. 从消息总线接收消息
2. 构建包含历史、记忆和技能的上下文
3. 调用 LLM 生成响应
4. 执行工具调用
5. 将响应发送回通道
"""

from __future__ import annotations

import asyncio
import json
import re
import os
import time
from contextlib import AsyncExitStack, nullcontext
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

from nanobot.agent.context import ContextBuilder
from nanobot.agent.hook import AgentHook, AgentHookContext, CompositeHook
from nanobot.agent.memory import MemoryConsolidator
from nanobot.agent.runner import AgentRunSpec, AgentRunner
from nanobot.agent.subagent import SubagentManager
from nanobot.agent.tools.cron import CronTool
from nanobot.agent.skills import BUILTIN_SKILLS_DIR
from nanobot.agent.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from nanobot.agent.tools.message import MessageTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.shell import ExecTool
from nanobot.agent.tools.spawn import SpawnTool
from nanobot.agent.tools.web import WebFetchTool, WebSearchTool
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.command import CommandContext, CommandRouter, register_builtin_commands
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMProvider
from nanobot.session.manager import Session, SessionManager

if TYPE_CHECKING:
    # 这些导入只在类型检查时执行
    from nanobot.config.schema import ChannelsConfig, ExecToolConfig, WebSearchConfig
    from nanobot.cron.service import CronService


class _LoopHook(AgentHook):
    """
    主代理循环的核心生命周期钩子
    
    处理流式增量中继、进度报告、工具调用日志记录，
    以及为内置代理路径剥离思考标签（think tags）。
    
    钩子系统允许在代理执行的不同阶段插入自定义逻辑，
    例如在工具执行前、流式输出时等。
    """

    def __init__(
        self,
        agent_loop: AgentLoop,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        *,
        channel: str = "cli",
        chat_id: str = "direct",
        message_id: str | None = None,
    ) -> None:
        """
        初始化循环钩子
        
        Args:
            agent_loop: AgentLoop 实例，用于访问代理的核心功能
            on_progress: 进度回调函数，用于报告处理进度
            on_stream: 流式输出回调函数，用于实时输出内容增量
            on_stream_end: 流式输出结束回调函数
            channel: 通道名称（如 "cli", "telegram" 等）
            chat_id: 聊天 ID，用于标识具体的对话
            message_id: 消息 ID，用于消息追踪
        """
        self._loop = agent_loop
        self._on_progress = on_progress
        self._on_stream = on_stream
        self._on_stream_end = on_stream_end
        self._channel = channel
        self._chat_id = chat_id
        self._message_id = message_id
        self._stream_buf = ""  # 流式输出缓冲区，用于累积增量内容

    def wants_streaming(self) -> bool:
        """检查是否需要流式输出"""
        return self._on_stream is not None

    async def on_stream(self, context: AgentHookContext, delta: str) -> None:
        """
        处理流式输出的增量内容
        
        这个方法会在 LLM 生成每个增量时被调用。
        它会剥离思考标签，只输出实际内容。
        
        Args:
            context: 钩子上下文，包含当前执行状态
            delta: 增量内容
        """
        from nanobot.utils.helpers import strip_think

        # 获取之前缓冲区中清理后的内容
        prev_clean = strip_think(self._stream_buf)
        # 将新增量添加到缓冲区
        self._stream_buf += delta
        # 获取添加新内容后清理的内容
        new_clean = strip_think(self._stream_buf)
        # 计算增量部分（新增加的清理后内容）
        incremental = new_clean[len(prev_clean):]
        # 如果有增量内容且设置了流式回调，则调用回调
        if incremental and self._on_stream:
            await self._on_stream(incremental)

    async def on_stream_end(self, context: AgentHookContext, *, resuming: bool) -> None:
        """
        流式输出结束时的处理
        
        Args:
            context: 钩子上下文
            resuming: 是否继续执行（True 表示后面还有工具调用，
                     False 表示这是最终响应）
        """
        if self._on_stream_end:
            await self._on_stream_end(resuming=resuming)
        # 清空流式输出缓冲区
        self._stream_buf = ""

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        """
        在执行工具之前调用
        
        这个方法会：
        1. 报告代理的思考过程（如果有进度回调）
        2. 报告工具调用提示
        3. 记录工具调用日志
        4. 设置工具上下文
        
        Args:
            context: 钩子上下文，包含工具调用信息
        """
        if self._on_progress:
            # 如果没有流式输出，则报告思考过程
            if not self._on_stream:
                thought = self._loop._strip_think(
                    context.response.content if context.response else None
                )
                if thought:
                    await self._on_progress(thought)
            # 报告工具调用提示
            tool_hint = self._loop._strip_think(self._loop._tool_hint(context.tool_calls))
            await self._on_progress(tool_hint, tool_hint=True)
        # 记录工具调用日志
        for tc in context.tool_calls:
            args_str = json.dumps(tc.arguments, ensure_ascii=False)
            logger.info("Tool call: {}({})", tc.name, args_str[:200])
        # 设置工具上下文（通道、聊天ID、消息ID）
        self._loop._set_tool_context(self._channel, self._chat_id, self._message_id)

    def finalize_content(self, context: AgentHookContext, content: str | None) -> str | None:
        """
        最终化内容，剥离思考标签
        
        Args:
            context: 钩子上下文
            content: 原始内容
            
        Returns:
            处理后的内容（已剥离思考标签）
        """
        return self._loop._strip_think(content)


class _LoopHookChain(AgentHook):
    """
    循环钩子链：先运行核心循环钩子，然后运行额外的钩子
    
    这保留了 _LoopHook 的历史失败行为，同时允许用户提供的钩子
    选择加入 CompositeHook 隔离。
    
    使用责任链模式，将多个钩子串联起来执行。
    """

    __slots__ = ("_primary", "_extras")

    def __init__(self, primary: AgentHook, extra_hooks: list[AgentHook]) -> None:
        """
        初始化钩子链
        
        Args:
            primary: 主钩子（核心循环钩子）
            extra_hooks: 额外的钩子列表
        """
        self._primary = primary
        self._extras = CompositeHook(extra_hooks)

    def wants_streaming(self) -> bool:
        """检查是否需要流式输出（任一钩子需要即返回 True）"""
        return self._primary.wants_streaming() or self._extras.wants_streaming()

    async def before_iteration(self, context: AgentHookContext) -> None:
        """
        在每次迭代之前调用
        
        Args:
            context: 钩子上下文
        """
        await self._primary.before_iteration(context)
        await self._extras.before_iteration(context)

    async def on_stream(self, context: AgentHookContext, delta: str) -> None:
        """
        处理流式输出增量
        
        Args:
            context: 钩子上下文
            delta: 增量内容
        """
        await self._primary.on_stream(context, delta)
        await self._extras.on_stream(context, delta)

    async def on_stream_end(self, context: AgentHookContext, *, resuming: bool) -> None:
        """
        流式输出结束处理
        
        Args:
            context: 钩子上下文
            resuming: 是否继续执行
        """
        await self._primary.on_stream_end(context, resuming=resuming)
        await self._extras.on_stream_end(context, resuming=resuming)

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        """
        在执行工具之前调用
        
        Args:
            context: 钩子上下文
        """
        await self._primary.before_execute_tools(context)
        await self._extras.before_execute_tools(context)

    async def after_iteration(self, context: AgentHookContext) -> None:
        """
        在每次迭代之后调用
        
        Args:
            context: 钩子上下文
        """
        await self._primary.after_iteration(context)
        await self._extras.after_iteration(context)

    def finalize_content(self, context: AgentHookContext, content: str | None) -> str | None:
        """
        最终化内容
        
        Args:
            context: 钩子上下文
            content: 原始内容
            
        Returns:
            处理后的内容
        """
        content = self._primary.finalize_content(context, content)
        return self._extras.finalize_content(context, content)


class AgentLoop:
    """
    代理循环：核心处理引擎
    
    AgentLoop 是 nanobot 的核心处理引擎，负责协调整个消息处理流程。
    
    主要职责：
    1. 从消息总线接收消息
    2. 构建包含历史、记忆和技能的上下文
    3. 调用 LLM 生成响应
    4. 执行工具调用
    5. 将响应发送回通道
    
    工作流程：
    1. 消息到达 -> 2. 构建上下文 -> 3. LLM 生成 -> 4. 执行工具 -> 5. 返回响应
    """

    _TOOL_RESULT_MAX_CHARS = 16_000  # 工具结果最大字符数限制

    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        max_iterations: int = 40,
        context_window_tokens: int = 65_536,
        web_search_config: WebSearchConfig | None = None,
        web_proxy: str | None = None,
        exec_config: ExecToolConfig | None = None,
        cron_service: CronService | None = None,
        restrict_to_workspace: bool = False,
        session_manager: SessionManager | None = None,
        mcp_servers: dict | None = None,
        channels_config: ChannelsConfig | None = None,
        timezone: str | None = None,
        hooks: list[AgentHook] | None = None,
    ):
        """
        初始化代理循环
        
        Args:
            bus: 消息总线，用于接收和发送消息
            provider: LLM 提供商，用于调用 AI 模型
            workspace: 工作目录路径
            model: 使用的模型名称（如 "anthropic/claude-opus-4-5"）
            max_iterations: 最大迭代次数，防止无限循环
            context_window_tokens: 上下文窗口令牌数
            web_search_config: 网络搜索配置
            web_proxy: 网络代理地址
            exec_config: 执行工具配置
            cron_service: 定时任务服务
            restrict_to_workspace: 是否限制工具只能访问工作目录
            session_manager: 会话管理器
            mcp_servers: MCP 服务器配置
            channels_config: 通道配置
            timezone: 时区设置
            hooks: 额外的钩子列表
        """
        from nanobot.config.schema import ExecToolConfig, WebSearchConfig

        # 核心组件
        self.bus = bus  # 消息总线
        self.channels_config = channels_config  # 通道配置
        self.provider = provider  # LLM 提供商
        self.workspace = workspace  # 工作目录
        self.model = model or provider.get_default_model()  # 模型名称
        self.max_iterations = max_iterations  # 最大迭代次数
        self.context_window_tokens = context_window_tokens  # 上下文窗口大小
        
        # 工具配置
        self.web_search_config = web_search_config or WebSearchConfig()  # 网络搜索配置
        self.web_proxy = web_proxy  # 网络代理
        self.exec_config = exec_config or ExecToolConfig()  # 执行工具配置
        self.cron_service = cron_service  # 定时任务服务
        self.restrict_to_workspace = restrict_to_workspace  # 是否限制到工作目录
        
        # 运行时状态
        self._start_time = time.time()  # 启动时间
        self._last_usage: dict[str, int] = {}  # 最后一次使用的令牌统计
        self._extra_hooks: list[AgentHook] = hooks or []  # 额外的钩子

        # 初始化核心组件
        self.context = ContextBuilder(workspace, timezone=timezone)  # 上下文构建器
        self.sessions = session_manager or SessionManager(workspace)  # 会话管理器
        self.tools = ToolRegistry()  # 工具注册表
        self.runner = AgentRunner(provider)  # 代理运行器
        
        # 子代理管理器
        self.subagents = SubagentManager(
            provider=provider,
            workspace=workspace,
            bus=bus,
            model=self.model,
            web_search_config=self.web_search_config,
            web_proxy=web_proxy,
            exec_config=self.exec_config,
            restrict_to_workspace=restrict_to_workspace,
        )

        # 运行控制
        self._running = False  # 是否正在运行
        
        # MCP (Model Context Protocol) 相关
        self._mcp_servers = mcp_servers or {}  # MCP 服务器配置
        self._mcp_stack: AsyncExitStack | None = None  # MCP 异步退出栈
        self._mcp_connected = False  # MCP 是否已连接
        self._mcp_connecting = False  # MCP 是否正在连接
        
        # 任务管理
        self._active_tasks: dict[str, list[asyncio.Task]] = {}  # 活动任务（session_key -> tasks）
        self._background_tasks: list[asyncio.Task] = []  # 后台任务列表
        self._session_locks: dict[str, asyncio.Lock] = {}  # 会话锁（防止并发冲突）
        
        # 并发控制
        # NANOBOT_MAX_CONCURRENT_REQUESTS: <=0 表示无限制；默认为 3
        _max = int(os.environ.get("NANOBOT_MAX_CONCURRENT_REQUESTS", "3"))
        self._concurrency_gate: asyncio.Semaphore | None = (
            asyncio.Semaphore(_max) if _max > 0 else None
        )
        
        # 记忆整合器
        self.memory_consolidator = MemoryConsolidator(
            workspace=workspace,
            provider=provider,
            model=self.model,
            sessions=self.sessions,
            context_window_tokens=context_window_tokens,
            build_messages=self.context.build_messages,
            get_tool_definitions=self.tools.get_definitions,
            max_completion_tokens=provider.generation.max_tokens,
        )
        
        # 注册默认工具
        self._register_default_tools()
        
        # 初始化命令路由器
        self.commands = CommandRouter()
        register_builtin_commands(self.commands)

    def _register_default_tools(self) -> None:
        """
        注册默认工具集
        
        注册的工具包括：
        - 文件系统工具：读、写、编辑、列出目录
        - 网络工具：搜索、抓取
        - 执行工具：运行 shell 命令
        - 消息工具：发送消息
        - 子代理工具：创建子代理
        - 定时任务工具：设置定时任务
        """
        # 确定允许访问的目录
        allowed_dir = self.workspace if self.restrict_to_workspace else None
        extra_read = [BUILTIN_SKILLS_DIR] if allowed_dir else None
        
        # 注册文件系统工具
        self.tools.register(ReadFileTool(workspace=self.workspace, allowed_dir=allowed_dir, extra_allowed_dirs=extra_read))
        for cls in (WriteFileTool, EditFileTool, ListDirTool):
            self.tools.register(cls(workspace=self.workspace, allowed_dir=allowed_dir))
        
        # 注册执行工具（如果启用）
        if self.exec_config.enable:
            self.tools.register(ExecTool(
                working_dir=str(self.workspace),
                timeout=self.exec_config.timeout,
                restrict_to_workspace=self.restrict_to_workspace,
                path_append=self.exec_config.path_append,
            ))
        
        # 注册网络工具
        self.tools.register(WebSearchTool(config=self.web_search_config, proxy=self.web_proxy))
        self.tools.register(WebFetchTool(proxy=self.web_proxy))
        
        # 注册消息工具
        self.tools.register(MessageTool(send_callback=self.bus.publish_outbound))
        
        # 注册子代理工具
        self.tools.register(SpawnTool(manager=self.subagents))
        
        # 注册定时任务工具（如果配置了定时任务服务）
        if self.cron_service:
            self.tools.register(
                CronTool(self.cron_service, default_timezone=self.context.timezone or "UTC")
            )

    async def _connect_mcp(self) -> None:
        """
        连接到配置的 MCP 服务器（一次性，延迟连接）
        
        MCP (Model Context Protocol) 是一种用于连接外部工具服务器的协议。
        这个方法会在第一次需要时连接 MCP 服务器，之后不会重复连接。
        """
        # 如果已连接、正在连接或没有配置 MCP 服务器，则直接返回
        if self._mcp_connected or self._mcp_connecting or not self._mcp_servers:
            return
        
        self._mcp_connecting = True
        from nanobot.agent.tools.mcp import connect_mcp_servers
        
        try:
            # 创建异步退出栈，用于管理 MCP 连接的生命周期
            self._mcp_stack = AsyncExitStack()
            await self._mcp_stack.__aenter__()
            # 连接 MCP 服务器并注册工具
            await connect_mcp_servers(self._mcp_servers, self.tools, self._mcp_stack)
            self._mcp_connected = True
        except BaseException as e:
            logger.error("Failed to connect MCP servers (will retry next message): {}", e)
            # 连接失败时清理资源
            if self._mcp_stack:
                try:
                    await self._mcp_stack.aclose()
                except Exception:
                    pass
                self._mcp_stack = None
        finally:
            self._mcp_connecting = False

    def _set_tool_context(self, channel: str, chat_id: str, message_id: str | None = None) -> None:
        """
        更新需要路由信息的工具的上下文
        
        某些工具（如消息工具、子代理工具、定时任务工具）需要知道当前
        的通道和聊天 ID 才能正确工作。这个方法会更新这些工具的上下文。
        
        Args:
            channel: 通道名称
            chat_id: 聊天 ID
            message_id: 消息 ID（可选）
        """
        for name in ("message", "spawn", "cron"):
            if tool := self.tools.get(name):
                if hasattr(tool, "set_context"):
                    tool.set_context(channel, chat_id, *([message_id] if name == "message" else []))

    @staticmethod
    def _strip_think(text: str | None) -> str | None:
        """
        移除某些模型嵌入内容中的 <think…</think > 块
        
        某些 AI 模型会在输出中包含思考过程的标签，这个方法会将其移除，
        只保留实际的响应内容。
        
        Args:
            text: 原始文本
            
        Returns:
            移除思考标签后的文本
        """
        if not text:
            return None
        from nanobot.utils.helpers import strip_think
        return strip_think(text) or None

    @staticmethod
    def _tool_hint(tool_calls: list) -> str:
        """
        将工具调用格式化为简洁的提示
        
        例如：'web_search("query")'
        
        Args:
            tool_calls: 工具调用列表
            
        Returns:
            格式化后的工具调用提示字符串
        """
        def _fmt(tc):
            # 获取工具参数
            args = (tc.arguments[0] if isinstance(tc.arguments, list) else tc.arguments) or {}
            # 获取第一个参数值
            val = next(iter(args.values()), None) if isinstance(args, dict) else None
            if not isinstance(val, str):
                return tc.name
            # 截断过长的参数值
            return f'{tc.name}("{val[:40]}…")' if len(val) > 40 else f'{tc.name}("{val}")'
        return ", ".join(_fmt(tc) for tc in tool_calls)

    async def _run_agent_loop(
        self,
        initial_messages: list[dict],
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        *,
        channel: str = "cli",
        chat_id: str = "direct",
        message_id: str | None = None,
    ) -> tuple[str | None, list[str], list[dict]]:
        """
        运行代理迭代循环
        
        这是核心的 LLM 交互循环，会：
        1. 调用 LLM 生成响应
        2. 执行工具调用
        3. 循环直到完成或达到最大迭代次数
        
        Args:
            initial_messages: 初始消息列表
            on_progress: 进度回调函数
            on_stream: 流式输出回调函数，每个内容增量时调用
            on_stream_end: 流式输出结束回调函数
                - resuming=True 表示后面还有工具调用（加载动画应重启）
                - resuming=False 表示这是最终响应
            channel: 通道名称
            chat_id: 聊天 ID
            message_id: 消息 ID
            
        Returns:
            元组：(最终内容, 使用的工具列表, 所有消息)
        """
        # 创建循环钩子
        loop_hook = _LoopHook(
            self,
            on_progress=on_progress,
            on_stream=on_stream,
            on_stream_end=on_stream_end,
            channel=channel,
            chat_id=chat_id,
            message_id=message_id,
        )
        
        # 如果有额外的钩子，创建钩子链
        hook: AgentHook = (
            _LoopHookChain(loop_hook, self._extra_hooks)
            if self._extra_hooks
            else loop_hook
        )

        # 运行代理
        result = await self.runner.run(AgentRunSpec(
            initial_messages=initial_messages,
            tools=self.tools,
            model=self.model,
            max_iterations=self.max_iterations,
            hook=hook,
            error_message="Sorry, I encountered an error calling the AI model.",
            concurrent_tools=True,  # 允许并发执行工具
        ))
        
        # 记录令牌使用情况
        self._last_usage = result.usage
        
        # 检查停止原因
        if result.stop_reason == "max_iterations":
            logger.warning("Max iterations ({}) reached", self.max_iterations)
        elif result.stop_reason == "error":
            logger.error("LLM returned error: {}", (result.final_content or "")[:200])
        
        return result.final_content, result.tools_used, result.messages

    async def run(self) -> None:
        """
        运行代理循环，将消息作为任务分发以保持对 /stop 命令的响应
        
        这是代理的主循环，会：
        1. 从消息总线接收消息
        2. 检查是否是优先命令（如 /stop）
        3. 为每条消息创建异步任务进行处理
        4. 跟踪活动任务以便于管理
        """
        self._running = True
        await self._connect_mcp()  # 连接 MCP 服务器
        logger.info("Agent loop started")

        while self._running:
            try:
                # 从消息总线获取消息，超时时间为 1 秒
                msg = await asyncio.wait_for(self.bus.consume_inbound(), timeout=1.0)
            except asyncio.TimeoutError:
                # 超时后继续循环
                continue
            except asyncio.CancelledError:
                # 保留真正的任务取消，以便关闭可以干净地完成
                # 只忽略可能从集成中泄漏的非任务 CancelledError 信号
                if not self._running or asyncio.current_task().cancelling():
                    raise
                continue
            except Exception as e:
                logger.warning("Error consuming inbound message: {}, continuing...", e)
                continue

            # 获取消息内容
            raw = msg.content.strip()
            
            # 检查是否是优先命令（如 /stop）
            if self.commands.is_priority(raw):
                ctx = CommandContext(msg=msg, session=None, key=msg.session_key, raw=raw, loop=self)
                result = await self.commands.dispatch_priority(ctx)
                if result:
                    await self.bus.publish_outbound(result)
                continue
            
            # 为消息创建异步任务
            task = asyncio.create_task(self._dispatch(msg))
            self._active_tasks.setdefault(msg.session_key, []).append(task)
            # 任务完成后从活动任务列表中移除
            task.add_done_callback(lambda t, k=msg.session_key: self._active_tasks.get(k, []) and self._active_tasks[k].remove(t) if t in self._active_tasks.get(k, []) else None)

    async def _dispatch(self, msg: InboundMessage) -> None:
        """
        处理消息：会话内串行，跨会话并发
        
        这个方法确保：
        1. 同一会话内的消息按顺序处理（使用锁）
        2. 不同会话的消息可以并发处理
        3. 全局并发请求数受限制（使用信号量）
        
        Args:
            msg: 入站消息
        """
        # 获取或创建会话锁
        lock = self._session_locks.setdefault(msg.session_key, asyncio.Lock())
        # 获取并发控制门
        gate = self._concurrency_gate or nullcontext()
        
        async with lock, gate:
            try:
                on_stream = on_stream_end = None
                
                # 如果请求流式输出，设置流式回调
                if msg.metadata.get("_wants_stream"):
                    # 将一个答案分割成不同的流段
                    stream_base_id = f"{msg.session_key}:{time.time_ns()}"
                    stream_segment = 0

                    def _current_stream_id() -> str:
                        """获取当前流段 ID"""
                        return f"{stream_base_id}:{stream_segment}"

                    async def on_stream(delta: str) -> None:
                        """流式输出回调：发送增量内容"""
                        meta = dict(msg.metadata or {})
                        meta["_stream_delta"] = True
                        meta["_stream_id"] = _current_stream_id()
                        await self.bus.publish_outbound(OutboundMessage(
                            channel=msg.channel, chat_id=msg.chat_id,
                            content=delta,
                            metadata=meta,
                        ))

                    async def on_stream_end(*, resuming: bool = False) -> None:
                        """流式输出结束回调：发送结束标记"""
                        nonlocal stream_segment
                        meta = dict(msg.metadata or {})
                        meta["_stream_end"] = True
                        meta["_resuming"] = resuming
                        meta["_stream_id"] = _current_stream_id()
                        await self.bus.publish_outbound(OutboundMessage(
                            channel=msg.channel, chat_id=msg.chat_id,
                            content="",
                            metadata=meta,
                        ))
                        stream_segment += 1

                # 处理消息
                response = await self._process_message(
                    msg, on_stream=on_stream, on_stream_end=on_stream_end,
                )
                
                # 发送响应
                if response is not None:
                    await self.bus.publish_outbound(response)
                elif msg.channel == "cli":
                    # CLI 通道需要空响应来结束等待
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=msg.channel, chat_id=msg.chat_id,
                        content="", metadata=msg.metadata or {},
                    ))
            except asyncio.CancelledError:
                logger.info("Task cancelled for session {}", msg.session_key)
                raise
            except Exception:
                logger.exception("Error processing message for session {}", msg.session_key)
                # 发送错误响应
                await self.bus.publish_outbound(OutboundMessage(
                    channel=msg.channel, chat_id=msg.chat_id,
                    content="Sorry, I encountered an error.",
                ))

    async def close_mcp(self) -> None:
        """
        排空待处理的后台归档任务，然后关闭 MCP 连接
        
        这个方法在代理关闭时调用，确保所有后台任务完成，
        并正确关闭 MCP 连接。
        """
        # 等待所有后台任务完成
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
            self._background_tasks.clear()
        
        # 关闭 MCP 连接
        if self._mcp_stack:
            try:
                await self._mcp_stack.aclose()
            except (RuntimeError, BaseExceptionGroup):
                pass  # MCP SDK 取消作用域清理很嘈杂但无害
            self._mcp_stack = None

    def _schedule_background(self, coro) -> None:
        """
        将协程调度为跟踪的后台任务（在关闭时排空）
        
        Args:
            coro: 要调度的协程
        """
        task = asyncio.create_task(coro)
        self._background_tasks.append(task)
        task.add_done_callback(self._background_tasks.remove)

    def stop(self) -> None:
        """停止代理循环"""
        self._running = False
        logger.info("Agent loop stopping")

    async def _process_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
    ) -> OutboundMessage | None:
        """
        处理单个入站消息并返回响应
        
        这是消息处理的核心方法，会：
        1. 处理系统消息（如子代理消息）
        2. 处理斜杠命令
        3. 构建上下文
        4. 运行代理循环
        5. 保存会话历史
        
        Args:
            msg: 入站消息
            session_key: 会话键（可选）
            on_progress: 进度回调函数
            on_stream: 流式输出回调函数
            on_stream_end: 流式输出结束回调函数
            
        Returns:
            出站消息，如果不需要响应则返回 None
        """
        # 系统消息：从 chat_id 解析来源（"channel:chat_id"）
        if msg.channel == "system":
            channel, chat_id = (msg.chat_id.split(":", 1) if ":" in msg.chat_id
                                else ("cli", msg.chat_id))
            logger.info("Processing system message from {}", msg.sender_id)
            
            # 获取或创建会话
            key = f"{channel}:{chat_id}"
            session = self.sessions.get_or_create(key)
            
            # 可能进行记忆整合
            await self.memory_consolidator.maybe_consolidate_by_tokens(session)
            
            # 设置工具上下文
            self._set_tool_context(channel, chat_id, msg.metadata.get("message_id"))
            
            # 获取历史记录
            history = session.get_history(max_messages=0)
            
            # 确定当前角色（子代理消息为 assistant，否则为 user）
            current_role = "assistant" if msg.sender_id == "subagent" else "user"
            
            # 构建消息
            messages = self.context.build_messages(
                history=history,
                current_message=msg.content, channel=channel, chat_id=chat_id,
                current_role=current_role,
            )
            
            # 运行代理循环
            final_content, _, all_msgs = await self._run_agent_loop(
                messages, channel=channel, chat_id=chat_id,
                message_id=msg.metadata.get("message_id"),
            )
            
            # 保存会话
            self._save_turn(session, all_msgs, 1 + len(history))
            self.sessions.save(session)
            
            # 调度后台记忆整合
            self._schedule_background(self.memory_consolidator.maybe_consolidate_by_tokens(session))
            
            return OutboundMessage(channel=channel, chat_id=chat_id,
                                  content=final_content or "Background task completed.")

        # 记录消息预览
        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
        logger.info("Processing message from {}:{}: {}", msg.channel, msg.sender_id, preview)

        # 获取或创建会话
        key = session_key or msg.session_key
        session = self.sessions.get_or_create(key)

        # 处理斜杠命令
        raw = msg.content.strip()
        ctx = CommandContext(msg=msg, session=session, key=key, raw=raw, loop=self)
        if result := await self.commands.dispatch(ctx):
            return result

        # 可能进行记忆整合
        await self.memory_consolidator.maybe_consolidate_by_tokens(session)

        # 设置工具上下文
        self._set_tool_context(msg.channel, msg.chat_id, msg.metadata.get("message_id"))
        
        # 开始消息工具的新轮次
        if message_tool := self.tools.get("message"):
            if isinstance(message_tool, MessageTool):
                message_tool.start_turn()

        # 获取历史记录并构建消息
        history = session.get_history(max_messages=0)
        initial_messages = self.context.build_messages(
            history=history,
            current_message=msg.content,
            media=msg.media if msg.media else None,
            channel=msg.channel, chat_id=msg.chat_id,
        )

        # 定义进度回调（通过消息总线发送进度）
        async def _bus_progress(content: str, *, tool_hint: bool = False) -> None:
            meta = dict(msg.metadata or {})
            meta["_progress"] = True
            meta["_tool_hint"] = tool_hint
            await self.bus.publish_outbound(OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id, content=content, metadata=meta,
            ))

        # 运行代理循环
        final_content, _, all_msgs = await self._run_agent_loop(
            initial_messages,
            on_progress=on_progress or _bus_progress,
            on_stream=on_stream,
            on_stream_end=on_stream_end,
            channel=msg.channel, chat_id=msg.chat_id,
            message_id=msg.metadata.get("message_id"),
        )

        # 如果没有内容，设置默认响应
        if final_content is None:
            final_content = "I've completed processing but have no response to give."

        # 保存会话
        self._save_turn(session, all_msgs, 1 + len(history))
        self.sessions.save(session)
        
        # 调度后台记忆整合
        self._schedule_background(self.memory_consolidator.maybe_consolidate_by_tokens(session))

        # 如果消息工具在本轮中已发送消息，则不需要再发送响应
        if (mt := self.tools.get("message")) and isinstance(mt, MessageTool) and mt._sent_in_turn:
            return None

        # 记录响应预览
        preview = final_content[:120] + "..." if len(final_content) > 120 else final_content
        logger.info("Response to {}:{}: {}", msg.channel, msg.sender_id, preview)

        # 构建出站消息
        meta = dict(msg.metadata or {})
        if on_stream is not None:
            meta["_streamed"] = True
        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id, content=final_content,
            metadata=meta,
        )

    @staticmethod
    def _image_placeholder(block: dict[str, Any]) -> dict[str, str]:
        """
        将内联图像块转换为紧凑的文本占位符
        
        Args:
            block: 图像块字典
            
        Returns:
            文本占位符字典
        """
        path = (block.get("_meta") or {}).get("path", "")
        return {"type": "text", "text": f"[image: {path}]" if path else "[image]"}

    def _sanitize_persisted_blocks(
        self,
        content: list[dict[str, Any]],
        *,
        truncate_text: bool = False,
        drop_runtime: bool = False,
    ) -> list[dict[str, Any]]:
        """
        在写入会话历史之前剥离易失性多模态载荷
        
        这个方法会：
        1. 移除运行时上下文（如果 drop_runtime=True）
        2. 将内联图像转换为占位符
        3. 截断过长的文本（如果 truncate_text=True）
        
        Args:
            content: 内容块列表
            truncate_text: 是否截断文本
            drop_runtime: 是否移除运行时上下文
            
        Returns:
            过滤后的内容块列表
        """
        filtered: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, dict):
                filtered.append(block)
                continue

            # 移除运行时上下文
            if (
                drop_runtime
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
                and block["text"].startswith(ContextBuilder._RUNTIME_CONTEXT_TAG)
            ):
                continue

            # 将内联图像转换为占位符
            if (
                block.get("type") == "image_url"
                and block.get("image_url", {}).get("url", "").startswith("data:image/")
            ):
                filtered.append(self._image_placeholder(block))
                continue

            # 处理文本块
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                text = block["text"]
                # 截断过长的文本
                if truncate_text and len(text) > self._TOOL_RESULT_MAX_CHARS:
                    text = text[:self._TOOL_RESULT_MAX_CHARS] + "\n... (truncated)"
                filtered.append({**block, "text": text})
                continue

            filtered.append(block)

        return filtered

    def _save_turn(self, session: Session, messages: list[dict], skip: int) -> None:
        """
        将新轮次消息保存到会话中，截断大型工具结果
        
        Args:
            session: 会话对象
            messages: 消息列表
            skip: 跳过的消息数量（通常是历史消息数量）
        """
        from datetime import datetime
        
        for m in messages[skip:]:
            entry = dict(m)
            role, content = entry.get("role"), entry.get("content")
            
            # 跳过空的 assistant 消息（它们会污染会话上下文）
            if role == "assistant" and not content and not entry.get("tool_calls"):
                continue
            
            # 处理工具消息
            if role == "tool":
                if isinstance(content, str) and len(content) > self._TOOL_RESULT_MAX_CHARS:
                    entry["content"] = content[:self._TOOL_RESULT_MAX_CHARS] + "\n... (truncated)"
                elif isinstance(content, list):
                    filtered = self._sanitize_persisted_blocks(content, truncate_text=True)
                    if not filtered:
                        continue
                    entry["content"] = filtered
            
            # 处理用户消息
            elif role == "user":
                if isinstance(content, str) and content.startswith(ContextBuilder._RUNTIME_CONTEXT_TAG):
                    # 剥离运行时上下文前缀，只保留用户文本
                    parts = content.split("\n\n", 1)
                    if len(parts) > 1 and parts[1].strip():
                        entry["content"] = parts[1]
                    else:
                        continue
                if isinstance(content, list):
                    filtered = self._sanitize_persisted_blocks(content, drop_runtime=True)
                    if not filtered:
                        continue
                    entry["content"] = filtered
            
            # 设置时间戳
            entry.setdefault("timestamp", datetime.now().isoformat())
            session.messages.append(entry)
        
        # 更新会话修改时间
        session.updated_at = datetime.now()

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
        on_progress: Callable[[str], Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
    ) -> OutboundMessage | None:
        """
        直接处理消息并返回出站载荷
        
        这个方法用于直接处理消息，不通过消息总线。
        通常用于 CLI 模式或 API 调用。
        
        Args:
            content: 消息内容
            session_key: 会话键
            channel: 通道名称
            chat_id: 聊天 ID
            on_progress: 进度回调函数
            on_stream: 流式输出回调函数
            on_stream_end: 流式输出结束回调函数
            
        Returns:
            出站消息
        """
        await self._connect_mcp()
        msg = InboundMessage(channel=channel, sender_id="user", chat_id=chat_id, content=content)
        return await self._process_message(
            msg, session_key=session_key, on_progress=on_progress,
            on_stream=on_stream, on_stream_end=on_stream_end,
        )
