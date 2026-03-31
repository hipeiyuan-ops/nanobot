"""
聊天频道模块，提供多平台消息收发能力。

该模块实现了 nanobot 与各种聊天平台（如 Telegram、Discord、Slack、微信等）的集成。
采用插件化架构设计，支持内置频道和外部插件频道的自动发现与注册。

主要组件：
    - BaseChannel: 频道基类，定义了所有频道必须实现的接口
    - ChannelManager: 频道管理器，负责协调多个频道的消息路由

架构说明：
    所有频道实现都继承自 BaseChannel，通过消息总线（MessageBus）与 Agent 通信。
    频道可以独立启动和停止，支持热插拔。
"""

from nanobot.channels.base import BaseChannel
from nanobot.channels.manager import ChannelManager

__all__ = ["BaseChannel", "ChannelManager"]
