"""
斜杠命令路由和内置处理器模块。

该模块提供了斜杠命令的路由和注册功能：
    - CommandRouter: 命令路由器，支持优先级、精确匹配和前缀匹配
    - CommandContext: 命令上下文，包含处理器所需的所有信息
    - register_builtin_commands: 注册内置命令的函数

内置命令：
    - /new: 开始新对话
    - /stop: 停止当前任务
    - /restart: 重启机器人
    - /status: 显示状态
    - /help: 显示帮助
"""

from nanobot.command.builtin import register_builtin_commands
from nanobot.command.router import CommandContext, CommandRouter

__all__ = ["CommandContext", "CommandRouter", "register_builtin_commands"]
