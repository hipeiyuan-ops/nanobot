"""
配置模块：nanobot 的配置管理系统

这个模块提供了 nanobot 的配置管理功能，包括：
- 配置文件的加载和保存
- 配置路径的管理
- 运行时数据目录的管理
- 配置模式的定义

主要组件：
- Config: 根配置类，包含所有配置项
- load_config: 加载配置文件
- get_config_path: 获取配置文件路径
- get_data_dir: 获取数据目录
- get_runtime_subdir: 获取运行时子目录
- get_media_dir: 获取媒体文件目录
- get_cron_dir: 获取定时任务目录
- get_logs_dir: 获取日志目录
- get_workspace_path: 获取工作空间路径
- is_default_workspace: 检查是否为默认工作空间
- get_cli_history_path: 获取 CLI 历史记录路径
- get_bridge_install_dir: 获取桥接安装目录
- get_legacy_sessions_dir: 获取旧版会话目录
"""

from nanobot.config.loader import get_config_path, load_config
from nanobot.config.paths import (
    get_bridge_install_dir,
    get_cli_history_path,
    get_cron_dir,
    get_data_dir,
    get_legacy_sessions_dir,
    is_default_workspace,
    get_logs_dir,
    get_media_dir,
    get_runtime_subdir,
    get_workspace_path,
)
from nanobot.config.schema import Config

__all__ = [
    "Config",
    "load_config",
    "get_config_path",
    "get_data_dir",
    "get_runtime_subdir",
    "get_media_dir",
    "get_cron_dir",
    "get_logs_dir",
    "get_workspace_path",
    "is_default_workspace",
    "get_cli_history_path",
    "get_bridge_install_dir",
    "get_legacy_sessions_dir",
]
