"""
OpenAI 兼容的 HTTP API 模块。

该模块提供了与 OpenAI API 兼容的 HTTP 接口：
    - POST /v1/chat/completions: 聊天补全接口
    - GET /v1/models: 模型列表接口
    - GET /health: 健康检查接口

所有请求路由到单个持久化的 API 会话。

使用场景：
    - 作为 OpenAI API 的本地替代
    - 与支持 OpenAI API 的工具集成
    - 提供标准化的 HTTP 接口
"""

from nanobot.api.server import create_app

__all__ = ["create_app"]
