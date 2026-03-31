"""
文件系统工具模块，提供文件读写、编辑和目录列表功能。

该模块实现了 agent 与文件系统交互的核心工具：
- read_file: 读取文件内容，支持分页和图片识别
- write_file: 写入文件内容，自动创建父目录
- edit_file: 编辑文件，支持模糊匹配和差异显示
- list_dir: 列出目录内容，支持递归和过滤

所有工具都支持路径限制，可以限制只能访问特定目录。
"""

import difflib
import mimetypes
from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool
from nanobot.utils.helpers import build_image_content_blocks, detect_image_mime


def _resolve_path(
    path: str,
    workspace: Path | None = None,
    allowed_dir: Path | None = None,
    extra_allowed_dirs: list[Path] | None = None,
) -> Path:
    """
    解析路径并强制执行目录限制。

    如果路径是相对路径，会相对于工作空间解析。
    如果设置了 allowed_dir，会检查路径是否在允许的目录内。

    Args:
        path: 原始路径字符串
        workspace: 工作空间路径
        allowed_dir: 允许访问的目录
        extra_allowed_dirs: 额外允许访问的目录列表

    Returns:
        解析后的绝对路径

    Raises:
        PermissionError: 如果路径不在允许的目录内
    """
    p = Path(path).expanduser()
    if not p.is_absolute() and workspace:
        p = workspace / p
    resolved = p.resolve()
    if allowed_dir:
        all_dirs = [allowed_dir] + (extra_allowed_dirs or [])
        if not any(_is_under(resolved, d) for d in all_dirs):
            raise PermissionError(f"Path {path} is outside allowed directory {allowed_dir}")
    return resolved


def _is_under(path: Path, directory: Path) -> bool:
    """
    检查路径是否在指定目录下。

    Args:
        path: 要检查的路径
        directory: 目录路径

    Returns:
        如果路径在目录下则返回 True
    """
    try:
        path.relative_to(directory.resolve())
        return True
    except ValueError:
        return False


class _FsTool(Tool):
    """
    文件系统工具的共享基类。

    提供通用的初始化和路径解析功能。

    Attributes:
        _workspace: 工作空间路径
        _allowed_dir: 允许访问的目录
        _extra_allowed_dirs: 额外允许访问的目录列表
    """

    def __init__(
        self,
        workspace: Path | None = None,
        allowed_dir: Path | None = None,
        extra_allowed_dirs: list[Path] | None = None,
    ):
        """
        初始化文件系统工具。

        Args:
            workspace: 工作空间路径
            allowed_dir: 允许访问的目录（安全限制）
            extra_allowed_dirs: 额外允许访问的目录列表
        """
        self._workspace = workspace
        self._allowed_dir = allowed_dir
        self._extra_allowed_dirs = extra_allowed_dirs

    def _resolve(self, path: str) -> Path:
        """
        解析路径并检查权限。

        Args:
            path: 原始路径字符串

        Returns:
            解析后的路径
        """
        return _resolve_path(path, self._workspace, self._allowed_dir, self._extra_allowed_dirs)


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------

