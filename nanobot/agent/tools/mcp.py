"""
MCP（Model Context Protocol）客户端模块，连接 MCP 服务器并将其工具包装为原生 nanobot 工具。

MCP 是一个开放协议，允许外部工具服务器与 AI 模型集成。
该模块实现了 MCP 客户端，支持：
- stdio 传输：通过标准输入输出与本地进程通信
- SSE 传输：通过 Server-Sent Events 与 HTTP 服务器通信
- streamableHttp 传输：通过 HTTP 流与服务器通信

连接后，MCP 服务器的工具会自动注册到 nanobot 的工具注册表。
"""

import asyncio
from contextlib import AsyncExitStack
from typing import Any

import httpx
from loguru import logger

from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.registry import ToolRegistry


def _extract_nullable_branch(options: Any) -> tuple[dict[str, Any], bool] | None:
    """
    从可空联合类型中提取非 null 分支。

    JSON Schema 允许可空类型，如 {"oneOf": [{"type": "string"}, {"type": "null"}]}。
    该函数提取非 null 的分支，用于 OpenAI 格式转换。

    Args:
        options: oneOf/anyOf 选项列表

    Returns:
        (非 null 分支, 是否可空) 或 None
    """
    if not isinstance(options, list):
        return None

    non_null: list[dict[str, Any]] = []
    saw_null = False
    for option in options:
        if not isinstance(option, dict):
            return None
        if option.get("type") == "null":
            saw_null = True
            continue
        non_null.append(option)

    if saw_null and len(non_null) == 1:
        return non_null[0], True
    return None


def _normalize_schema_for_openai(schema: Any) -> dict[str, Any]:
    """
    将 JSON Schema 规范化为 OpenAI 兼容格式。

    主要处理可空类型的转换：
    - {"type": ["string", "null"]} -> {"type": "string", "nullable": true}
    - {"oneOf": [...]} 可空联合 -> 单一类型 + nullable

    Args:
        schema: 原始 JSON Schema

    Returns:
        规范化后的 JSON Schema
    """
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}

    normalized = dict(schema)

    # 处理 type 数组形式的可空类型
    raw_type = normalized.get("type")
    if isinstance(raw_type, list):
        non_null = [item for item in raw_type if item != "null"]
        if "null" in raw_type and len(non_null) == 1:
            normalized["type"] = non_null[0]
            normalized["nullable"] = True

    # 处理 oneOf/anyOf 形式的可空类型
    for key in ("oneOf", "anyOf"):
        nullable_branch = _extract_nullable_branch(normalized.get(key))
        if nullable_branch is not None:
            branch, _ = nullable_branch
            merged = {k: v for k, v in normalized.items() if k != key}
            merged.update(branch)
            normalized = merged
            normalized["nullable"] = True
            break

    # 递归处理嵌套的 properties
    if "properties" in normalized and isinstance(normalized["properties"], dict):
        normalized["properties"] = {
            name: _normalize_schema_for_openai(prop)
            if isinstance(prop, dict)
            else prop
            for name, prop in normalized["properties"].items()
        }

    # 递归处理嵌套的 items
    if "items" in normalized and isinstance(normalized["items"], dict):
        normalized["items"] = _normalize_schema_for_openai(normalized["items"])

    if normalized.get("type") != "object":
        return normalized

    normalized.setdefault("properties", {})
    normalized.setdefault("required", [])
    return normalized


class MCPToolWrapper(Tool):
    """
    MCP 服务器工具的包装器，将单个 MCP 工具包装为 nanobot 工具。

    该类实现了 Tool 接口，将 MCP 工具调用委托给 MCP 会话。

    Attributes:
        _session: MCP 客户端会话
        _original_name: MCP 工具的原始名称
        _name: 包装后的工具名称（添加 mcp_ 前缀）
        _description: 工具描述
        _parameters: 规范化后的参数模式
        _tool_timeout: 工具调用超时时间
    """

    def __init__(self, session, server_name: str, tool_def, tool_timeout: int = 30):
        """
        初始化 MCP 工具包装器。

        Args:
            session: MCP 客户端会话
            server_name: MCP 服务器名称
            tool_def: MCP 工具定义
            tool_timeout: 工具调用超时时间（秒）
        """
        self._session = session
        self._original_name = tool_def.name
        # 添加 mcp_ 前缀避免命名冲突
        self._name = f"mcp_{server_name}_{tool_def.name}"
        self._description = tool_def.description or tool_def.name
        raw_schema = tool_def.inputSchema or {"type": "object", "properties": {}}
        self._parameters = _normalize_schema_for_openai(raw_schema)
        self._tool_timeout = tool_timeout

    @property
    def name(self) -> str:
        """工具名称。"""
        return self._name

    @property
    def description(self) -> str:
        """工具描述。"""
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        """工具参数的 JSON Schema。"""
        return self._parameters

    async def execute(self, **kwargs: Any) -> str:
        """
        执行 MCP 工具调用。

        Args:
            **kwargs: 工具参数

        Returns:
            工具执行结果
        """
        from mcp import types

        try:
            result = await asyncio.wait_for(
                self._session.call_tool(self._original_name, arguments=kwargs),
                timeout=self._tool_timeout,
            )
        except asyncio.TimeoutError:
            logger.warning("MCP tool '{}' timed out after {}s", self._name, self._tool_timeout)
            return f"(MCP tool call timed out after {self._tool_timeout}s)"
        except asyncio.CancelledError:
            # MCP SDK 的 anyio 取消作用域可能在超时/失败时泄漏 CancelledError
            # 只有当我们的任务被外部取消时才重新抛出（例如 /stop）
            task = asyncio.current_task()
            if task is not None and task.cancelling() > 0:
                raise
            logger.warning("MCP tool '{}' was cancelled by server/SDK", self._name)
            return "(MCP tool call was cancelled)"
        except Exception as exc:
            logger.exception(
                "MCP tool '{}' failed: {}: {}",
                self._name,
                type(exc).__name__,
                exc,
            )
            return f"(MCP tool call failed: {type(exc).__name__})"

        # 提取文本内容
        parts = []
        for block in result.content:
            if isinstance(block, types.TextContent):
                parts.append(block.text)
            else:
                parts.append(str(block))
        return "\n".join(parts) or "(no output)"


