"""
模型信息助手模块，用于配置向导。

模型数据库/自动完成功能在 litellm 被替换期间暂时禁用。
所有公共函数签名保留，以便调用者无需更改即可继续工作。

注意：
    当前所有函数返回空值或 None。待新的模型信息源集成后，
    这些函数将恢复完整功能。
"""

from __future__ import annotations

from typing import Any


def get_all_models() -> list[str]:
    """
    获取所有可用模型列表。

    当前返回空列表，待模型数据库集成后恢复功能。

    返回：
        模型名称列表
    """
    return []


def find_model_info(model_name: str) -> dict[str, Any] | None:
    """
    查找指定模型的详细信息。

    当前返回 None，待模型数据库集成后恢复功能。

    参数：
        model_name: 模型名称

    返回：
        模型信息字典，未找到返回 None
    """
    return None


def get_model_context_limit(model: str, provider: str = "auto") -> int | None:
    """
    获取模型的上下文窗口限制（token 数量）。

    当前返回 None，待模型数据库集成后恢复功能。

    参数：
        model: 模型名称
        provider: 提供商名称（"auto" 表示自动检测）

    返回：
        上下文窗口 token 数量，未知返回 None
    """
    return None


def get_model_suggestions(partial: str, provider: str = "auto", limit: int = 20) -> list[str]:
    """
    根据部分输入获取模型名称建议（用于自动完成）。

    当前返回空列表，待模型数据库集成后恢复功能。

    参数：
        partial: 部分模型名称输入
        provider: 提供商名称（"auto" 表示自动检测）
        limit: 最大返回数量

    返回：
        匹配的模型名称列表
    """
    return []


def format_token_count(tokens: int) -> str:
    """
    格式化 token 数量用于显示（如 200000 -> '200,000'）。

    参数：
        tokens: token 数量

    返回：
        格式化后的字符串
    """
    return f"{tokens:,}"
