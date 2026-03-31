"""
运行时路径助手：从活动配置上下文派生的路径管理

这个模块提供了 nanobot 运行时各种目录和路径的管理功能。所有路径都基于
配置文件的位置自动派生，支持多实例部署。

主要功能：
- 数据目录管理：获取实例级别的运行时数据目录
- 子目录管理：创建和管理各种运行时子目录
- 媒体目录：管理媒体文件存储
- 定时任务目录：管理定时任务存储
- 日志目录：管理日志文件存储
- 工作空间路径：管理代理的工作空间
- CLI 历史记录：管理命令行历史
- 桥接目录：管理 WhatsApp 桥接安装

目录结构示例：
~/.nanobot/
├── config.json          # 配置文件
├── workspace/           # 工作空间
├── media/               # 媒体文件
│   └── telegram/        # 按通道分类
├── cron/                # 定时任务
├── logs/                # 日志文件
└── bridge/              # WhatsApp 桥接
"""

from __future__ import annotations

from pathlib import Path

from nanobot.config.loader import get_config_path
from nanobot.utils.helpers import ensure_dir


def get_data_dir() -> Path:
    """
    获取实例级别的运行时数据目录
    
    数据目录是配置文件的父目录，所有实例相关的数据都存储在这里。
    例如，如果配置文件在 ~/.nanobot/config.json，则数据目录是 ~/.nanobot/
    
    Returns:
        Path: 数据目录的路径（已确保存在）
        
    示例：
        >>> data_dir = get_data_dir()
        >>> print(data_dir)  # /home/user/.nanobot
    """
    return ensure_dir(get_config_path().parent)


def get_runtime_subdir(name: str) -> Path:
    """
    获取实例数据目录下的命名运行时子目录
    
    在数据目录下创建或获取一个指定名称的子目录。
    用于组织不同类型的运行时数据。
    
    Args:
        name: 子目录名称（如 "media", "cron", "logs"）
        
    Returns:
        Path: 子目录的路径（已确保存在）
        
    示例：
        >>> media_dir = get_runtime_subdir("media")
        >>> print(media_dir)  # /home/user/.nanobot/media
    """
    return ensure_dir(get_data_dir() / name)


def get_media_dir(channel: str | None = None) -> Path:
    """
    获取媒体目录，可选择按通道命名空间分隔
    
    媒体目录用于存储从用户接收的图片、文件等媒体资源。
    可以按通道进行分隔，避免不同通道的媒体文件混淆。
    
    Args:
        channel: 可选的通道名称（如 "telegram", "discord"）
                如果提供，则在媒体目录下创建该通道的子目录
        
    Returns:
        Path: 媒体目录的路径（已确保存在）
        
    示例：
        >>> base_media = get_media_dir()
        >>> print(base_media)  # /home/user/.nanobot/media
        
        >>> telegram_media = get_media_dir("telegram")
        >>> print(telegram_media)  # /home/user/.nanobot/media/telegram
    """
    base = get_runtime_subdir("media")
    return ensure_dir(base / channel) if channel else base


def get_cron_dir() -> Path:
    """
    获取定时任务存储目录
    
    定时任务目录用于存储所有定时任务的配置和状态。
    
    Returns:
        Path: 定时任务目录的路径（已确保存在）
        
    示例：
        >>> cron_dir = get_cron_dir()
        >>> print(cron_dir)  # /home/user/.nanobot/cron
    """
    return get_runtime_subdir("cron")


def get_logs_dir() -> Path:
    """
    获取日志目录
    
    日志目录用于存储 nanobot 的运行日志文件。
    
    Returns:
        Path: 日志目录的路径（已确保存在）
        
    示例：
        >>> logs_dir = get_logs_dir()
        >>> print(logs_dir)  # /home/user/.nanobot/logs
    """
    return get_runtime_subdir("logs")


def get_workspace_path(workspace: str | None = None) -> Path:
    """
    解析并确保代理工作空间路径存在
    
    工作空间是代理执行任务的主要目录，代理可以在这里读写文件、
    执行命令等。如果未指定工作空间，使用默认路径。
    
    Args:
        workspace: 可选的工作空间路径字符串
                  支持 ~ 扩展（如 "~/my_workspace"）
        
    Returns:
        Path: 工作空间的路径（已确保存在）
        
    示例：
        >>> default_ws = get_workspace_path()
        >>> print(default_ws)  # /home/user/.nanobot/workspace
        
        >>> custom_ws = get_workspace_path("~/my_project")
        >>> print(custom_ws)  # /home/user/my_project
    """
    path = Path(workspace).expanduser() if workspace else Path.home() / ".nanobot" / "workspace"
    return ensure_dir(path)


def is_default_workspace(workspace: str | Path | None) -> bool:
    """
    检查工作空间是否解析为 nanobot 的默认工作空间路径
    
    用于判断是否使用默认工作空间，这对某些迁移和兼容性逻辑很重要。
    
    Args:
        workspace: 要检查的工作空间路径（字符串或 Path 对象）
                  可以是 None，表示使用默认路径
        
    Returns:
        bool: 如果是默认工作空间返回 True，否则返回 False
        
    示例：
        >>> is_default_workspace(None)  # True
        >>> is_default_workspace("~/.nanobot/workspace")  # True
        >>> is_default_workspace("~/my_project")  # False
    """
    current = Path(workspace).expanduser() if workspace is not None else Path.home() / ".nanobot" / "workspace"
    default = Path.home() / ".nanobot" / "workspace"
    return current.resolve(strict=False) == default.resolve(strict=False)


def get_cli_history_path() -> Path:
    """
    获取共享的 CLI 历史记录文件路径
    
    CLI 历史记录文件存储用户在命令行界面输入的所有命令，
    支持上下箭头键浏览历史命令。
    
    注意：这是全局共享的，不随配置路径变化。
    
    Returns:
        Path: CLI 历史记录文件的路径
        
    示例：
        >>> history_path = get_cli_history_path()
        >>> print(history_path)  # /home/user/.nanobot/history/cli_history
    """
    return Path.home() / ".nanobot" / "history" / "cli_history"


def get_bridge_install_dir() -> Path:
    """
    获取共享的 WhatsApp 桥接安装目录
    
    WhatsApp 桥接需要 Node.js 环境，这个目录存储桥接代码和依赖。
    
    注意：这是全局共享的，不随配置路径变化。
    
    Returns:
        Path: WhatsApp 桥接安装目录的路径
        
    示例：
        >>> bridge_dir = get_bridge_install_dir()
        >>> print(bridge_dir)  # /home/user/.nanobot/bridge
    """
    return Path.home() / ".nanobot" / "bridge"


def get_legacy_sessions_dir() -> Path:
    """
    获取旧版全局会话目录（用于迁移回退）
    
    在旧版本中，会话数据存储在全局目录中。这个函数返回该目录路径，
    用于数据迁移时的回退处理。
    
    注意：这是全局共享的，不随配置路径变化。
    
    Returns:
        Path: 旧版会话目录的路径
        
    示例：
        >>> legacy_dir = get_legacy_sessions_dir()
        >>> print(legacy_dir)  # /home/user/.nanobot/sessions
    """
    return Path.home() / ".nanobot" / "sessions"