async def connect_mcp_servers(
    mcp_servers: dict, registry: ToolRegistry, stack: AsyncExitStack
) -> None:
    """
    连接配置的 MCP 服务器并注册其工具。

    该函数会遍历所有配置的 MCP 服务器，建立连接，
    并将服务器的工具注册到 nanobot 的工具注册表。

    支持的传输类型：
    - stdio: 通过命令行启动本地进程
    - sse: Server-Sent Events (HTTP)
    - streamableHttp: HTTP 流

    Args:
        mcp_servers: MCP 服务器配置字典
        registry: 工具注册表
        stack: 异步退出栈（用于资源管理）
    """
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.sse import sse_client
    from mcp.client.stdio import stdio_client
    from mcp.client.streamable_http import streamable_http_client

    for name, cfg in mcp_servers.items():
        try:
            # 确定传输类型
            transport_type = cfg.type
            if not transport_type:
                if cfg.command:
                    transport_type = "stdio"
                elif cfg.url:
                    # 约定：以 /sse 结尾的 URL 使用 SSE 传输，其他使用 streamableHttp
                    transport_type = (
                        "sse" if cfg.url.rstrip("/").endswith("/sse") else "streamableHttp"
                    )
                else:
                    logger.warning("MCP server '{}': no command or url configured, skipping", name)
                    continue

            if transport_type == "stdio":
                # stdio 传输：启动本地进程
                params = StdioServerParameters(
                    command=cfg.command, args=cfg.args, env=cfg.env or None
                )
                read, write = await stack.enter_async_context(stdio_client(params))
            elif transport_type == "sse":
                # SSE 传输：HTTP Server-Sent Events
                def httpx_client_factory(
                    headers: dict[str, str] | None = None,
                    timeout: httpx.Timeout | None = None,
                    auth: httpx.Auth | None = None,
                ) -> httpx.AsyncClient:
                    merged_headers = {
                        "Accept": "application/json, text/event-stream",
                        **(cfg.headers or {}),
                        **(headers or {}),
                    }
                    return httpx.AsyncClient(
                        headers=merged_headers or None,
                        follow_redirects=True,
                        timeout=timeout,
                        auth=auth,
                    )

                read, write = await stack.enter_async_context(
                    sse_client(cfg.url, httpx_client_factory=httpx_client_factory)
                )
            elif transport_type == "streamableHttp":
                # streamableHttp 传输：HTTP 流
                # 始终提供显式的 httpx 客户端，避免 MCP HTTP 传输继承 httpx 的默认 5 秒超时
                http_client = await stack.enter_async_context(
                    httpx.AsyncClient(
                        headers=cfg.headers or None,
                        follow_redirects=True,
                        timeout=None,
                    )
                )
                read, write, _ = await stack.enter_async_context(
                    streamable_http_client(cfg.url, http_client=http_client)
                )
            else:
                logger.warning("MCP server '{}': unknown transport type '{}'", name, transport_type)
                continue

            # 创建 MCP 会话并初始化
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()

            # 获取服务器提供的工具列表
            tools = await session.list_tools()
            enabled_tools = set(cfg.enabled_tools)
            allow_all_tools = "*" in enabled_tools
            registered_count = 0
            matched_enabled_tools: set[str] = set()
            available_raw_names = [tool_def.name for tool_def in tools.tools]
            available_wrapped_names = [f"mcp_{name}_{tool_def.name}" for tool_def in tools.tools]

            # 注册工具
            for tool_def in tools.tools:
                wrapped_name = f"mcp_{name}_{tool_def.name}"
                if (
                    not allow_all_tools
                    and tool_def.name not in enabled_tools
                    and wrapped_name not in enabled_tools
                ):
                    logger.debug(
                        "MCP: skipping tool '{}' from server '{}' (not in enabledTools)",
                        wrapped_name,
                        name,
                    )
                    continue
                wrapper = MCPToolWrapper(session, name, tool_def, tool_timeout=cfg.tool_timeout)
                registry.register(wrapper)
                logger.debug("MCP: registered tool '{}' from server '{}'", wrapper.name, name)
                registered_count += 1
                if enabled_tools:
                    if tool_def.name in enabled_tools:
                        matched_enabled_tools.add(tool_def.name)
                    if wrapped_name in enabled_tools:
                        matched_enabled_tools.add(wrapped_name)

            # 检查是否有未匹配的 enabledTools
            if enabled_tools and not allow_all_tools:
                unmatched_enabled_tools = sorted(enabled_tools - matched_enabled_tools)
                if unmatched_enabled_tools:
                    logger.warning(
                        "MCP server '{}': enabledTools entries not found: {}. Available raw names: {}. "
                        "Available wrapped names: {}",
                        name,
                        ", ".join(unmatched_enabled_tools),
                        ", ".join(available_raw_names) or "(none)",
                        ", ".join(available_wrapped_names) or "(none)",
                    )

            logger.info("MCP server '{}': connected, {} tools registered", name, registered_count)
        except Exception as e:
            logger.error("MCP server '{}': failed to connect: {}", name, e)
