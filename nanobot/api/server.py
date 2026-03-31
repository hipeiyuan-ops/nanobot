"""
OpenAI 兼容的 HTTP API 服务器，用于固定的 nanobot 会话。

提供以下端点：
    - POST /v1/chat/completions: 聊天补全接口
    - GET /v1/models: 模型列表接口
    - GET /health: 健康检查接口

所有请求路由到单个持久化的 API 会话。

特性：
    - OpenAI API 兼容格式
    - 支持多会话（通过 session_id 参数）
    - 请求超时控制
    - 空响应自动重试

限制：
    - 当前不支持流式响应（stream=true）
    - 仅支持单条用户消息
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from aiohttp import web
from loguru import logger

API_SESSION_KEY = "api:default"
API_CHAT_ID = "default"


def _error_json(status: int, message: str, err_type: str = "invalid_request_error") -> web.Response:
    """
    构建错误响应 JSON。

    参数：
        status: HTTP 状态码
        message: 错误消息
        err_type: 错误类型

    返回：
        JSON 格式的错误响应
    """
    return web.json_response(
        {"error": {"message": message, "type": err_type, "code": status}},
        status=status,
    )


def _chat_completion_response(content: str, model: str) -> dict[str, Any]:
    """
    构建聊天补全响应。

    参数：
        content: 响应内容
        model: 模型名称

    返回：
        OpenAI 格式的响应字典
    """
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _response_text(value: Any) -> str:
    """
    将 process_direct 输出标准化为纯文本。

    参数：
        value: process_direct 返回值

    返回：
        纯文本字符串
    """
    if value is None:
        return ""
    if hasattr(value, "content"):
        return str(getattr(value, "content") or "")
    return str(value)


async def handle_chat_completions(request: web.Request) -> web.Response:
    """
    处理 POST /v1/chat/completions 请求。

    参数：
        request: aiohttp 请求对象

    返回：
        OpenAI 格式的聊天补全响应

    处理流程：
        1. 解析请求体
        2. 验证消息格式
        3. 调用 Agent 处理
        4. 返回响应

    限制：
        - 仅支持单条用户消息
        - 不支持流式响应
    """
    try:
        body = await request.json()
    except Exception:
        return _error_json(400, "Invalid JSON body")

    messages = body.get("messages")
    if not isinstance(messages, list) or len(messages) != 1:
        return _error_json(400, "Only a single user message is supported")

    if body.get("stream", False):
        return _error_json(400, "stream=true is not supported yet. Set stream=false or omit it.")

    message = messages[0]
    if not isinstance(message, dict) or message.get("role") != "user":
        return _error_json(400, "Only a single user message is supported")
    user_content = message.get("content", "")
    if isinstance(user_content, list):
        user_content = " ".join(
            part.get("text", "") for part in user_content if part.get("type") == "text"
        )

    agent_loop = request.app["agent_loop"]
    timeout_s: float = request.app.get("request_timeout", 120.0)
    model_name: str = request.app.get("model_name", "nanobot")
    if (requested_model := body.get("model")) and requested_model != model_name:
        return _error_json(400, f"Only configured model '{model_name}' is available")

    session_key = f"api:{body['session_id']}" if body.get("session_id") else API_SESSION_KEY
    session_locks: dict[str, asyncio.Lock] = request.app["session_locks"]
    session_lock = session_locks.setdefault(session_key, asyncio.Lock())

    logger.info("API request session_key={} content={}", session_key, user_content[:80])

    _FALLBACK = "I've completed processing but have no response to give."

    try:
        async with session_lock:
            try:
                response = await asyncio.wait_for(
                    agent_loop.process_direct(
                        content=user_content,
                        session_key=session_key,
                        channel="api",
                        chat_id=API_CHAT_ID,
                    ),
                    timeout=timeout_s,
                )
                response_text = _response_text(response)

                if not response_text or not response_text.strip():
                    logger.warning(
                        "Empty response for session {}, retrying",
                        session_key,
                    )
                    retry_response = await asyncio.wait_for(
                        agent_loop.process_direct(
                            content=user_content,
                            session_key=session_key,
                            channel="api",
                            chat_id=API_CHAT_ID,
                        ),
                        timeout=timeout_s,
                    )
                    response_text = _response_text(retry_response)
                    if not response_text or not response_text.strip():
                        logger.warning(
                            "Empty response after retry for session {}, using fallback",
                            session_key,
                        )
                        response_text = _FALLBACK

            except asyncio.TimeoutError:
                return _error_json(504, f"Request timed out after {timeout_s}s")
            except Exception:
                logger.exception("Error processing request for session {}", session_key)
                return _error_json(500, "Internal server error", err_type="server_error")
    except Exception:
        logger.exception("Unexpected API lock error for session {}", session_key)
        return _error_json(500, "Internal server error", err_type="server_error")

    return web.json_response(_chat_completion_response(response_text, model_name))


async def handle_models(request: web.Request) -> web.Response:
    """
    处理 GET /v1/models 请求。

    参数：
        request: aiohttp 请求对象

    返回：
        可用模型列表
    """
    model_name = request.app.get("model_name", "nanobot")
    return web.json_response({
        "object": "list",
        "data": [
            {
                "id": model_name,
                "object": "model",
                "created": 0,
                "owned_by": "nanobot",
            }
        ],
    })


async def handle_health(request: web.Request) -> web.Response:
    """
    处理 GET /health 请求。

    参数：
        request: aiohttp 请求对象

    返回：
        健康状态 JSON
    """
    return web.json_response({"status": "ok"})


def create_app(agent_loop, model_name: str = "nanobot", request_timeout: float = 120.0) -> web.Application:
    """
    创建 aiohttp 应用实例。

    参数：
        agent_loop: 已初始化的 AgentLoop 实例
        model_name: 响应中报告的模型名称
        request_timeout: 每个请求的超时时间（秒）

    返回：
        配置好的 aiohttp Application 实例

    注册的路由：
        - POST /v1/chat/completions
        - GET /v1/models
        - GET /health
    """
    app = web.Application()
    app["agent_loop"] = agent_loop
    app["model_name"] = model_name
    app["request_timeout"] = request_timeout
    app["session_locks"] = {}

    app.router.add_post("/v1/chat/completions", handle_chat_completions)
    app.router.add_get("/v1/models", handle_models)
    app.router.add_get("/health", handle_health)
    return app
