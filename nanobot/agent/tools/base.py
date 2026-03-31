"""
Agent 工具基类模块，定义了所有工具的抽象接口。

该模块提供了工具系统的核心抽象：
- Tool: 工具基类，定义了工具必须实现的接口
- 参数类型转换和验证
- JSON Schema 到 OpenAI 格式的转换

工具是 agent 与外部环境交互的主要方式。每个工具需要：
1. 定义名称、描述和参数模式
2. 实现异步执行方法
3. 支持参数验证和类型转换
"""

from abc import ABC, abstractmethod
from typing import Any


class Tool(ABC):
    """
    Agent 工具的抽象基类。

    工具是 agent 可以使用的能力，用于与环境交互。
    例如：读取文件、执行命令、搜索网络等。

    每个工具需要实现：
    - name: 工具名称（在函数调用中使用）
    - description: 工具描述（帮助 LLM 理解用途）
    - parameters: 参数的 JSON Schema
    - execute: 异步执行方法

    工具还提供：
    - 参数类型转换（cast_params）
    - 参数验证（validate_params）
    - OpenAI 格式转换（to_schema）
    """

    # JSON Schema 类型到 Python 类型的映射
    _TYPE_MAP = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "array": list,
        "object": dict,
    }

    @staticmethod
    def _resolve_type(t: Any) -> str | None:
        """
        将 JSON Schema 类型解析为简单字符串。

        JSON Schema 允许联合类型，如 "type": ["string", "null"]。
        该方法提取第一个非 null 类型，以便验证和类型转换正常工作。

        Args:
            t: JSON Schema 类型值

        Returns:
            解析后的类型字符串，如果无法解析则返回 None
        """
        if isinstance(t, list):
            for item in t:
                if item != "null":
                    return item
            return None
        return t

    @property
    @abstractmethod
    def name(self) -> str:
        """
        工具名称，用于函数调用。

        Returns:
            工具名称字符串
        """
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        """
        工具描述，帮助 LLM 理解工具的用途。

        Returns:
            工具描述字符串
        """
        pass

    @property
    @abstractmethod
    def parameters(self) -> dict[str, Any]:
        """
        工具参数的 JSON Schema。

        定义了工具接受的参数结构、类型和约束。

        Returns:
            JSON Schema 字典
        """
        pass

    @abstractmethod
    async def execute(self, **kwargs: Any) -> Any:
        """
        异步执行工具。

        Args:
            **kwargs: 工具特定的参数

        Returns:
            工具执行结果（字符串或内容块列表）
        """
        pass

    def cast_params(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        根据模式进行安全的类型转换。

        LLM 可能返回字符串形式的数字或布尔值，
        该方法根据 JSON Schema 将它们转换为正确的类型。

        Args:
            params: 原始参数字典

        Returns:
            类型转换后的参数字典
        """
        schema = self.parameters or {}
        if schema.get("type", "object") != "object":
            return params

        return self._cast_object(params, schema)

    def _cast_object(self, obj: Any, schema: dict[str, Any]) -> dict[str, Any]:
        """
        根据模式转换对象（字典）。

        Args:
            obj: 要转换的对象
            schema: JSON Schema

        Returns:
            转换后的对象
        """
        if not isinstance(obj, dict):
            return obj

        props = schema.get("properties", {})
        result = {}

        for key, value in obj.items():
            if key in props:
                result[key] = self._cast_value(value, props[key])
            else:
                result[key] = value

        return result

    def _cast_value(self, val: Any, schema: dict[str, Any]) -> Any:
        """
        根据模式转换单个值。

        支持的类型转换：
        - 字符串到整数/浮点数
        - 字符串到布尔值
        - 嵌套对象和数组

        Args:
            val: 要转换的值
            schema: JSON Schema

        Returns:
            转换后的值
        """
        target_type = self._resolve_type(schema.get("type"))

        # 已经是正确类型的值
        if target_type == "boolean" and isinstance(val, bool):
            return val
        if target_type == "integer" and isinstance(val, int) and not isinstance(val, bool):
            return val
        if target_type in self._TYPE_MAP and target_type not in ("boolean", "integer", "array", "object"):
            expected = self._TYPE_MAP[target_type]
            if isinstance(val, expected):
                return val

        # 字符串到整数
        if target_type == "integer" and isinstance(val, str):
            try:
                return int(val)
            except ValueError:
                return val

        # 字符串到浮点数
        if target_type == "number" and isinstance(val, str):
            try:
                return float(val)
            except ValueError:
                return val

        # 任何值到字符串
        if target_type == "string":
            return val if val is None else str(val)

        # 字符串到布尔值
        if target_type == "boolean" and isinstance(val, str):
            val_lower = val.lower()
            if val_lower in ("true", "1", "yes"):
                return True
            if val_lower in ("false", "0", "no"):
                return False
            return val

        # 数组类型
        if target_type == "array" and isinstance(val, list):
            item_schema = schema.get("items")
            return [self._cast_value(item, item_schema) for item in val] if item_schema else val

        # 对象类型
        if target_type == "object" and isinstance(val, dict):
            return self._cast_object(val, schema)

        return val

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        """
        根据 JSON Schema 验证工具参数。

        Args:
            params: 要验证的参数字典

        Returns:
            错误列表（空列表表示验证通过）
        """
        if not isinstance(params, dict):
            return [f"parameters must be an object, got {type(params).__name__}"]
        schema = self.parameters or {}
        if schema.get("type", "object") != "object":
            raise ValueError(f"Schema must be object type, got {schema.get('type')!r}")
        return self._validate(params, {**schema, "type": "object"}, "")

    def _validate(self, val: Any, schema: dict[str, Any], path: str) -> list[str]:
        """
        递归验证值是否符合模式。

        Args:
            val: 要验证的值
            schema: JSON Schema
            path: 当前路径（用于错误消息）

        Returns:
            错误列表
        """
        raw_type = schema.get("type")
        nullable = (isinstance(raw_type, list) and "null" in raw_type) or schema.get(
            "nullable", False
        )
        t, label = self._resolve_type(raw_type), path or "parameter"

        # 允许 null 值
        if nullable and val is None:
            return []

        # 类型检查
        if t == "integer" and (not isinstance(val, int) or isinstance(val, bool)):
            return [f"{label} should be integer"]
        if t == "number" and (
            not isinstance(val, self._TYPE_MAP[t]) or isinstance(val, bool)
        ):
            return [f"{label} should be number"]
        if t in self._TYPE_MAP and t not in ("integer", "number") and not isinstance(val, self._TYPE_MAP[t]):
            return [f"{label} should be {t}"]

        errors = []

        # 枚举值检查
        if "enum" in schema and val not in schema["enum"]:
            errors.append(f"{label} must be one of {schema['enum']}")

        # 数值范围检查
        if t in ("integer", "number"):
            if "minimum" in schema and val < schema["minimum"]:
                errors.append(f"{label} must be >= {schema['minimum']}")
            if "maximum" in schema and val > schema["maximum"]:
                errors.append(f"{label} must be <= {schema['maximum']}")

        # 字符串长度检查
        if t == "string":
            if "minLength" in schema and len(val) < schema["minLength"]:
                errors.append(f"{label} must be at least {schema['minLength']} chars")
            if "maxLength" in schema and len(val) > schema["maxLength"]:
                errors.append(f"{label} must be at most {schema['maxLength']} chars")

        # 对象属性检查
        if t == "object":
            props = schema.get("properties", {})
            # 必需属性检查
            for k in schema.get("required", []):
                if k not in val:
                    errors.append(f"missing required {path + '.' + k if path else k}")
            # 递归验证属性
            for k, v in val.items():
                if k in props:
                    errors.extend(self._validate(v, props[k], path + "." + k if path else k))

        # 数组元素检查
        if t == "array" and "items" in schema:
            for i, item in enumerate(val):
                errors.extend(
                    self._validate(item, schema["items"], f"{path}[{i}]" if path else f"[{i}]")
                )

        return errors

    def to_schema(self) -> dict[str, Any]:
        """
        将工具转换为 OpenAI 函数模式格式。

        该格式用于 LLM 的函数调用 API。

        Returns:
            OpenAI 格式的工具模式字典
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
