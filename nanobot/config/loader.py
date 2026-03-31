"""
配置加载工具模块

这个模块提供了配置文件的加载、保存和迁移功能。主要功能包括：
- 从 JSON 文件加载配置
- 保存配置到 JSON 文件
- 配置格式的自动迁移（处理旧版本配置格式）
- 多实例配置路径管理

配置文件默认位置：~/.nanobot/config.json
"""

import json
from pathlib import Path

import pydantic
from loguru import logger

from nanobot.config.schema import Config

# 全局变量：存储当前配置路径（用于多实例支持）
# 多实例允许同时运行多个 nanobot 实例，每个实例有自己的配置文件
_current_config_path: Path | None = None


def set_config_path(path: Path) -> None:
    """
    设置当前配置路径（用于派生数据目录）
    
    在多实例模式下，每个实例可以有自己的配置文件和数据目录。
    这个函数用于设置当前实例的配置文件路径。
    
    Args:
        path: 配置文件的路径
        
    示例：
        >>> set_config_path(Path("/path/to/config.json"))
        >>> get_config_path()  # 返回刚才设置的路径
    """
    global _current_config_path
    _current_config_path = path


def get_config_path() -> Path:
    """
    获取配置文件路径
    
    如果设置了自定义配置路径，则返回该路径；
    否则返回默认路径 ~/.nanobot/config.json
    
    Returns:
        Path: 配置文件的路径
        
    示例：
        >>> path = get_config_path()
        >>> print(path)  # /home/user/.nanobot/config.json
    """
    if _current_config_path:
        return _current_config_path
    return Path.home() / ".nanobot" / "config.json"


def load_config(config_path: Path | None = None) -> Config:
    """
    从文件加载配置或创建默认配置
    
    这个函数会：
    1. 确定配置文件路径（使用提供的路径或默认路径）
    2. 如果文件存在，尝试加载并解析
    3. 执行配置迁移（处理旧版本格式）
    4. 验证配置数据
    5. 如果加载失败，返回默认配置
    
    Args:
        config_path: 可选的配置文件路径。如果未提供，使用默认路径
        
    Returns:
        Config: 加载的配置对象
        
    示例：
        >>> config = load_config()  # 使用默认路径
        >>> config = load_config(Path("/custom/path/config.json"))  # 使用自定义路径
    """
    path = config_path or get_config_path()

    if path.exists():
        try:
            # 读取 JSON 文件
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            # 执行配置迁移（处理旧版本格式）
            data = _migrate_config(data)
            # 验证并返回配置对象
            return Config.model_validate(data)
        except (json.JSONDecodeError, ValueError, pydantic.ValidationError) as e:
            # 加载失败时记录警告并返回默认配置
            logger.warning(f"Failed to load config from {path}: {e}")
            logger.warning("Using default configuration.")

    # 文件不存在或加载失败，返回默认配置
    return Config()


def save_config(config: Config, config_path: Path | None = None) -> None:
    """
    保存配置到文件
    
    将配置对象序列化为 JSON 格式并保存到指定文件。
    如果目录不存在，会自动创建。
    
    Args:
        config: 要保存的配置对象
        config_path: 可选的保存路径。如果未提供，使用默认路径
        
    示例：
        >>> config = Config()
        >>> save_config(config)  # 保存到默认路径
        >>> save_config(config, Path("/custom/path/config.json"))  # 保存到自定义路径
    """
    path = config_path or get_config_path()
    # 确保父目录存在
    path.parent.mkdir(parents=True, exist_ok=True)

    # 将配置对象转换为 JSON 兼容的字典
    data = config.model_dump(mode="json", by_alias=True)

    # 写入文件，使用 UTF-8 编码和缩进格式
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _migrate_config(data: dict) -> dict:
    """
    迁移旧配置格式到当前格式
    
    这个函数处理配置格式的版本升级，确保旧版本的配置文件
    能够正确加载到新版本的 nanobot 中。
    
    当前的迁移：
    - 将 tools.exec.restrictToWorkspace 移动到 tools.restrictToWorkspace
    
    Args:
        data: 原始配置数据字典
        
    Returns:
        dict: 迁移后的配置数据字典
        
    示例：
        >>> old_data = {"tools": {"exec": {"restrictToWorkspace": True}}}
        >>> new_data = _migrate_config(old_data)
        >>> # new_data = {"tools": {"restrictToWorkspace": True, "exec": {}}}
    """
    # 将 tools.exec.restrictToWorkspace 移动到 tools.restrictToWorkspace
    tools = data.get("tools", {})
    exec_cfg = tools.get("exec", {})
    if "restrictToWorkspace" in exec_cfg and "restrictToWorkspace" not in tools:
        tools["restrictToWorkspace"] = exec_cfg.pop("restrictToWorkspace")
    return data
