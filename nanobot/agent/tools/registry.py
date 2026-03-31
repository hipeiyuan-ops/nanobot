"""
工具注册表模块，管理工具的注册、查找和执行。

该模块提供了工具系统的核心管理功能：
- ToolRegistry: 工具注册表，管理工具的生命周期
- 工具注册和查找
- 参数类型转换和验证
- 工具执行和错误处理

注册表是 agent 与工具交互的中心枢纽。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from loguru import logger

from nanobot.agent.tools.base import Tool


class ToolRegistry:
    """
    工具注册表，管理工具的注册、查找和执行。

    该类是工具系统的核心，负责：
    - 注册工具实例
    - 生成 OpenAI 格式的工具定义列表
    - 根据名称查找工具
    - 执行工具调用并处理结果

    工具注册表支持：
    - 动态注册和注销工具
    - 工具名称唯一性检查
    - 参数类型转换
    - 异步工具执行

    Attributes:
        _tools: 工具名称到工具实例的映射
    """

    def __init__(self) -> None:
        """初始化工具注册表。"""
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """
        注册工具。

        如果同名工具已存在，会发出警告并覆盖。

        Args:
            tool: 要注册的工具实例
        """
        if tool.name in self._tools:
            logger.warning("Overwriting existing tool: {}", tool.name)
        self._tools[tool.name] = tool
        logger.debug("Registered tool: {}", tool.name)

    def unregister(self, name: str) -> bool:
        """
        注销工具。

        Args:
            name: 工具名称

        Returns:
            是否成功注销
        """
        if name in self._tools:
            del self._tools[name]
            return True
        return False

    def get(self, name: str) -> Tool | None:
        """
        根据名称获取工具。

        Args:
            name: 工具名称

        Returns:
            工具实例，如果不存在则返回 None
        """
        return self._tools.get(name)

    def list_tools(self) -> list[str]:
        """
        列出所有已注册的工具名称。

        Returns:
            工具名称列表
        """
        return list(self._tools.keys())

    def get_definitions(self) -> list[dict[str, Any]]:
        """
        获取所有工具的 OpenAI 格式定义。

        该方法用于生成 LLM 函数调用 API 所需的工具定义列表。

        Returns:
            OpenAI 格式的工具定义列表
        """
        return [tool.to_schema() for tool in self._tools.values()]

    async def execute(self, name: str, arguments: Any) -> Any:
        """
        执行工具调用。

        该方法处理完整的工具执行流程：
        1. 查找工具
        2. 解析参数（如果需要）
        3. 类型转换
        4. 参数验证
        5. 执行工具
        6. 格式化结果

        Args:
            name: 工具名称
            arguments: 工具参数（字符串或字典）

        Returns:
            工具执行结果（字符串或内容块列表）
        """
        tool = self._tools.get(name)
        if tool is None:
            return f"Error: Unknown tool '{name}'"

        # 解析参数
        params: dict[str, Any] = {}
        if isinstance(arguments, str):
            try:
                params = json.loads(arguments) if arguments.strip() else {}
            except json.JSONDecodeError:
                return f"Error: Invalid JSON arguments for tool '{name}'"
        elif isinstance(arguments, dict):
            params = arguments
        elif arguments is not None:
            return f"Error: Arguments must be a JSON string or object for tool '{name}'"

        # 类型转换
        try:
            params = tool.cast_params(params)
        except Exception as e:
            logger.warning("Tool '{}' param cast failed: {}", name, e)

        # 参数验证
        errors = tool.validate_params(params)
        if errors:
            return f"Error: Invalid parameters for tool '{name}': {'; '.join(errors)}"

        # 执行工具
        try:
            result = await tool.execute(**params)
        except asyncio.CancelledError:
            # 取消错误需要向上传播
            raise
        except Exception as e:
            logger.exception("Tool '{}' failed", name)
            return f"Error: Tool '{name}' failed: {type(e).__name__}: {e}"

        # 格式化结果
        if result is None:
            return "Tool completed successfully (no output)"
        if isinstance(result, str):
            return result
        if isinstance(result, list):
            # 内容块列表（如图片）
            return result
        # 其他类型转换为字符串
        return str(result)

    def __contains__(self, name: str) -> bool:
        """检查工具是否已注册。"""
        return name in self._tools

    def __len__(self) -> int:
        """返回已注册的工具数量。"""
        return len(self._tools)
