"""确定性检测器。Runtime 用它们产生 L0 硬信号，全程不经 LLM。

故意不引 jsonschema：v0.1 只需要 type / required / properties 三件事，
自带一个 60 行的校验器比多一个依赖更划算。
"""

from __future__ import annotations

from typing import Any

_PY_TYPES: dict[str, type | tuple[type, ...]] = {
    "object": dict,
    "array": list,
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "null": type(None),
}


def validate_schema(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """返回错误列表；空列表表示通过。非空 -> VALIDATION_FAILED。"""
    if not schema:
        return []
    errors: list[str] = []

    expected = schema.get("type")
    if expected:
        py = _PY_TYPES.get(expected)
        if py is None:
            errors.append(f"{path}: 未知的 schema type {expected!r}")
            return errors
        # bool 是 int 的子类，单独挡掉
        if expected in ("integer", "number") and isinstance(value, bool):
            errors.append(f"{path}: 期望 {expected}，实际 boolean")
            return errors
        if not isinstance(value, py):
            errors.append(f"{path}: 期望 {expected}，实际 {type(value).__name__}")
            return errors

    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}.{key}: 缺少必填字段")
        for key, sub in schema.get("properties", {}).items():
            if key in value:
                errors.extend(validate_schema(value[key], sub, f"{path}.{key}"))

    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            errors.extend(validate_schema(item, schema["items"], f"{path}[{i}]"))

    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} 不在 enum {schema['enum']} 中")

    return errors
