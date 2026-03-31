"""
消息总线模块：用于解耦通道和代理之间的通信

这个模块提供了消息总线的核心功能，实现了通道（如 Telegram、Discord 等）
与代理核心之间的解耦通信。通过消息总线，通道可以将消息发送给代理，
代理也可以将响应发送回通道，而不需要直接耦合。

主要组件：
- MessageBus: 消息总线，管理入站和出站消息队列
- InboundMessage: 入站消息，从通道接收的消息
- OutboundMessage: 出站消息，发送到通道的响应
"""

from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus

__all__ = ["MessageBus", "InboundMessage", "OutboundMessage"]
