"""
CLI 模块，提供命令行界面功能。

该模块包含 nanobot 的命令行工具实现，支持以下功能：
- 交互式配置向导（onboard）
- 启动服务（serve）
- 启动网关模式（gateway）
- 启动 Agent 模式（agent）
- 显示频道和插件信息

主要组件：
    - commands: CLI 命令定义和实现
    - onboard: 交互式配置向导
    - stream: CLI 流式渲染器
    - models: 模型信息助手
"""
