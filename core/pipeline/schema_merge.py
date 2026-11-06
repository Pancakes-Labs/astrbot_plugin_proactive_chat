"""配置 schema 合并与默认值补齐工具。

Feature 通过点号路径声明附加配置（如 friend_settings.prompt_templates），
启动时由本模块把这些增量合并进 AstrBot 的静态 schema，并补齐缺失项。
"""

from __future__ import annotations

import copy
from typing import Any

# schema 未显式声明 default 时按类型回退空值，与 AstrBot 的约定保持一致。
_TYPE_DEFAULTS: dict[str, Any] = {
    "int": 0,
    "float": 0.0,
    "bool": False,
    "string": "",
    "text": "",
    "list": [],
    "file": [],
    "object": {},
    "template_list": [],
    "dict": {},
}


def split_path(key: Any) -> list[str]:
    """把 a.b.c 形式的键拆成路径段，空段会被丢弃。"""
    return [part for part in str(key).split(".") if part]


def _descend(container: dict[str, Any], part: str) -> dict[str, Any]:
    """进入 schema 中名为 part 的 object 节点，返回其 items 容器。

    不存在时按 AstrBot 的 object 声明结构就地补建，保证点号路径可以落地。
    """
    node = container.get(part)
    if not isinstance(node, dict):
        node = {"type": "object", "items": {}}
        container[part] = node

    items = node.get("items")
    if not isinstance(items, dict):
        items = {}
        node["items"] = items

    return items


def merge_schema_into(target: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """按点号路径把 patch 深度合并进 target（就地修改并返回 target）。

    已存在的节点以 target 为权威，只补缺失的元信息：
    静态 schema 里的描述、hint、隐藏标记等由维护者手写，不应被代码覆盖。
    """
    if not isinstance(target, dict) or not isinstance(patch, dict):
        return target

    for key, value in patch.items():
        parts = split_path(key)
        if not parts:
            continue

        container = target
        for part in parts[:-1]:
            container = _descend(container, part)

        leaf = parts[-1]
        existing = container.get(leaf)

        if existing is None:
            container[leaf] = copy.deepcopy(value)
            continue

        if not isinstance(existing, dict) or not isinstance(value, dict):
            # 类型冲突时保留已有声明，交由启动自检告警暴露出来。
            continue

        # 同为 object 声明时继续向 items 内部合并。
        if existing.get("type") == "object" and value.get("type") == "object":
            merge_schema_into(_descend(container, leaf), value.get("items") or {})

        for meta_key, meta_value in value.items():
            if meta_key == "items":
                continue
            existing.setdefault(meta_key, copy.deepcopy(meta_value))

    return target


def merge_defaults_into(
    target: dict[str, Any], patch: dict[str, Any]
) -> dict[str, Any]:
    """按点号路径合并默认值，已存在的键保持原值。"""
    if not isinstance(target, dict) or not isinstance(patch, dict):
        return target

    for key, value in patch.items():
        parts = split_path(key)
        if not parts:
            continue

        container = target
        for part in parts[:-1]:
            child = container.get(part)
            if not isinstance(child, dict):
                child = {}
                container[part] = child
            container = child

        leaf = parts[-1]
        current = container.get(leaf)
        if isinstance(current, dict) and isinstance(value, dict):
            merge_defaults_into(current, value)
            continue
        container.setdefault(leaf, copy.deepcopy(value))

    return target


def _default_for(meta: dict[str, Any]) -> Any:
    if "default" in meta:
        return copy.deepcopy(meta["default"])
    return copy.deepcopy(_TYPE_DEFAULTS.get(meta.get("type")))


def ensure_config_defaults(
    config: dict[str, Any], schema: dict[str, Any], path: str = ""
) -> list[str]:
    """按 schema 补齐缺失配置项，返回被补齐的点号路径列表。

    只补缺失与 None，不覆盖用户已有取值；object 节点会递归进入 items。
    """
    added: list[str] = []
    if not isinstance(config, dict) or not isinstance(schema, dict):
        return added

    for key, meta in schema.items():
        if not isinstance(meta, dict):
            continue

        key_path = f"{path}.{key}" if path else key

        if meta.get("type") == "object" and isinstance(meta.get("items"), dict):
            child = config.get(key)
            if not isinstance(child, dict):
                if child is not None and key in config:
                    # 存在但不合法的脏数据交给调用方处理，这里不强行覆盖。
                    continue
                child = {}
                config[key] = child
                added.append(key_path)
            added.extend(ensure_config_defaults(child, meta["items"], key_path))
            continue

        if key in config and config[key] is not None:
            continue

        config[key] = _default_for(meta)
        added.append(key_path)

    return added


__all__ = [
    "ensure_config_defaults",
    "merge_defaults_into",
    "merge_schema_into",
    "split_path",
]
