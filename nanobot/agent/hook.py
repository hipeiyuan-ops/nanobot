"""
Agent 生命周期钩子模块，提供在 agent 执行过程中注入自定义逻辑的能力。

该模块定义了 agent 运行时的生命周期钩子系统，允许外部代码在 agent 执行的
各个阶段插入自定义逻辑。主要用途包括：
1. 监控和日志记录
2. 流式输出处理
3. 自定义内容处理
4. 多个钩子的组合执行
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from nanobot.providers.base import LLMResponse, ToolCallRequest


@dataclass(slots=True)
class AgentHookContext:
    """
    Agent 每次迭代的可变状态上下文。

    该数据类封装了 agent 单次迭代中的所有状态信息，供钩子访问和修改。
    使用 slots=True 优化内存使用。

    Attributes:
        iteration: 当前迭代次数（从 0 开始）
        messages: 当前消息列表
        response: LLM 的响应对象（如果有的话）
        usage: token 使用统计（prompt_tokens, completion_tokens）
        tool_calls: 本次迭代中的工具调用列表
        tool_results: 工具执行结果列表
        tool_events: 工具事件列表（用于记录工具执行的状态）
        final_content: 最终输出内容
        stop_reason: 停止原因（completed, max_iterations, error, tool_error）
        error: 错误信息（如果有的话）
    """

    iteration: int
    messages: list[dict[str, Any]]
    response: LLMResponse | None = None
    usage: dict[str, int] = field(default_factory=dict)
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    tool_results: list[Any] = field(default_factory=list)
    tool_events: list[dict[str, str]] = field(default_factory=list)
    final_content: str | None = None
    stop_reason: str | None = None
    error: str | None = None


class AgentHook:
    """
    Agent 生命周期钩子的基类。

    该类定义了 agent 运行时的生命周期接口，子类可以重写这些方法来注入自定义逻辑。
    所有方法都有默认的空实现，子类只需重写需要的方法。

    生命周期顺序：
    1. before_iteration - 迭代开始前
    2. on_stream - 流式输出时（如果启用流式）
    3. on_stream_end - 流式输出结束时
    4. before_execute_tools - 工具执行前
    5. after_iteration - 迭代结束后
    6. finalize_content - 内容最终处理

    钩子可以用于：
    - 监控和日志
    - 流式输出到 UI
    - 修改消息或工具调用
    - 自定义内容后处理
    """

    def wants_streaming(self) -> bool:
        """
        指示是否需要流式输出。

        如果返回 True，agent 会使用流式 API 调用 LLM，
        并通过 on_stream 回调传递增量内容。

        Returns:
            True 表示需要流式输出，False 表示不需要
        """
        return False

    async def before_iteration(self, context: AgentHookContext) -> None:
        """
        在每次迭代开始前调用。

        可以用于准备状态、修改消息等。

        Args:
            context: 当前迭代的上下文
        """
        pass

    async def on_stream(self, context: AgentHookContext, delta: str) -> None:
        """
        在流式输出时调用，接收增量内容。

        只有当 wants_streaming() 返回 True 时才会被调用。

        Args:
            context: 当前迭代的上下文
            delta: 增量文本内容
        """
        pass

    async def on_stream_end(self, context: AgentHookContext, *, resuming: bool) -> None:
        """
        在流式输出结束时调用。

        Args:
            context: 当前迭代的上下文
            resuming: 是否即将继续下一轮迭代（有工具调用）
        """
        pass

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        """
        在工具执行前调用。

        可以用于修改工具调用参数、添加日志等。

        Args:
            context: 当前迭代的上下文
        """
        pass

    async def after_iteration(self, context: AgentHookContext) -> None:
        """
        在每次迭代结束后调用。

        可以用于清理状态、记录结果等。

        Args:
            context: 当前迭代的上下文
        """
        pass

    def finalize_content(self, context: AgentHookContext, content: str | None) -> str | None:
        """
        对最终内容进行后处理。

        可以用于修改、过滤或增强最终输出内容。
        注意：这个方法不是异步的，且没有错误隔离（bug 应该暴露出来）。

        Args:
            context: 当前迭代的上下文
            content: 原始内容

        Returns:
            处理后的内容
        """
        return content


class CompositeHook(AgentHook):
    """
    组合钩子，将多个钩子串联执行。

    该类实现了组合模式，允许将多个钩子组合成一个钩子链。
    所有钩子按顺序执行，且异步方法具有错误隔离：
    单个钩子的异常会被捕获并记录，不会影响其他钩子的执行。

    特性：
    - 错误隔离：异步方法中的异常会被捕获并记录，不会中断其他钩子
    - 流式传播：只要有一个钩子需要流式，整体就启用流式
    - 内容管道：finalize_content 会形成处理管道，前一个的输出是后一个的输入

    Attributes:
        _hooks: 钩子列表
    """

    __slots__ = ("_hooks",)

    def __init__(self, hooks: list[AgentHook]) -> None:
        """
        初始化组合钩子。

        Args:
            hooks: 要组合的钩子列表
        """
        self._hooks = list(hooks)

    def wants_streaming(self) -> bool:
        """
        只要有任何一个钩子需要流式，就启用流式。

        Returns:
            是否需要流式输出
        """
        return any(h.wants_streaming() for h in self._hooks)

    async def before_iteration(self, context: AgentHookContext) -> None:
        """
        按顺序调用所有钩子的 before_iteration 方法。

        每个钩子的异常会被捕获并记录，不会影响其他钩子。
        """
        for h in self._hooks:
            try:
                await h.before_iteration(context)
            except Exception:
                logger.exception("AgentHook.before_iteration error in {}", type(h).__name__)

    async def on_stream(self, context: AgentHookContext, delta: str) -> None:
        """
        按顺序调用所有钩子的 on_stream 方法。

        每个钩子的异常会被捕获并记录，不会影响其他钩子。
        """
        for h in self._hooks:
            try:
                await h.on_stream(context, delta)
            except Exception:
                logger.exception("AgentHook.on_stream error in {}", type(h).__name__)

    async def on_stream_end(self, context: AgentHookContext, *, resuming: bool) -> None:
        """
        按顺序调用所有钩子的 on_stream_end 方法。

        每个钩子的异常会被捕获并记录，不会影响其他钩子。
        """
        for h in self._hooks:
            try:
                await h.on_stream_end(context, resuming=resuming)
            except Exception:
                logger.exception("AgentHook.on_stream_end error in {}", type(h).__name__)

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        """
        按顺序调用所有钩子的 before_execute_tools 方法。

        每个钩子的异常会被捕获并记录，不会影响其他钩子。
        """
        for h in self._hooks:
            try:
                await h.before_execute_tools(context)
            except Exception:
                logger.exception("AgentHook.before_execute_tools error in {}", type(h).__name__)

    async def after_iteration(self, context: AgentHookContext) -> None:
        """
        按顺序调用所有钩子的 after_iteration 方法。

        每个钩子的异常会被捕获并记录，不会影响其他钩子。
        """
        for h in self._hooks:
            try:
                await h.after_iteration(context)
            except Exception:
                logger.exception("AgentHook.after_iteration error in {}", type(h).__name__)

    def finalize_content(self, context: AgentHookContext, content: str | None) -> str | None:
        """
        形成处理管道，依次调用每个钩子的 finalize_content 方法。

        前一个钩子的输出是后一个钩子的输入。
        注意：这个方法没有错误隔离，bug 应该暴露出来。

        Args:
            context: 当前迭代的上下文
            content: 原始内容

        Returns:
            经过所有钩子处理后的内容
        """
        for h in self._hooks:
            content = h.finalize_content(context, content)
        return content
