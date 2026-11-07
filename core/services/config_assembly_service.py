"""配置装配服务。

把 Feature 自带的配置声明、_conf_schema.json 静态契约与用户现状三者对齐：
1. 汇总各 Feature 的 config_schema / default_config；
2. 与静态 schema 合并后写回 plugin.config.schema（供面板与配置页消费）；
3. 按合并结果补齐缺失配置项；
4. 执行一次性数据迁移（旧字符串提示词 -> 模板列表）。

注意 AstrBot 的 schema 是静态的：构造 AstrBotConfig 时就已确定，
运行期写回只影响展示与本次补齐，不替代在 _conf_schema.json 中声明字段。
Feature 声明了但静态 schema 缺失的键会在启动日志中告警。
"""

from __future__ import annotations

import copy
from typing import Any

from astrbot.api import logger

from ..pipeline.feature_registry import FeatureRegistry
from ..pipeline.schema_merge import (
    ensure_config_defaults,
    merge_defaults_into,
    merge_schema_into,
    split_path,
)
from . import config_migrator


def _lookup_path(source: dict[str, Any], dotted_key: str) -> Any:
    """按点号路径取值，任一层缺失即返回 None。"""
    current: Any = source
    for part in split_path(dotted_key):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


class ConfigAssemblyService:
    """负责启动期的配置装配与迁移。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        # Feature 声明的默认值，装配阶段填充，供后续补齐使用。
        self._feature_defaults: dict[str, Any] = {}

    async def assemble(self) -> None:
        """执行完整装配流程；异常只告警，不阻断插件启动。"""
        try:
            self._assemble_runtime_schema()
        except Exception as e:
            logger.warning(f"[主动消息] 配置 schema 装配失败喵: {e}")

        try:
            await self._migrate_runtime_config()
        except Exception as e:
            logger.warning(f"[主动消息] 配置迁移失败喵: {e}")

    # ------------------------------------------------------------------
    # schema 装配
    # ------------------------------------------------------------------
    def _collect_feature_config(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """构建一次流水线以取得全部 Feature 的配置声明。

        这里只做注册、不执行任何 Stage，开销仅为对象构造。
        """
        registry = FeatureRegistry(config=dict(self.plugin.config))
        registry.register_features(self.plugin.orchestrator.build_features())
        return registry.collected_config_schema(), registry.collected_default_config()

    def _assemble_runtime_schema(self) -> None:
        """合并 Feature 声明与静态 schema，并做一致性自检。"""
        declared, defaults = self._collect_feature_config()

        schema = getattr(self.plugin.config, "schema", None)
        if not isinstance(schema, dict):
            # 无 schema 时（如框架差异）只记录声明，不做写回。
            self._feature_defaults = defaults
            if declared:
                logger.debug(
                    f"[主动消息] 当前配置缺少 schema，仅保留 Feature 声明 "
                    f"({len(declared)} 项) 喵。"
                )
            return

        # 自检必须在合并前进行，否则缺失项会被合入结果掩盖。
        missing = self._find_missing_declarations(schema, declared)
        if missing:
            logger.warning(
                "[主动消息] 以下配置项由 Feature 声明但未写入配置文件，"
                f"重启时会被框架裁剪喵: {', '.join(missing)}"
            )

        merge_schema_into(schema, declared)

        self._feature_defaults = defaults
        if declared:
            logger.info(f"[主动消息] 已装配 {len(declared)} 项 Feature 配置声明喵。")

    @staticmethod
    def _find_missing_declarations(
        schema: dict[str, Any], declared: dict[str, Any]
    ) -> list[str]:
        """找出 Feature 已声明、但静态 schema 中不存在的点号路径。

        features 的声明已按点号路径合并过一层，这里只需要判断末级键是否存在。
        """
        missing: list[str] = []

        def _exists(container: dict[str, Any], parts: list[str]) -> bool:
            current: Any = container
            for index, part in enumerate(parts):
                if not isinstance(current, dict) or part not in current:
                    return False
                if index == len(parts) - 1:
                    return True
                node = current[part]
                if not isinstance(node, dict):
                    return False
                current = node.get("items", {})
            return False

        def _walk(node: Any, path: list[str]) -> None:
            if not isinstance(node, dict):
                return
            for key, value in node.items():
                full = path + [str(key)]
                if not isinstance(value, dict):
                    continue

                # 嵌套 object 声明：先确认容器本身存在，再逐个子字段下钻校验。
                if value.get("type") == "object" and isinstance(
                    value.get("items"), dict
                ):
                    if not _exists(schema, full):
                        missing.append(".".join(full))
                        continue
                    _walk(value["items"], full)
                    continue

                # 点号路径合并产生的中间层没有 type 标记，继续下钻。
                if "type" not in value:
                    _walk(value, full)
                    continue

                if not _exists(schema, full):
                    missing.append(".".join(full))

        _walk(declared, [])
        return missing

    # ------------------------------------------------------------------
    # 运行期迁移与补齐
    # ------------------------------------------------------------------
    async def _migrate_runtime_config(self) -> None:
        """迁移旧配置结构并补齐 Feature 声明的默认值。"""
        plugin = self.plugin
        config = plugin.config
        if not isinstance(config, dict):
            return

        # 顺序至关重要：迁移必须先于默认值补齐。
        # 若先补齐，prompt_templates 会被填成空列表，迁移器据此判定"已迁移"，
        # 导致旧 proactive_prompt 永远得不到留档（会话覆写不走补齐，形态会不一致）。
        changed = config_migrator.migrate_config(config)

        defaults = self._feature_defaults
        added: list[str] = []
        if defaults:
            added = self._apply_defaults(config, defaults)

        migrated_sessions = await config_migrator.migrate_override_store(
            getattr(plugin, "session_override_manager", None)
        )

        # 仅在确有变更时落盘，避免无谓地重写配置文件。
        if changed or added:
            try:
                save_async = getattr(config, "save_config_async", None)
                if callable(save_async):
                    await save_async()
                else:
                    config.save_config()
                # 迁移与补齐是两件事：分开记录，避免"补齐"被误读为"结构又升级了一次"。
                if changed:
                    logger.info("[主动消息] 配置结构已升级并保存喵。")
                if added:
                    logger.info(
                        f"[主动消息] 已为 {len(added)} 项缺失配置写入默认值并保存喵。"
                    )
                    logger.debug(f"[主动消息] 本次补齐的配置项喵: {', '.join(added)}")
            except Exception as e:
                logger.error(f"[主动消息] 保存升级后的配置失败喵: {e}")

        if migrated_sessions:
            logger.info(f"[主动消息] 已迁移 {migrated_sessions} 个会话的覆写配置喵。")

    def _apply_defaults(
        self, config: dict[str, Any], defaults: dict[str, Any]
    ) -> list[str]:
        """把 Feature 默认值合并进配置，返回补齐的点号路径。

        config 必须是原始对象本身（就地补齐），传拷贝会导致补齐结果丢失。
        """
        schema = getattr(config, "schema", None)
        if isinstance(schema, dict):
            # 有 schema 时以 schema 为准补齐：能覆盖嵌套分组里的全部字段。
            return ensure_config_defaults(config, schema)

        before = copy.deepcopy(dict(config))
        merge_defaults_into(config, defaults)
        # 只回报真正发生变化的键，避免"声明了但已存在"的项被重复计为补齐。
        return [
            key
            for key in defaults
            if _lookup_path(before, key) != _lookup_path(dict(config), key)
        ]


__all__ = ["ConfigAssemblyService"]