class ReadFileTool(_FsTool):
    """
    文件读取工具，支持分页和图片识别。

    该工具可以：
    - 读取文本文件并显示行号
    - 识别并返回图片文件
    - 分页读取大文件
    - 自动截断过长的输出

    返回格式：
    - 文本文件：带行号的内容
    - 图片文件：base64 编码的图片块
    """

    # 最大字符数限制
    _MAX_CHARS = 128_000
    # 默认每页行数
    _DEFAULT_LIMIT = 2000

    @property
    def name(self) -> str:
        """工具名称。"""
        return "read_file"

    @property
    def description(self) -> str:
        """工具描述。"""
        return (
            "Read the contents of a file. Returns numbered lines. "
            "Use offset and limit to paginate through large files."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        """工具参数的 JSON Schema。"""
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "The file path to read"},
                "offset": {
                    "type": "integer",
                    "description": "Line number to start reading from (1-indexed, default 1)",
                    "minimum": 1,
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of lines to read (default 2000)",
                    "minimum": 1,
                },
            },
            "required": ["path"],
        }

    async def execute(self, path: str | None = None, offset: int = 1, limit: int | None = None, **kwargs: Any) -> Any:
        """
        执行文件读取。

        Args:
            path: 文件路径
            offset: 起始行号（从 1 开始）
            limit: 最大读取行数

        Returns:
            文件内容或错误消息
        """
        try:
            if not path:
                return "Error reading file: Unknown path"
            fp = self._resolve(path)
            if not fp.exists():
                return f"Error: File not found: {path}"
            if not fp.is_file():
                return f"Error: Not a file: {path}"

            raw = fp.read_bytes()
            if not raw:
                return f"(Empty file: {path})"

            # 检测是否为图片
            mime = detect_image_mime(raw) or mimetypes.guess_type(path)[0]
            if mime and mime.startswith("image/"):
                return build_image_content_blocks(raw, mime, str(fp), f"(Image file: {path})")

            # 尝试解码为文本
            try:
                text_content = raw.decode("utf-8")
            except UnicodeDecodeError:
                return f"Error: Cannot read binary file {path} (MIME: {mime or 'unknown'}). Only UTF-8 text and images are supported."

            # 分页处理
            all_lines = text_content.splitlines()
            total = len(all_lines)

            if offset < 1:
                offset = 1
            if offset > total:
                return f"Error: offset {offset} is beyond end of file ({total} lines)"

            start = offset - 1
            end = min(start + (limit or self._DEFAULT_LIMIT), total)
            numbered = [f"{start + i + 1}| {line}" for i, line in enumerate(all_lines[start:end])]
            result = "\n".join(numbered)

            # 截断过长的输出
            if len(result) > self._MAX_CHARS:
                trimmed, chars = [], 0
                for line in numbered:
                    chars += len(line) + 1
                    if chars > self._MAX_CHARS:
                        break
                    trimmed.append(line)
                end = start + len(trimmed)
                result = "\n".join(trimmed)

            # 添加分页提示
            if end < total:
                result += f"\n\n(Showing lines {offset}-{end} of {total}. Use offset={end + 1} to continue.)"
            else:
                result += f"\n\n(End of file — {total} lines total)"
            return result
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error reading file: {e}"


# ---------------------------------------------------------------------------
# write_file
# ---------------------------------------------------------------------------

class WriteFileTool(_FsTool):
    """
    文件写入工具。

    该工具可以：
    - 写入内容到文件
    - 自动创建父目录
    - 覆盖现有文件
    """

    @property
    def name(self) -> str:
        """工具名称。"""
        return "write_file"

    @property
    def description(self) -> str:
        """工具描述。"""
        return "Write content to a file at the given path. Creates parent directories if needed."

    @property
    def parameters(self) -> dict[str, Any]:
        """工具参数的 JSON Schema。"""
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "The file path to write to"},
                "content": {"type": "string", "description": "The content to write"},
            },
            "required": ["path", "content"],
        }

    async def execute(self, path: str | None = None, content: str | None = None, **kwargs: Any) -> str:
        """
        执行文件写入。

        Args:
            path: 文件路径
            content: 要写入的内容

        Returns:
            操作结果消息
        """
        try:
            if not path:
                raise ValueError("Unknown path")
            if content is None:
                raise ValueError("Unknown content")
            fp = self._resolve(path)
            # 自动创建父目录
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(content, encoding="utf-8")
            return f"Successfully wrote {len(content)} bytes to {fp}"
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error writing file: {e}"


# ---------------------------------------------------------------------------
# edit_file
# ---------------------------------------------------------------------------

