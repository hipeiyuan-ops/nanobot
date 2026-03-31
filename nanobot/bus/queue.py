"""
异步消息队列：用于解耦通道和代理之间的通信

这个模块实现了 MessageBus 类，它是一个异步消息总线，用于在聊天通道
和代理核心之间传递消息。通过使用异步队列，MessageBus 实现了通道和代理
之间的解耦，使得它们可以独立运行和扩展。

工作流程：
1. 通道（如 Telegram、Discord）接收到用户消息
2. 通道将消息封装为 InboundMessage，通过 publish_inbound 发送到入站队列
3. 代理从入站队列消费消息（consume_inbound）
4. 代理处理消息并生成响应
5. 代理将响应封装为 OutboundMessage，通过 publish_outbound 发送到出站队列
6. 通道从出站队列消费响应（consume_outbound）
7. 通道将响应发送给用户

这种设计使得：
- 通道和代理可以独立开发和测试
- 可以轻松添加新的通道而不影响代理核心
- 支持多个通道同时工作
- 提供了良好的扩展性和可维护性
"""

import asyncio

from nanobot.bus.events import InboundMessage, OutboundMessage


class MessageBus:
    """
    异步消息总线：解耦聊天通道与代理核心
    
    MessageBus 是 nanobot 通信架构的核心组件，它使用两个异步队列来管理
    消息的流动：
    
    1. 入站队列（inbound）：存储从通道接收的消息
       - 通道通过 publish_inbound() 发布消息
       - 代理通过 consume_inbound() 消费消息
    
    2. 出站队列（outbound）：存储代理生成的响应
       - 代理通过 publish_outbound() 发布响应
       - 通道通过 consume_outbound() 消费响应
    
    使用异步队列的优势：
    - 非阻塞操作：生产者和消费者可以独立运行
    - 自动缓冲：队列会缓冲消息，处理速度不匹配时不会丢失消息
    - 线程安全：asyncio.Queue 是线程安全的
    
    示例：
        >>> bus = MessageBus()
        >>> 
        >>> # 通道发布消息
        >>> await bus.publish_inbound(InboundMessage(
        ...     channel="telegram",
        ...     sender_id="user123",
        ...     chat_id="chat456",
        ...     content="你好！"
        ... ))
        >>> 
        >>> # 代理消费消息
        >>> msg = await bus.consume_inbound()
        >>> 
        >>> # 代理发布响应
        >>> await bus.publish_outbound(OutboundMessage(
        ...     channel="telegram",
        ...     chat_id="chat456",
        ...     content="你好！有什么可以帮助你的吗？"
        ... ))
        >>> 
        >>> # 通道消费响应
        >>> response = await bus.consume_outbound()
    """

    def __init__(self):
        """
        初始化消息总线
        
        创建两个异步队列：
        - inbound: 入站消息队列，存储从通道接收的消息
        - outbound: 出站消息队列，存储代理生成的响应
        """
        self.inbound: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self.outbound: asyncio.Queue[OutboundMessage] = asyncio.Queue()

    async def publish_inbound(self, msg: InboundMessage) -> None:
        """
        发布入站消息：从通道向代理发送消息
        
        通道调用此方法将接收到的用户消息发送到入站队列。
        这是一个异步操作，如果队列已满，会等待直到有空间可用。
        
        Args:
            msg: 入站消息对象，包含通道、发送者、内容等信息
        
        示例：
            >>> await bus.publish_inbound(InboundMessage(
            ...     channel="telegram",
            ...     sender_id="user123",
            ...     chat_id="chat456",
            ...     content="你好！"
            ... ))
        """
        await self.inbound.put(msg)

    async def consume_inbound(self) -> InboundMessage:
        """
        消费入站消息：代理从此方法获取待处理的消息
        
        代理调用此方法从入站队列获取消息进行处理。
        这是一个阻塞操作，如果队列为空，会等待直到有消息可用。
        
        Returns:
            InboundMessage: 入站消息对象
        
        示例：
            >>> msg = await bus.consume_inbound()
            >>> print(f"收到来自 {msg.channel} 的消息：{msg.content}")
        """
        return await self.inbound.get()

    async def publish_outbound(self, msg: OutboundMessage) -> None:
        """
        发布出站消息：从代理向通道发送响应
        
        代理调用此方法将处理后的响应发送到出站队列。
        这是一个异步操作，如果队列已满，会等待直到有空间可用。
        
        Args:
            msg: 出站消息对象，包含目标通道、聊天ID、响应内容等信息
        
        示例：
            >>> await bus.publish_outbound(OutboundMessage(
            ...     channel="telegram",
            ...     chat_id="chat456",
            ...     content="处理完成！"
            ... ))
        """
        await self.outbound.put(msg)

    async def consume_outbound(self) -> OutboundMessage:
        """
        消费出站消息：通道从此方法获取待发送的响应
        
        通道调用此方法从出站队列获取响应并发送给用户。
        这是一个阻塞操作，如果队列为空，会等待直到有响应可用。
        
        Returns:
            OutboundMessage: 出站消息对象
        
        示例：
            >>> response = await bus.consume_outbound()
            >>> print(f"向 {response.channel} 发送响应：{response.content}")
        """
        return await self.outbound.get()

    @property
    def inbound_size(self) -> int:
        """
        获取入站队列中待处理消息的数量
        
        这个属性可以用于监控队列状态，例如检测是否有消息积压。
        
        Returns:
            int: 入站队列中的消息数量
        
        示例：
            >>> if bus.inbound_size > 10:
            ...     print("警告：入站消息积压！")
        """
        return self.inbound.qsize()

    @property
    def outbound_size(self) -> int:
        """
        获取出站队列中待发送响应的数量
        
        这个属性可以用于监控队列状态，例如检测是否有响应积压。
        
        Returns:
            int: 出站队列中的响应数量
        
        示例：
            >>> if bus.outbound_size > 10:
            ...     print("警告：出站响应积压！")
        """
        return self.outbound.qsize()
