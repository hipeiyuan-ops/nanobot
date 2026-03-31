"""
Agent 工具模块。

该模块提供了 agent 可用的工具系统，包括：
- Tool: 工具基类，定义了工具的接口
- ToolRegistry: 工具注册表，管理工具的注册和执行

工具是 agent 与外部环境交互的主要方式，包括：
- 文件系统操作（读写文件、列出目录）
- Shell 命令执行
- Web 搜索和获取
- 消息发送
- 定时任务
- 子代理生成
- MCP（Model Context Protocol）工具集成
"""

from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.registry import ToolRegistry

__all__ = ["Tool", "ToolRegistry"]