def _find_match(content: str, old_text: str) -> tuple[str | None, int]:
    """
    在内容中定位要替换的文本。

    查找策略：
    1. 首先尝试精确匹配
    2. 如果失败，尝试忽略空白差异的模糊匹配

    Args:
        content: 文件内容
        old_text: 要查找的文本

    Returns:
        (匹配的文本片段, 匹配次数) 或 (None, 0)
    """
    if old_text in content:
        return old_text, content.count(old_text)

    # 模糊匹配：忽略行首尾空白
    old_lines = old_text.splitlines()
    if not old_lines:
        return None, 0
    stripped_old = [l.strip() for l in old_lines]
    content_lines = content.splitlines()

    candidates = []
    for i in range(len(content_lines) - len(stripped_old) + 1):
        window = content_lines[i : i + len(stripped_old)]
        if [l.strip() for l in window] == stripped_old:
            candidates.append("\n".join(window))

    if candidates:
        return candidates[0], len(candidates)
    return None, 0


class EditFileTool(_FsTool):
    """
    文件编辑工具，支持模糊匹配。

    该工具可以：
    - 查找并替换文本
    - 支持忽略空白差异的模糊匹配
    - 显示差异帮助定位问题
    - 支持全局替换

    编辑策略：
    1. 首先尝试精确匹配
    2. 如果失败，尝试忽略行首尾空白的匹配
    3. 如果找到多个匹配，提示用户提供更多上下文
    """

    @property
    def name(self) -> str:
        """工具名称。"""
        return "edit_file"

    @property
    def description(self) -> str:
        """工具描述。"""
        return (
            "Edit a file by replacing old_text with new_text. "
            "Supports minor whitespace/line-ending differences. "
            "Set replace_all=true to replace every occurrence."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        """工具参数的 JSON Schema。"""
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "The file path to edit"},
                "old_text": {"type": "string", "description": "The text to find and replace"},
                "new_text": {"type": "string", "description": "The text to replace with"},
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace all occurrences (default false)",
                },
            },
            "required": ["path", "old_text", "new_text"],
        }

    async def execute(
        self, path: str | None = None, old_text: str | None = None,
        new_text: str | None = None,
        replace_all: bool = False, **kwargs: Any,
    ) -> str:
        """
        执行文件编辑。

        Args:
            path: 文件路径
            old_text: 要查找的文本
            new_text: 替换后的文本
            replace_all: 是否替换所有匹配

        Returns:
            操作结果消息
        """
        try:
            if not path:
                raise ValueError("Unknown path")
            if old_text is None:
                raise ValueError("Unknown old_text")
            if new_text is None:
                raise ValueError("Unknown new_text")

            fp = self._resolve(path)
            if not fp.exists():
                return f"Error: File not found: {path}"

            # 读取并规范化换行符
            raw = fp.read_bytes()
            uses_crlf = b"\r\n" in raw
            content = raw.decode("utf-8").replace("\r\n", "\n")
            match, count = _find_match(content, old_text.replace("\r\n", "\n"))

            if match is None:
                return self._not_found_msg(old_text, content, path)
            if count > 1 and not replace_all:
                return (
                    f"Warning: old_text appears {count} times. "
                    "Provide more context to make it unique, or set replace_all=true."
                )

            # 执行替换
            norm_new = new_text.replace("\r\n", "\n")
            new_content = content.replace(match, norm_new) if replace_all else content.replace(match, norm_new, 1)
            # 恢复原始换行符
            if uses_crlf:
                new_content = new_content.replace("\n", "\r\n")

            fp.write_bytes(new_content.encode("utf-8"))
            return f"Successfully edited {fp}"
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error editing file: {e}"

    @staticmethod
    def _not_found_msg(old_text: str, content: str, path: str) -> str:
        """
        生成未找到匹配时的错误消息，包含最佳匹配的差异。

        Args:
            old_text: 要查找的文本
            content: 文件内容
            path: 文件路径

        Returns:
            错误消息
        """
        lines = content.splitlines(keepends=True)
        old_lines = old_text.splitlines(keepends=True)
        window = len(old_lines)

        # 查找最佳匹配
        best_ratio, best_start = 0.0, 0
        for i in range(max(1, len(lines) - window + 1)):
            ratio = difflib.SequenceMatcher(None, old_lines, lines[i : i + window]).ratio()
            if ratio > best_ratio:
                best_ratio, best_start = ratio, i

        # 如果找到相似的内容，显示差异
        if best_ratio > 0.5:
            diff = "\n".join(difflib.unified_diff(
                old_lines, lines[best_start : best_start + window],
                fromfile="old_text (provided)",
                tofile=f"{path} (actual, line {best_start + 1})",
                lineterm="",
            ))
            return f"Error: old_text not found in {path}.\nBest match ({best_ratio:.0%} similar) at line {best_start + 1}:\n{diff}"
        return f"Error: old_text not found in {path}. No similar text found. Verify the file content."


