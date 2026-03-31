"""
Shell 命令执行工具模块，提供安全的命令行执行能力。

该模块实现了 exec 工具，允许 agent 执行系统命令：
- 支持超时控制
- 支持工作目录限制
- 支持环境变量配置
- 支持输出截断

安全特性：
- 可以限制命令只能在工作空间内执行
- 可以配置超时防止长时间运行
- 可以配置 PATH 环境变量
"""

import asyncio
import os
import shutil
from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool


class ExecTool(Tool):
    """
    Shell 命令执行工具。

    该工具允许 agent 执行系统命令，支持：
    - 超时控制
    - 工作目录设置
    - 环境变量配置
    - 输出截断
    - 安全限制

    安全特性：
    - 可以限制命令只能在工作空间内执行
    - 可以配置超时防止长时间运行
    - 可以配置 PATH 环境变量

    Attributes:
        _working_dir: 工作目录
        _timeout: 超时时间（秒）
        _restrict_to_workspace: 是否限制在工作空间内
        _path_append: 要追加到 PATH 的路径列表
    """

    # 最大输出长度
    _MAX_OUTPUT = 64_000

    def __init__(
        self,
        working_dir: str,
        timeout: int = 120,
        restrict_to_workspace: bool = False,
        path_append: list[str] | None = None,
    ):
        """
        初始化 Shell 执行工具。

        Args:
            working_dir: 工作目录
            timeout: 超时时间（秒）
            restrict_to_workspace: 是否限制在工作空间内
            path_append: 要追加到 PATH 的路径列表
        """
        self._working_dir = working_dir
        self._timeout = timeout
        self._restrict_to_workspace = restrict_to_workspace
        self._path_append = path_append or []

    @property
    def name(self) -> str:
        """工具名称。"""
        return "exec"

    @property
    def description(self) -> str:
        """工具描述。"""
        return (
            "Execute a shell command and return its output. "
            "Use for system operations like file management, package installation, etc. "
            "Avoid interactive commands. Prefer file tools when possible."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        """工具参数的 JSON Schema。"""
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute",
                },
                "cwd": {
                    "type": "string",
                    "description": "Working directory (optional, defaults to workspace)",
                },
                "timeout": {
                    "type": "integer",
                    "description": f"Timeout in seconds (default: {self._timeout})",
                    "minimum": 1,
                    "maximum": 600,
                },
            },
            "required": ["command"],
        }

    async def execute(
        self,
        command: str,
        cwd: str | None = None,
        timeout: int | None = None,
        **kwargs: Any,
    ) -> str:
        """
        执行 Shell 命令。

        Args:
            command: 要执行的命令
            cwd: 工作目录（可选）
            timeout: 超时时间（可选）

        Returns:
            命令输出或错误消息
        """
        # 确定工作目录
        work_dir = Path(cwd) if cwd else Path(self._working_dir)
        if not work_dir.is_absolute():
            work_dir = Path(self._working_dir) / work_dir

        # 检查工作目录是否在工作空间内
        if self._restrict_to_workspace:
            try:
                work_dir.resolve().relative_to(Path(self._working_dir).resolve())
            except ValueError:
                return f"Error: Working directory must be inside {self._working_dir}"

        if not work_dir.exists():
            return f"Error: Working directory does not exist: {work_dir}"

        # 配置环境变量
        env = os.environ.copy()
        if self._path_append:
            current_path = env.get("PATH", "")
            env["PATH"] = os.pathsep.join(self._path_append) + os.pathsep + current_path

        # 确定超时时间
        actual_timeout = min(timeout or self._timeout, 600)

        try:
            # 执行命令
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(work_dir),
                env=env,
            )

            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=actual_timeout,
            )

            # 解码输出
            output_parts = []
            if stdout:
                try:
                    output_parts.append(stdout.decode("utf-8"))
                except UnicodeDecodeError:
                    output_parts.append(stdout.decode("latin-1"))
            if stderr:
                try:
                    output_parts.append(stderr.decode("utf-8"))
                except UnicodeDecodeError:
                    output_parts.append(stderr.decode("latin-1"))

            output = "\n".join(output_parts).strip()

            # 截断过长的输出
            if len(output) > self._MAX_OUTPUT:
                output = output[: self._MAX_OUTPUT] + "\n... (output truncated)"

            # 添加退出码信息
            if process.returncode != 0:
                output = f"Exit code: {process.returncode}\n{output}"

            return output or "(no output)"

        except asyncio.TimeoutError:
            return f"Error: Command timed out after {actual_timeout} seconds"
        except FileNotFoundError as e:
            return f"Error: Command not found: {e}"
        except Exception as e:
            return f"Error: {type(e).__name__}: {e}"

    @staticmethod
    def is_command_available(command: str) -> bool:
        """
        检查命令是否可用。

        Args:
            command: 命令名称

        Returns:
            命令是否可用
        """
        return shutil.which(command) is not None
