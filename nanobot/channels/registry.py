"""
频道自动发现模块，支持内置频道和外部插件。

该模块提供了频道类的自动发现和加载机制：
- 通过 pkgutil 扫描内置频道模块
- 通过 entry_points 加载外部插件频道
- 内置频道优先级高于外部插件

主要功能：
    - discover_channel_names: 发现所有内置频道模块名称
    - load_channel_class: 加载指定模块的频道类
    - discover_plugins: 发现外部插件频道
    - discover_all: 合并内置和外部频道

使用方式：
    - 内置频道：在 nanobot.channels 包下创建模块，定义 BaseChannel 子类
    - 外部插件：在 setup.py/pyproject.toml 中注册 nanobot.channels 入口点
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from nanobot.channels.base import BaseChannel

_INTERNAL = frozenset({"base", "manager", "registry"})


def discover_channel_names() -> list[str]:
    """
    通过扫描包发现所有内置频道模块名称（零导入）。

    遍历 nanobot.channels 包下的所有模块，
    排除内部模块（base、manager、registry）和子包。

    返回：
        内置频道模块名称列表
    """
    import nanobot.channels as pkg

    return [
        name
        for _, name, ispkg in pkgutil.iter_modules(pkg.__path__)
        if name not in _INTERNAL and not ispkg
    ]


def load_channel_class(module_name: str) -> type[BaseChannel]:
    """
    导入指定模块并返回找到的第一个 BaseChannel 子类。

    参数：
        module_name: 模块名称（如 "telegram"、"discord"）

    返回：
        频道类

    异常：
        ImportError: 模块中没有 BaseChannel 子类
    """
    from nanobot.channels.base import BaseChannel as _Base

    mod = importlib.import_module(f"nanobot.channels.{module_name}")
    for attr in dir(mod):
        obj = getattr(mod, attr)
        if isinstance(obj, type) and issubclass(obj, _Base) and obj is not _Base:
            return obj
    raise ImportError(f"No BaseChannel subclass in nanobot.channels.{module_name}")


def discover_plugins() -> dict[str, type[BaseChannel]]:
    """
    发现通过 entry_points 注册的外部频道插件。

    扫描 "nanobot.channels" 入口点组中的所有入口点，
    加载对应的频道类。

    返回：
        插件频道字典 {名称: 频道类}
    """
    from importlib.metadata import entry_points

    plugins: dict[str, type[BaseChannel]] = {}
    for ep in entry_points(group="nanobot.channels"):
        try:
            cls = ep.load()
            plugins[ep.name] = cls
        except Exception as e:
            logger.warning("Failed to load channel plugin '{}': {}", ep.name, e)
    return plugins


def discover_all() -> dict[str, type[BaseChannel]]:
    """
    返回所有频道：内置（pkgutil）合并外部（entry_points）。

    内置频道优先——外部插件无法覆盖内置频道名称。

    发现流程：
        1. 扫描内置频道模块
        2. 加载外部插件频道
        3. 检测并警告被覆盖的插件

    返回：
        所有频道字典 {名称: 频道类}
    """
    builtin: dict[str, type[BaseChannel]] = {}
    for modname in discover_channel_names():
        try:
            builtin[modname] = load_channel_class(modname)
        except ImportError as e:
            logger.debug("Skipping built-in channel '{}': {}", modname, e)

    external = discover_plugins()
    shadowed = set(external) & set(builtin)
    if shadowed:
        logger.warning("Plugin(s) shadowed by built-in channels (ignored): {}", shadowed)

    return {**external, **builtin}
