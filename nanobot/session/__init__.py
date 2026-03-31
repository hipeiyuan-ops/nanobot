"""
会话管理模块。

该模块提供了对话会话的持久化管理功能：
    - Session: 会话数据类，存储消息历史和元数据
    - SessionManager: 会话管理器，处理会话的加载、保存和缓存

会话存储格式：
    - 使用 JSONL 格式存储（每行一个 JSON 对象）
    - 第一行为元数据，后续行为消息记录
    - 支持从旧版路径迁移
"""

from nanobot.session.manager import Session, SessionManager

__all__ = ["SessionManager", "Session"]
