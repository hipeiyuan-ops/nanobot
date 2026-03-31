"""
Agent 技能加载器模块，提供动态加载和管理 agent 技能的能力。

技能是扩展 agent 能力的 markdown 文件（SKILL.md），用于教导 agent
如何使用特定工具或执行特定任务。技能可以包含：
- 使用说明
- 示例
- 元数据（描述、依赖等）

技能来源：
1. 工作空间技能：用户自定义的技能，优先级更高
2. 内置技能：系统提供的默认技能
"""

import json
import os
import re
import shutil
from pathlib import Path

# 默认内置技能目录（相对于本文件的父目录）
BUILTIN_SKILLS_DIR = Path(__file__).parent.parent / "skills"


class SkillsLoader:
    """
    Agent 技能加载器。

    该类负责加载和管理 agent 可用的技能。技能是 markdown 文件，
    用于教导 agent 如何使用特定工具或执行特定任务。

    技能优先级：
    1. 工作空间技能（workspace/skills/）：用户自定义，优先级最高
    2. 内置技能（builtin_skills/）：系统提供，作为后备

    技能元数据：
    技能文件可以包含 YAML 前置元数据，用于定义：
    - description: 技能描述
    - always: 是否始终加载到上下文
    - requires: 依赖要求（CLI 工具、环境变量等）
    """

    def __init__(self, workspace: Path, builtin_skills_dir: Path | None = None):
        """
        初始化技能加载器。

        Args:
            workspace: 工作空间路径
            builtin_skills_dir: 内置技能目录（可选，默认使用系统内置目录）
        """
        self.workspace = workspace
        self.workspace_skills = workspace / "skills"
        self.builtin_skills = builtin_skills_dir or BUILTIN_SKILLS_DIR

    def list_skills(self, filter_unavailable: bool = True) -> list[dict[str, str]]:
        """
        列出所有可用的技能。

        技能按优先级排序：工作空间技能优先于内置技能。
        同名技能只保留优先级高的版本。

        Args:
            filter_unavailable: 是否过滤掉依赖未满足的技能

        Returns:
            技能信息列表，每个元素包含 'name', 'path', 'source'
        """
        skills = []

        # 工作空间技能（最高优先级）
        if self.workspace_skills.exists():
            for skill_dir in self.workspace_skills.iterdir():
                if skill_dir.is_dir():
                    skill_file = skill_dir / "SKILL.md"
                    if skill_file.exists():
                        skills.append({"name": skill_dir.name, "path": str(skill_file), "source": "workspace"})

        # 内置技能
        if self.builtin_skills and self.builtin_skills.exists():
            for skill_dir in self.builtin_skills.iterdir():
                if skill_dir.is_dir():
                    skill_file = skill_dir / "SKILL.md"
                    # 只添加未被工作空间覆盖的技能
                    if skill_file.exists() and not any(s["name"] == skill_dir.name for s in skills):
                        skills.append({"name": skill_dir.name, "path": str(skill_file), "source": "builtin"})

        # 根据依赖过滤
        if filter_unavailable:
            return [s for s in skills if self._check_requirements(self._get_skill_meta(s["name"]))]
        return skills

    def load_skill(self, name: str) -> str | None:
        """
        按名称加载技能内容。

        优先从工作空间加载，如果不存在则从内置技能加载。

        Args:
            name: 技能名称（目录名）

        Returns:
            技能内容，如果不存在则返回 None
        """
        # 优先检查工作空间
        workspace_skill = self.workspace_skills / name / "SKILL.md"
        if workspace_skill.exists():
            return workspace_skill.read_text(encoding="utf-8")

        # 检查内置技能
        if self.builtin_skills:
            builtin_skill = self.builtin_skills / name / "SKILL.md"
            if builtin_skill.exists():
                return builtin_skill.read_text(encoding="utf-8")

        return None

    def load_skills_for_context(self, skill_names: list[str]) -> str:
        """
        加载指定技能用于注入到 agent 上下文。

        该方法用于加载标记为 always=true 的技能，
        它们会自动包含在系统提示中。

        Args:
            skill_names: 技能名称列表

        Returns:
            格式化的技能内容，用分隔符连接
        """
        parts = []
        for name in skill_names:
            content = self.load_skill(name)
            if content:
                # 移除前置元数据
                content = self._strip_frontmatter(content)
                parts.append(f"### Skill: {name}\n\n{content}")

        return "\n\n---\n\n".join(parts) if parts else ""

    def build_skills_summary(self) -> str:
        """
        构建所有技能的摘要（名称、描述、路径、可用性）。

        该摘要用于渐进式加载 - agent 可以通过 read_file 工具
        按需读取完整的技能内容。

        Returns:
            XML 格式的技能摘要
        """
        all_skills = self.list_skills(filter_unavailable=False)
        if not all_skills:
            return ""

        def escape_xml(s: str) -> str:
            """转义 XML 特殊字符。"""
            return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        lines = ["<skills>"]
        for s in all_skills:
            name = escape_xml(s["name"])
            path = s["path"]
            desc = escape_xml(self._get_skill_description(s["name"]))
            skill_meta = self._get_skill_meta(s["name"])
            available = self._check_requirements(skill_meta)

            lines.append(f"  <skill available=\"{str(available).lower()}\">")
            lines.append(f"    <name>{name}</name>")
            lines.append(f"    <description>{desc}</description>")
            lines.append(f"    <location>{path}</location>")

            # 显示不可用技能的缺失依赖
            if not available:
                missing = self._get_missing_requirements(skill_meta)
                if missing:
                    lines.append(f"    <requires>{escape_xml(missing)}</requires>")

            lines.append("  </skill>")
        lines.append("</skills>")

        return "\n".join(lines)

    def _get_missing_requirements(self, skill_meta: dict) -> str:
        """
        获取缺失的依赖描述。

        Args:
            skill_meta: 技能元数据

        Returns:
            缺失依赖的描述字符串
        """
        missing = []
        requires = skill_meta.get("requires", {})
        # 检查 CLI 工具依赖
        for b in requires.get("bins", []):
            if not shutil.which(b):
                missing.append(f"CLI: {b}")
        # 检查环境变量依赖
        for env in requires.get("env", []):
            if not os.environ.get(env):
                missing.append(f"ENV: {env}")
        return ", ".join(missing)

    def _get_skill_description(self, name: str) -> str:
        """
        从前置元数据获取技能描述。

        Args:
            name: 技能名称

        Returns:
            技能描述，如果没有则返回技能名称
        """
        meta = self.get_skill_metadata(name)
        if meta and meta.get("description"):
            return meta["description"]
        return name  # 回退到技能名称

    def _strip_frontmatter(self, content: str) -> str:
        """
        移除 markdown 内容中的 YAML 前置元数据。

        前置元数据格式：
        ---
        key: value
        ---

        Args:
            content: 原始内容

        Returns:
            移除前置元数据后的内容
        """
        if content.startswith("---"):
            match = re.match(r"^---\n.*?\n---\n", content, re.DOTALL)
            if match:
                return content[match.end():].strip()
        return content

    def _parse_nanobot_metadata(self, raw: str) -> dict:
        """
        从前置元数据解析技能元数据 JSON。

        支持 nanobot 和 openclaw 两个键名。

        Args:
            raw: 元数据 JSON 字符串

        Returns:
            解析后的元数据字典
        """
        try:
            data = json.loads(raw)
            return data.get("nanobot", data.get("openclaw", {})) if isinstance(data, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}

    def _check_requirements(self, skill_meta: dict) -> bool:
        """
        检查技能的依赖是否满足。

        检查内容：
        - bins: 需要的 CLI 工具
        - env: 需要的环境变量

        Args:
            skill_meta: 技能元数据

        Returns:
            依赖是否全部满足
        """
        requires = skill_meta.get("requires", {})
        # 检查 CLI 工具
        for b in requires.get("bins", []):
            if not shutil.which(b):
                return False
        # 检查环境变量
        for env in requires.get("env", []):
            if not os.environ.get(env):
                return False
        return True

    def _get_skill_meta(self, name: str) -> dict:
        """
        获取技能的 nanobot 元数据。

        元数据缓存在前置元数据的 metadata 字段中。

        Args:
            name: 技能名称

        Returns:
            解析后的元数据字典
        """
        meta = self.get_skill_metadata(name) or {}
        return self._parse_nanobot_metadata(meta.get("metadata", ""))

    def get_always_skills(self) -> list[str]:
        """
        获取标记为 always=true 且依赖满足的技能。

        这些技能会自动加载到 agent 的上下文中。

        Returns:
            技能名称列表
        """
        result = []
        for s in self.list_skills(filter_unavailable=True):
            meta = self.get_skill_metadata(s["name"]) or {}
            skill_meta = self._parse_nanobot_metadata(meta.get("metadata", ""))
            # 检查 always 标记（支持两种位置）
            if skill_meta.get("always") or meta.get("always"):
                result.append(s["name"])
        return result

    def get_skill_metadata(self, name: str) -> dict | None:
        """
        从技能的前置元数据获取元数据。

        前置元数据是 YAML 格式，位于文件开头。

        Args:
            name: 技能名称

        Returns:
            元数据字典，如果不存在则返回 None
        """
        content = self.load_skill(name)
        if not content:
            return None

        if content.startswith("---"):
            match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
            if match:
                # 简单的 YAML 解析（仅支持 key: value 格式）
                metadata = {}
                for line in match.group(1).split("\n"):
                    if ":" in line:
                        key, value = line.split(":", 1)
                        metadata[key.strip()] = value.strip().strip('"\'')
                return metadata

        return None
