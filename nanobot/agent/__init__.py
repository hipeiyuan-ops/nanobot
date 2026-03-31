"""
Agent 核心模块。

该模块是 nanobot agent 系统的入口点，导出了所有核心组件供外部使用。
主要包含以下组件：
- AgentLoop: Agent 主循环，负责协调 LLM 调用和工具执行
- AgentHook: 生命周期钩子，用于在 agent 执行过程中注入自定义逻辑
- AgentHookContext: 钩子上下文，包含每次迭代的可变状态
- CompositeHook: 组合钩子，将多个钩子串联执行
- ContextBuilder: 上下文构建器，负责组装 agent 的系统提示和消息
- MemoryStore: 内存存储，管理长期记忆和历史记录
- SkillsLoader: 技能加载器，动态加载 agent 可用的技能
- SubagentManager: 子代理管理器，负责后台任务的执行
"""

from nanobot.agent.context import ContextBuilder
from nanobot.agent.hook import AgentHook, AgentHookContext, CompositeHook
from nanobot.agent.loop import AgentLoop
from nanobot.agent.memory import MemoryStore
from nanobot.agent.skills import SkillsLoader
from nanobot.agent.subagent import SubagentManager

__all__ = [
    "AgentHook",
    "AgentHookContext",
    "AgentLoop",
    "CompositeHook",
    "ContextBuilder",
    "MemoryStore",
    "SkillsLoader",
    "SubagentManager",
]
