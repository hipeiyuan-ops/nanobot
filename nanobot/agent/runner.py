"""
Agent 执行循环模块，提供工具调用 agent 的核心执行逻辑。

该模块实现了 agent 的主执行循环，负责：
1. 调用 LLM 并处理响应
2. 执行工具调用并收集结果
3. 管理迭代次数和停止条件
4. 支持流式输出和钩子扩展

这是一个与产品层无关的通用执行器，可以被不同的前端使用。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from nanobot.agent.hook import AgentHook, AgentHookContext
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.providers.base import LLMProvider, ToolCallRequest
from nanobot.utils.helpers import build_assistant_message

# 默认消息模板
_DEFAULT_MAX_ITERATIONS_MESSAGE = (
    "I reached the maximum number of tool call iterations ({max_iterations}) "
    "without completing the task. You can try breaking the task into smaller steps."
)
_DEFAULT_ERROR_MESSAGE = "Sorry, I encountered an error calling the AI model."


@dataclass(slots=True)
class AgentRunSpec:
    """
    单次 agent 执行的配置规格。

    该数据类封装了执行 agent 所需的所有配置参数。

    Attributes:
        initial_messages: 初始消息列表（包含系统提示和历史）
        tools: 工具注册表
        model: 使用的模型名称
        max_iterations: 最大迭代次数
        temperature: 生成温度（可选）
        max_tokens: 最大生成 token 数（可选）
        reasoning_effort: 推理努力程度（可选，某些模型支持）
        hook: 生命周期钩子（可选）
        error_message: 错误时的消息模板
        max_iterations_message: 达到最大迭代时的消息模板
        concurrent_tools: 是否并发执行工具
        fail_on_tool_error: 工具错误是否导致执行失败
    """

    initial_messages: list[dict[str, Any]]
    tools: ToolRegistry
    model: str
    max_iterations: int
    temperature: float | None = None
    max_tokens: int | None = None
    reasoning_effort: str | None = None
    hook: AgentHook | None = None
    error_message: str | None = _DEFAULT_ERROR_MESSAGE
    max_iterations_message: str | None = None
    concurrent_tools: bool = False
    fail_on_tool_error: bool = False


@dataclass(slots=True)
class AgentRunResult:
    """
    Agent 执行的结果。

    该数据类封装了 agent 执行完成后的所有输出信息。

    Attributes:
        final_content: 最终输出内容
        messages: 完整的消息列表（包含所有迭代）
        tools_used: 使用的工具名称列表
        usage: token 使用统计
        stop_reason: 停止原因（completed, max_iterations, error, tool_error）
        error: 错误信息（如果有的话）
        tool_events: 工具事件列表
    """

    final_content: str | None
    messages: list[dict[str, Any]]
    tools_used: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    stop_reason: str = "completed"
    error: str | None = None
    tool_events: list[dict[str, str]] = field(default_factory=list)


class AgentRunner:
    """
    工具调用 agent 的执行器。

    该类实现了 agent 的核心执行循环，不包含产品层的逻辑。
    主要流程：
    1. 初始化消息列表和状态
    2. 循环调用 LLM 直到完成或达到最大迭代
    3. 如果有工具调用，执行工具并将结果添加到消息
    4. 如果没有工具调用，返回最终内容

    支持的特性：
    - 流式输出（通过钩子）
    - 并发工具执行
    - 自定义错误处理
    - 生命周期钩子
    """

    def __init__(self, provider: LLMProvider):
        """
        初始化执行器。

        Args:
            provider: LLM 提供商实例
        """
        self.provider = provider

    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        """
        执行 agent 运行规格。

        这是执行器的主入口方法，实现了完整的执行循环。

        Args:
            spec: 执行规格

        Returns:
            执行结果
        """
        # 初始化钩子（如果没有提供则使用空钩子）
        hook = spec.hook or AgentHook()
        # 复制初始消息列表
        messages = list(spec.initial_messages)
        final_content: str | None = None
        tools_used: list[str] = []
        usage = {"prompt_tokens": 0, "completion_tokens": 0}
        error: str | None = None
        stop_reason = "completed"
        tool_events: list[dict[str, str]] = []

        # 主执行循环
        for iteration in range(spec.max_iterations):
            # 创建迭代上下文
            context = AgentHookContext(iteration=iteration, messages=messages)
            # 调用钩子的迭代前回调
            await hook.before_iteration(context)

            # 构建 LLM 调用参数
            kwargs: dict[str, Any] = {
                "messages": messages,
                "tools": spec.tools.get_definitions(),
                "model": spec.model,
            }
            if spec.temperature is not None:
                kwargs["temperature"] = spec.temperature
            if spec.max_tokens is not None:
                kwargs["max_tokens"] = spec.max_tokens
            if spec.reasoning_effort is not None:
                kwargs["reasoning_effort"] = spec.reasoning_effort

            # 根据是否需要流式输出选择不同的调用方式
            if hook.wants_streaming():
                # 流式调用
                async def _stream(delta: str) -> None:
                    await hook.on_stream(context, delta)

                response = await self.provider.chat_stream_with_retry(
                    **kwargs,
                    on_content_delta=_stream,
                )
            else:
                # 非流式调用
                response = await self.provider.chat_with_retry(**kwargs)

            # 更新使用统计
            raw_usage = response.usage or {}
            usage = {
                "prompt_tokens": int(raw_usage.get("prompt_tokens", 0) or 0),
                "completion_tokens": int(raw_usage.get("completion_tokens", 0) or 0),
            }
            # 更新上下文
            context.response = response
            context.usage = usage
            context.tool_calls = list(response.tool_calls)

            # 处理工具调用
            if response.has_tool_calls:
                # 流式输出结束时通知钩子
                if hook.wants_streaming():
                    await hook.on_stream_end(context, resuming=True)

                # 添加 assistant 消息（包含工具调用）
                messages.append(build_assistant_message(
                    response.content or "",
                    tool_calls=[tc.to_openai_tool_call() for tc in response.tool_calls],
                    reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                ))
                # 记录使用的工具
                tools_used.extend(tc.name for tc in response.tool_calls)

                # 调用钩子的工具执行前回调
                await hook.before_execute_tools(context)

                # 执行工具
                results, new_events, fatal_error = await self._execute_tools(spec, response.tool_calls)
                tool_events.extend(new_events)
                # 更新上下文
                context.tool_results = list(results)
                context.tool_events = list(new_events)

                # 处理致命错误
                if fatal_error is not None:
                    error = f"Error: {type(fatal_error).__name__}: {fatal_error}"
                    stop_reason = "tool_error"
                    context.error = error
                    context.stop_reason = stop_reason
                    await hook.after_iteration(context)
                    break

                # 添加工具结果消息
                for tool_call, result in zip(response.tool_calls, results):
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": tool_call.name,
                        "content": result,
                    })
                # 调用钩子的迭代后回调
                await hook.after_iteration(context)
                continue

            # 没有工具调用，处理最终响应
            if hook.wants_streaming():
                await hook.on_stream_end(context, resuming=False)

            # 通过钩子处理内容
            clean = hook.finalize_content(context, response.content)

            # 处理错误响应
            if response.finish_reason == "error":
                final_content = clean or spec.error_message or _DEFAULT_ERROR_MESSAGE
                stop_reason = "error"
                error = final_content
                context.final_content = final_content
                context.error = error
                context.stop_reason = stop_reason
                await hook.after_iteration(context)
                break

            # 正常完成
            messages.append(build_assistant_message(
                clean,
                reasoning_content=response.reasoning_content,
                thinking_blocks=response.thinking_blocks,
            ))
            final_content = clean
            context.final_content = final_content
            context.stop_reason = stop_reason
            await hook.after_iteration(context)
            break
        else:
            # 达到最大迭代次数
            stop_reason = "max_iterations"
            template = spec.max_iterations_message or _DEFAULT_MAX_ITERATIONS_MESSAGE
            final_content = template.format(max_iterations=spec.max_iterations)

        return AgentRunResult(
            final_content=final_content,
            messages=messages,
            tools_used=tools_used,
            usage=usage,
            stop_reason=stop_reason,
            error=error,
            tool_events=tool_events,
        )

    async def _execute_tools(
        self,
        spec: AgentRunSpec,
        tool_calls: list[ToolCallRequest],
    ) -> tuple[list[Any], list[dict[str, str]], BaseException | None]:
        """
        执行工具调用列表。

        根据配置选择并发或顺序执行。

        Args:
            spec: 执行规格
            tool_calls: 工具调用列表

        Returns:
            (结果列表, 事件列表, 致命错误) 的元组
        """
        if spec.concurrent_tools:
            # 并发执行所有工具
            tool_results = await asyncio.gather(*(
                self._run_tool(spec, tool_call)
                for tool_call in tool_calls
            ))
        else:
            # 顺序执行所有工具
            tool_results = [
                await self._run_tool(spec, tool_call)
                for tool_call in tool_calls
            ]

        # 收集结果
        results: list[Any] = []
        events: list[dict[str, str]] = []
        fatal_error: BaseException | None = None
        for result, event, error in tool_results:
            results.append(result)
            events.append(event)
            # 只记录第一个致命错误
            if error is not None and fatal_error is None:
                fatal_error = error
        return results, events, fatal_error

    async def _run_tool(
        self,
        spec: AgentRunSpec,
        tool_call: ToolCallRequest,
    ) -> tuple[Any, dict[str, str], BaseException | None]:
        """
        执行单个工具调用。

        Args:
            spec: 执行规格
            tool_call: 工具调用请求

        Returns:
            (结果, 事件, 错误) 的元组
        """
        try:
            result = await spec.tools.execute(tool_call.name, tool_call.arguments)
        except asyncio.CancelledError:
            # 取消错误需要向上传播
            raise
        except BaseException as exc:
            # 其他异常转换为错误消息
            event = {
                "name": tool_call.name,
                "status": "error",
                "detail": str(exc),
            }
            if spec.fail_on_tool_error:
                # 如果配置为失败，返回错误和异常
                return f"Error: {type(exc).__name__}: {exc}", event, exc
            # 否则只返回错误消息
            return f"Error: {type(exc).__name__}: {exc}", event, None

        # 构建事件详情（截断过长的输出）
        detail = "" if result is None else str(result)
        detail = detail.replace("\n", " ").strip()
        if not detail:
            detail = "(empty)"
        elif len(detail) > 120:
            detail = detail[:120] + "..."
        return result, {
            "name": tool_call.name,
            "status": "error" if isinstance(result, str) and result.startswith("Error") else "ok",
            "detail": detail,
        }, None
