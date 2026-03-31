"""
配置模式定义：使用 Pydantic 实现类型安全的配置管理

这个模块定义了 nanobot 的所有配置模式，使用 Pydantic 库提供：
- 类型验证：自动验证配置值的类型
- 默认值：为所有配置项提供合理的默认值
- 序列化/反序列化：支持 JSON 格式的读写
- 别名支持：同时支持驼峰命名和蛇形命名

配置层次结构：
Config (根配置)
├── agents (代理配置)
│   └── defaults (默认代理配置)
├── channels (通道配置)
├── providers (提供商配置)
│   ├── anthropic, openai, openrouter, ... (各提供商)
├── api (API 服务器配置)
├── gateway (网关配置)
│   └── heartbeat (心跳配置)
└── tools (工具配置)
    ├── web (网络工具配置)
    │   └── search (搜索配置)
    ├── exec (执行工具配置)
    └── mcp_servers (MCP 服务器配置)
"""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from pydantic_settings import BaseSettings


class Base(BaseModel):
    """
    基础模型：支持驼峰命名和蛇形命名的键名
    
    这个基类为所有配置模型提供统一的配置：
    - alias_generator: 自动将蛇形命名转换为驼峰命名（用于 JSON 序列化）
    - populate_by_name: 允许使用原始名称（蛇形命名）或别名（驼峰命名）来设置字段
    
    示例：
        JSON 中可以使用 "apiKey" 或 "api_key"，都能正确解析
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ChannelsConfig(Base):
    """
    聊天通道配置
    
    管理所有聊天通道（Telegram、Discord、Slack 等）的全局设置。
    内置通道和插件通道的配置作为额外字段存储（字典形式）。
    每个通道在 __init__ 中解析自己的配置。
    
    流式输出设置：
    - send_progress: 流式传输代理的文本进度到通道
    - send_tool_hints: 流式传输工具调用提示（如 read_file("…")）
    - send_max_retries: 最大发送重试次数
    
    属性：
        send_progress: 是否流式传输代理的文本进度（默认 True）
        send_tool_hints: 是否流式传输工具调用提示（默认 False）
        send_max_retries: 最大发送尝试次数，包括初始发送（默认 3，范围 0-10）
    """

    model_config = ConfigDict(extra="allow")  # 允许额外字段，用于通道特定配置

    send_progress: bool = True  # 流式传输代理的文本进度到通道
    send_tool_hints: bool = False  # 流式传输工具调用提示（如 read_file("…")）
    send_max_retries: int = Field(default=3, ge=0, le=10)  # 最大发送尝试次数（初始发送包含在内）


class AgentDefaults(Base):
    """
    默认代理配置
    
    定义代理的默认行为和参数。这些值可以在运行时被覆盖。
    
    属性：
        workspace: 工作空间目录路径（默认 ~/.nanobot/workspace）
        model: 使用的 AI 模型（默认 anthropic/claude-opus-4-5）
        provider: 提供商名称或 "auto" 自动检测（默认 "auto"）
        max_tokens: 最大生成令牌数（默认 8192）
        context_window_tokens: 上下文窗口大小（默认 65536）
        temperature: 生成温度，控制随机性（默认 0.1）
        max_tool_iterations: 最大工具迭代次数，防止无限循环（默认 40）
        reasoning_effort: 推理努力程度，启用 LLM 思考模式（low/medium/high）
        timezone: IANA 时区名称（默认 UTC）
    """

    workspace: str = "~/.nanobot/workspace"  # 工作空间目录
    model: str = "anthropic/claude-opus-4-5"  # AI 模型
    provider: str = (
        "auto"  # 提供商名称（如 "anthropic", "openrouter"）或 "auto" 自动检测
    )
    max_tokens: int = 8192  # 最大生成令牌数
    context_window_tokens: int = 65_536  # 上下文窗口大小
    temperature: float = 0.1  # 生成温度（0-1，越高越随机）
    max_tool_iterations: int = 40  # 最大工具迭代次数
    reasoning_effort: str | None = None  # low / medium / high - 启用 LLM 思考模式
    timezone: str = "UTC"  # IANA 时区，如 "Asia/Shanghai", "America/New_York"


class AgentsConfig(Base):
    """
    代理配置容器
    
    包含代理的所有配置，目前只有默认配置。
    未来可以扩展支持多个代理配置。
    
    属性：
        defaults: 默认代理配置
    """

    defaults: AgentDefaults = Field(default_factory=AgentDefaults)


class ProviderConfig(Base):
    """
    LLM 提供商配置
    
    每个提供商（如 OpenAI、Anthropic、OpenRouter）都有相同的配置结构。
    
    属性：
        api_key: API 密钥（默认空字符串）
        api_base: API 基础 URL，用于自定义端点（可选）
        extra_headers: 自定义请求头字典（可选）
            例如：AiHubMix 的 APP-Code
    
    示例：
        {
            "apiKey": "sk-xxx",
            "apiBase": "https://api.custom.com/v1",
            "extraHeaders": {"X-Custom": "value"}
        }
    """

    api_key: str = ""  # API 密钥
    api_base: str | None = None  # API 基础 URL（可选）
    extra_headers: dict[str, str] | None = None  # 自定义请求头（如 AiHubMix 的 APP-Code）


class ProvidersConfig(Base):
    """
    LLM 提供商配置集合
    
    包含所有支持的 LLM 提供商的配置。每个提供商都有相同的配置结构。
    
    支持的提供商：
    - custom: 任何 OpenAI 兼容端点
    - azure_openai: Azure OpenAI（model = deployment name）
    - anthropic: Anthropic Claude
    - openai: OpenAI GPT
    - openrouter: OpenRouter 网关
    - deepseek: DeepSeek
    - groq: Groq
    - zhipu: 智谱 GLM
    - dashscope: 阿里云通义千问
    - vllm: vLLM 本地服务器
    - ollama: Ollama 本地模型
    - ovms: OpenVINO Model Server
    - gemini: Google Gemini
    - moonshot: Moonshot/Kimi
    - minimax: MiniMax
    - mistral: Mistral
    - stepfun: 阶跃星辰
    - aihubmix: AiHubMix API 网关
    - siliconflow: 硅基流动
    - volcengine: 火山引擎
    - volcengine_coding_plan: 火山引擎 Coding Plan
    - byteplus: BytePlus（火山引擎国际版）
    - byteplus_coding_plan: BytePlus Coding Plan
    - openai_codex: OpenAI Codex（OAuth）
    - github_copilot: GitHub Copilot（OAuth）
    """

    custom: ProviderConfig = Field(default_factory=ProviderConfig)  # 任何 OpenAI 兼容端点
    azure_openai: ProviderConfig = Field(default_factory=ProviderConfig)  # Azure OpenAI（model = deployment name）
    anthropic: ProviderConfig = Field(default_factory=ProviderConfig)
    openai: ProviderConfig = Field(default_factory=ProviderConfig)
    openrouter: ProviderConfig = Field(default_factory=ProviderConfig)
    deepseek: ProviderConfig = Field(default_factory=ProviderConfig)
    groq: ProviderConfig = Field(default_factory=ProviderConfig)
    zhipu: ProviderConfig = Field(default_factory=ProviderConfig)
    dashscope: ProviderConfig = Field(default_factory=ProviderConfig)
    vllm: ProviderConfig = Field(default_factory=ProviderConfig)
    ollama: ProviderConfig = Field(default_factory=ProviderConfig)  # Ollama 本地模型
    ovms: ProviderConfig = Field(default_factory=ProviderConfig)  # OpenVINO Model Server (OVMS)
    gemini: ProviderConfig = Field(default_factory=ProviderConfig)
    moonshot: ProviderConfig = Field(default_factory=ProviderConfig)
    minimax: ProviderConfig = Field(default_factory=ProviderConfig)
    mistral: ProviderConfig = Field(default_factory=ProviderConfig)
    stepfun: ProviderConfig = Field(default_factory=ProviderConfig)  # Step Fun（阶跃星辰）
    aihubmix: ProviderConfig = Field(default_factory=ProviderConfig)  # AiHubMix API 网关
    siliconflow: ProviderConfig = Field(default_factory=ProviderConfig)  # SiliconFlow（硅基流动）
    volcengine: ProviderConfig = Field(default_factory=ProviderConfig)  # VolcEngine（火山引擎）
    volcengine_coding_plan: ProviderConfig = Field(default_factory=ProviderConfig)  # VolcEngine Coding Plan
    byteplus: ProviderConfig = Field(default_factory=ProviderConfig)  # BytePlus（VolcEngine 国际版）
    byteplus_coding_plan: ProviderConfig = Field(default_factory=ProviderConfig)  # BytePlus Coding Plan
    openai_codex: ProviderConfig = Field(default_factory=ProviderConfig, exclude=True)  # OpenAI Codex（OAuth）
    github_copilot: ProviderConfig = Field(default_factory=ProviderConfig, exclude=True)  # Github Copilot（OAuth）


class HeartbeatConfig(Base):
    """
    心跳服务配置
    
    心跳服务定期执行后台任务，如检查邮件、更新数据等。
    
    属性：
        enabled: 是否启用心跳服务（默认 True）
        interval_s: 心跳间隔时间，秒（默认 1800 = 30 分钟）
        keep_recent_messages: 保留的最近消息数量（默认 8）
    """

    enabled: bool = True  # 是否启用
    interval_s: int = 30 * 60  # 30 分钟
    keep_recent_messages: int = 8  # 保留的最近消息数量


class ApiConfig(Base):
    """
    OpenAI 兼容 API 服务器配置
    
    nanobot 可以作为 OpenAI 兼容的 API 服务器运行，允许其他应用通过
    标准 OpenAI API 接口调用 nanobot。
    
    属性：
        host: 绑定地址（默认 127.0.0.1，仅本地访问，更安全）
        port: 监听端口（默认 8900）
        timeout: 每个请求的超时时间，秒（默认 120.0）
    """

    host: str = "127.0.0.1"  # 更安全的默认值：仅本地绑定
    port: int = 8900
    timeout: float = 120.0  # 每个请求的超时时间（秒）


class GatewayConfig(Base):
    """
    网关/服务器配置
    
    网关是 nanobot 的主要运行模式，负责接收来自各个通道的消息并处理。
    
    属性：
        host: 绑定地址（默认 0.0.0.0，监听所有接口）
        port: 监听端口（默认 18790）
        heartbeat: 心跳服务配置
    """

    host: str = "0.0.0.0"
    port: int = 18790
    heartbeat: HeartbeatConfig = Field(default_factory=HeartbeatConfig)


class WebSearchConfig(Base):
    """
    网络搜索工具配置
    
    配置代理的网络搜索功能，支持多种搜索提供商。
    
    支持的提供商：
    - brave: Brave Search（默认）
    - tavily: Tavily
    - duckduckgo: DuckDuckGo（免费）
    - searxng: SearXNG（自托管）
    - jina: Jina（免费额度）
    
    属性：
        provider: 搜索提供商名称（默认 "brave"）
        api_key: API 密钥
        base_url: SearXNG 基础 URL
        max_results: 最大搜索结果数（默认 5）
    """

    provider: str = "brave"  # brave, tavily, duckduckgo, searxng, jina
    api_key: str = ""
    base_url: str = ""  # SearXNG 基础 URL
    max_results: int = 5


class WebToolsConfig(Base):
    """
    网络工具配置
    
    配置所有网络相关工具的全局设置。
    
    属性：
        proxy: HTTP/SOCKS5 代理 URL
            例如："http://127.0.0.1:7890" 或 "socks5://127.0.0.1:1080"
        search: 网络搜索配置
    """

    proxy: str | None = (
        None  # HTTP/SOCKS5 代理 URL，如 "http://127.0.0.1:7890" 或 "socks5://127.0.0.1:1080"
    )
    search: WebSearchConfig = Field(default_factory=WebSearchConfig)


class ExecToolConfig(Base):
    """
    Shell 执行工具配置
    
    配置代理执行 shell 命令的能力。
    
    属性：
        enable: 是否启用执行工具（默认 True）
        timeout: 命令执行超时时间，秒（默认 60）
        path_append: 要追加到 PATH 的额外目录
    """

    enable: bool = True
    timeout: int = 60
    path_append: str = ""


class MCPServerConfig(Base):
    """
    MCP 服务器连接配置（stdio 或 HTTP）
    
    MCP (Model Context Protocol) 是一种用于连接外部工具服务器的协议。
    支持 stdio（本地进程）和 HTTP/SSE（远程服务器）两种传输模式。
    
    属性：
        type: 传输类型（"stdio", "sse", "streamableHttp"），省略时自动检测
        command: Stdio 模式下要运行的命令（如 "npx"）
        args: Stdio 模式下的命令参数列表
        env: Stdio 模式下的额外环境变量
        url: HTTP/SSE 模式下的端点 URL
        headers: HTTP/SSE 模式下的自定义请求头
        tool_timeout: 工具调用超时时间，秒（默认 30）
        enabled_tools: 只注册这些工具
            - 接受原始 MCP 名称或包装后的 mcp_<server>_<tool> 名称
            - ["*"] = 所有工具
            - [] = 无工具
    
    示例：
        # Stdio 模式（本地）
        {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/dir"]
        }
        
        # HTTP 模式（远程）
        {
            "url": "https://mcp.example.com/sse",
            "headers": {"Authorization": "Bearer xxx"}
        }
    """

    type: Literal["stdio", "sse", "streamableHttp"] | None = None  # 省略时自动检测
    command: str = ""  # Stdio: 要运行的命令（如 "npx"）
    args: list[str] = Field(default_factory=list)  # Stdio: 命令参数
    env: dict[str, str] = Field(default_factory=dict)  # Stdio: 额外环境变量
    url: str = ""  # HTTP/SSE: 端点 URL
    headers: dict[str, str] = Field(default_factory=dict)  # HTTP/SSE: 自定义请求头
    tool_timeout: int = 30  # 工具调用超时时间（秒）
    enabled_tools: list[str] = Field(default_factory=lambda: ["*"])  # 只注册这些工具


class ToolsConfig(Base):
    """
    工具配置
    
    配置代理可用的所有工具。
    
    属性：
        web: 网络工具配置
        exec: Shell 执行工具配置
        restrict_to_workspace: 是否限制所有工具只能访问工作空间目录（默认 False）
        mcp_servers: MCP 服务器配置字典
    """

    web: WebToolsConfig = Field(default_factory=WebToolsConfig)
    exec: ExecToolConfig = Field(default_factory=ExecToolConfig)
    restrict_to_workspace: bool = False  # 如果为 True，限制所有工具只能访问工作空间目录
    mcp_servers: dict[str, MCPServerConfig] = Field(default_factory=dict)


class Config(BaseSettings):
    """
    nanobot 根配置
    
    这是配置的根类，包含所有子配置。使用 pydantic-settings 支持
    从环境变量加载配置（前缀 NANOBOT_，嵌套分隔符 __）。
    
    属性：
        agents: 代理配置
        channels: 通道配置
        providers: 提供商配置
        api: API 服务器配置
        gateway: 网关配置
        tools: 工具配置
    
    环境变量示例：
        NANOBOT_AGENTS__DEFAULTS__MODEL=anthropic/claude-opus-4-5
        NANOBOT_PROVIDERS__OPENROUTER__API_KEY=sk-or-xxx
    """

    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)

    @property
    def workspace_path(self) -> Path:
        """
        获取扩展后的工作空间路径
        
        将配置中的工作空间路径（可能包含 ~）扩展为绝对路径。
        
        Returns:
            Path: 扩展后的工作空间路径
        """
        return Path(self.agents.defaults.workspace).expanduser()

    def _match_provider(
        self, model: str | None = None
    ) -> tuple["ProviderConfig | None", str | None]:
        """
        匹配提供商配置及其注册表名称
        
        根据模型名称和配置自动匹配最合适的提供商。
        匹配顺序：
        1. 显式指定的提供商（provider != "auto"）
        2. 模型名称前缀匹配（如 "openai/gpt-4" 匹配 openai）
        3. 关键词匹配（按注册表顺序）
        4. 本地提供商回退（如 Ollama）
        5. 网关提供商回退
        
        Args:
            model: 模型名称（可选，默认使用配置中的模型）
            
        Returns:
            元组：(提供商配置, 注册表名称)
            如果没有匹配，返回 (None, None)
        """
        from nanobot.providers.registry import PROVIDERS, find_by_name

        forced = self.agents.defaults.provider
        # 如果显式指定了提供商（非 "auto"）
        if forced != "auto":
            spec = find_by_name(forced)
            if spec:
                p = getattr(self.providers, spec.name, None)
                return (p, spec.name) if p else (None, None)
            return None, None

        # 准备模型名称的各种形式用于匹配
        model_lower = (model or self.agents.defaults.model).lower()
        model_normalized = model_lower.replace("-", "_")
        model_prefix = model_lower.split("/", 1)[0] if "/" in model_lower else ""
        normalized_prefix = model_prefix.replace("-", "_")

        def _kw_matches(kw: str) -> bool:
            """检查关键词是否匹配模型名称"""
            kw = kw.lower()
            return kw in model_lower or kw.replace("-", "_") in model_normalized

        # 优先级 1：显式提供商前缀匹配
        # 防止 "github-copilot/...codex" 匹配到 openai_codex
        for spec in PROVIDERS:
            p = getattr(self.providers, spec.name, None)
            if p and model_prefix and normalized_prefix == spec.name:
                if spec.is_oauth or spec.is_local or p.api_key:
                    return p, spec.name

        # 优先级 2：关键词匹配（按注册表顺序）
        for spec in PROVIDERS:
            p = getattr(self.providers, spec.name, None)
            if p and any(_kw_matches(kw) for kw in spec.keywords):
                if spec.is_oauth or spec.is_local or p.api_key:
                    return p, spec.name

        # 优先级 3：本地提供商回退
        # 对于没有提供商特定关键词的模型（如 Ollama 上的 "llama3.2"）
        # 优先选择 api_base 匹配 detect_by_base_keyword 的提供商
        local_fallback: tuple[ProviderConfig, str] | None = None
        for spec in PROVIDERS:
            if not spec.is_local:
                continue
            p = getattr(self.providers, spec.name, None)
            if not (p and p.api_base):
                continue
            if spec.detect_by_base_keyword and spec.detect_by_base_keyword in p.api_base:
                return p, spec.name
            if local_fallback is None:
                local_fallback = (p, spec.name)
        if local_fallback:
            return local_fallback

        # 优先级 4：网关提供商回退，然后是其他提供商（按注册表顺序）
        # OAuth 提供商不是有效的回退选项——它们需要显式选择模型
        for spec in PROVIDERS:
            if spec.is_oauth:
                continue
            p = getattr(self.providers, spec.name, None)
            if p and p.api_key:
                return p, spec.name
        return None, None

    def get_provider(self, model: str | None = None) -> ProviderConfig | None:
        """
        获取匹配的提供商配置（api_key, api_base, extra_headers）
        
        如果没有精确匹配，回退到第一个可用的提供商。
        
        Args:
            model: 模型名称（可选）
            
        Returns:
            ProviderConfig | None: 提供商配置，如果没有匹配则返回 None
        """
        p, _ = self._match_provider(model)
        return p

    def get_provider_name(self, model: str | None = None) -> str | None:
        """
        获取匹配提供商的注册表名称
        
        Args:
            model: 模型名称（可选）
            
        Returns:
            str | None: 提供商名称（如 "deepseek", "openrouter"），如果没有匹配则返回 None
        """
        _, name = self._match_provider(model)
        return name

    def get_api_key(self, model: str | None = None) -> str | None:
        """
        获取指定模型的 API 密钥
        
        如果没有精确匹配，回退到第一个可用的密钥。
        
        Args:
            model: 模型名称（可选）
            
        Returns:
            str | None: API 密钥，如果没有则返回 None
        """
        p = self.get_provider(model)
        return p.api_key if p else None

    def get_api_base(self, model: str | None = None) -> str | None:
        """
        获取指定模型的 API 基础 URL
        
        为网关和本地提供商应用默认 URL。
        
        Args:
            model: 模型名称（可选）
            
        Returns:
            str | None: API 基础 URL，如果没有则返回 None
        """
        from nanobot.providers.registry import find_by_name

        p, name = self._match_provider(model)
        if p and p.api_base:
            return p.api_base
        # 只有网关在这里获取默认 api_base
        # 标准提供商在提供商构造函数中解析其基础 URL
        if name:
            spec = find_by_name(name)
            if spec and (spec.is_gateway or spec.is_local) and spec.default_api_base:
                return spec.default_api_base
        return None

    model_config = ConfigDict(env_prefix="NANOBOT_", env_nested_delimiter="__")