# ---------------------------------------------------------------------------
# list_dir
# ---------------------------------------------------------------------------

class ListDirTool(_FsTool):
    """
    目录列表工具，支持递归和过滤。

    该工具可以：
    - 列出目录内容
    - 递归列出子目录
    - 自动过滤常见的噪声目录（.git, node_modules 等）
    - 限制返回条目数量
    """

    # 默认最大条目数
    _DEFAULT_MAX = 200
    # 自动忽略的目录
    _IGNORE_DIRS = {
        ".git", "node_modules", "__pycache__", ".venv", "venv",
        "dist", "build", ".tox", ".mypy_cache", ".pytest_cache",
        ".ruff_cache", ".coverage", "htmlcov",
    }

    @property
    def name(self) -> str:
        """工具名称。"""
        return "list_dir"

    @property
    def description(self) -> str:
        """工具描述。"""
        return (
            "List the contents of a directory. "
            "Set recursive=true to explore nested structure. "
            "Common noise directories (.git, node_modules, __pycache__, etc.) are auto-ignored."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        """工具参数的 JSON Schema。"""
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "The directory path to list"},
                "recursive": {
                    "type": "boolean",
                    "description": "Recursively list all files (default false)",
                },
                "max_entries": {
                    "type": "integer",
                    "description": "Maximum entries to return (default 200)",
                    "minimum": 1,
                },
            },
            "required": ["path"],
        }

    async def execute(
        self, path: str | None = None, recursive: bool = False,
        max_entries: int | None = None, **kwargs: Any,
    ) -> str:
        """
        执行目录列表。

        Args:
            path: 目录路径
            recursive: 是否递归列出
            max_entries: 最大返回条目数

        Returns:
            目录内容列表
        """
        try:
            if path is None:
                raise ValueError("Unknown path")
            dp = self._resolve(path)
            if not dp.exists():
                return f"Error: Directory not found: {path}"
            if not dp.is_dir():
                return f"Error: Not a directory: {path}"

            cap = max_entries or self._DEFAULT_MAX
            items: list[str] = []
            total = 0

            if recursive:
                # 递归列出
                for item in sorted(dp.rglob("*")):
                    if any(p in self._IGNORE_DIRS for p in item.parts):
                        continue
                    total += 1
                    if len(items) < cap:
                        rel = item.relative_to(dp)
                        items.append(f"{rel}/" if item.is_dir() else str(rel))
            else:
                # 非递归列出
                for item in sorted(dp.iterdir()):
                    if item.name in self._IGNORE_DIRS:
                        continue
                    total += 1
                    if len(items) < cap:
                        pfx = "📁 " if item.is_dir() else "📄 "
                        items.append(f"{pfx}{item.name}")

            if not items and total == 0:
                return f"Directory {path} is empty"

            result = "\n".join(items)
            if total > cap:
                result += f"\n\n(truncated, showing first {cap} of {total} entries)"
            return result
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error listing directory: {e}"
