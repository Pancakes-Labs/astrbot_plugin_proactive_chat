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

        # 结构合并涉及跨分组取并集与冲突裁决，迁移前先备份原始配置，
        # 万一日志与判断有出入，用户仍能从快照里找回原始值。
        # 备份失败时不继续迁移：旧分组会被置空，失败等于永久丢数据。
        if not self._backup_config_before_migration(config):
            logger.error(
                "[主动消息] 迁移前备份旧配置失败，已中止本次结构迁移以避免数据丢失喵。"
                "请检查数据目录写入权限后重载插件喵。"
            )
            return

        # 顺序至关重要：迁移必须先于默认值补齐。
        # 若先补齐，模板列表会被填成空列表，迁移器据此判定"已迁移"，
        # 导致旧提示词永远得不到留档（会话覆写不走补齐，形态会不一致）。
        changed = config_migrator.migrate_config(config)

        defaults = self._feature_defaults
        added: list[str] = []
        if defaults:
            added = self._apply_defaults(config, defaults)

        store = getattr(plugin, "session_override_manager", None)
        migrated_sessions = await config_migrator.migrate_override_store(store)
        # 迁移期保留了“原本属于被关闭一侧”的会话清单，
        # 需在覆写存储可用后立刻实体化为关闭型覆写，保持旧行为等价。
        disabled_overrides = await self.retry_pending_disabled_overrides()
        if disabled_overrides:
            logger.info(
                f"[主动消息] 已为 {disabled_overrides} 个会话写入单独关闭的覆写喵。"
            )
            changed = True

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

    async def retry_pending_disabled_overrides(self) -> int:
        """消费「等待单独关闭」清单，返回本次成功写入的数量。

        清单里都是完整 UMO，配置装配后即可一次写完；
        写入失败的条目会留在清单中，在后续启动流程里再次重试。
        """
        plugin = self.plugin
        config = plugin.config
        if not isinstance(config, dict):
            return 0
        if not config.get("_pending_disabled_overrides"):
            return 0
        try:
            return await config_migrator.apply_pending_disabled_overrides(
                getattr(plugin, "session_override_manager", None),
                config,
            )
        except Exception as e:
            logger.warning(f"[主动消息] 写入关闭型覆写失败喵: {e}")
            return 0

    # 备份文件名沿用 v1.2 之前版本的约定，便于老用户凭直觉找到。
    CONFIG_SNAPSHOT_FILE = "user_config_snapshot.json"
    PROMPTS_COLLECTION_FILE = "prompts_collection.md"

    def _backup_config_before_migration(self, config: dict[str, Any]) -> bool:
        """结构迁移前备份原始配置与提示词。

        Returns:
            True 表示「可以继续迁移」：包括无需迁移（无旧结构）的情况。
            只有在确实需要迁移、但备份写入失败时才返回 False。

        写两份文件：
        - user_config_snapshot.json：完整配置快照，用于精确回滚；
        - prompts_collection.md：提示词汇总，用于人眼快速核对。

        固定名文件若已存在则保留不动（不覆盖历史备份），但仍会为本次
        源配置另存一份带时间戳的快照：文件存在不能证明当前配置已备份，
        否则用户在上次迁移后新增的提示词不会进入任何备份。
        """
        from ..domain.config_keys import (
            LEGACY_FRIEND_SETTINGS_KEY,
            LEGACY_GROUP_SETTINGS_KEY,
        )

        has_legacy = any(
            isinstance(config.get(key), dict) and config.get(key)
            for key in (LEGACY_FRIEND_SETTINGS_KEY, LEGACY_GROUP_SETTINGS_KEY)
        )
        if not has_legacy:
            return True

        data_dir = getattr(self.plugin, "data_dir", None)
        if data_dir is None:
            return True

        try:
            import json
            from datetime import datetime
            from pathlib import Path

            root = Path(data_dir)
            root.mkdir(parents=True, exist_ok=True)
            config_text = json.dumps(dict(config), ensure_ascii=False, indent=4)
            prompts_text = self._build_prompts_collection(config)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

            # 固定名保留首份备份，便于老用户按旧约定查找。
            snapshot_path = root / self.CONFIG_SNAPSHOT_FILE
            if not snapshot_path.exists():
                snapshot_path.write_text(config_text, encoding="utf-8")

            prompts_path = root / self.PROMPTS_COLLECTION_FILE
            if not prompts_path.exists():
                prompts_path.write_text(prompts_text, encoding="utf-8")

            # 本次源配置的独立快照：每次都写，确保当前内容必有备份。
            (root / f"user_config_snapshot.{stamp}.json").write_text(
                config_text, encoding="utf-8"
            )
            (root / f"prompts_collection.{stamp}.md").write_text(
                prompts_text, encoding="utf-8"
            )

            logger.info(f"[主动消息] 已在迁移前备份配置快照与提示词汇总到 {root} 喵。")
            return True
        except Exception as e:
            logger.warning(f"[主动消息] 备份旧配置失败喵: {e}")
            return False

    @staticmethod
    def _build_prompts_collection(config: dict[str, Any]) -> str:
        """把旧配置里的提示词汇总成一份 Markdown，便于人工核对待迁移内容。"""
        from ..domain.config_keys import (
            LEGACY_FRIEND_SETTINGS_KEY,
            LEGACY_GROUP_SETTINGS_KEY,
        )

        label_map = {
            LEGACY_FRIEND_SETTINGS_KEY: "私聊",
            LEGACY_GROUP_SETTINGS_KEY: "群聊",
        }

        lines: list[str] = ["# 主动消息 Prompt 汇总备份", ""]

        for key, label in label_map.items():
            section = config.get(key)
            if not isinstance(section, dict):
                continue

            legacy_prompt = section.get("proactive_prompt")
            if isinstance(legacy_prompt, str) and legacy_prompt.strip():
                lines.extend(
                    [
                        f"## {label}主动消息提示词",
                        "",
                        "```text",
                        legacy_prompt,
                        "```",
                        "",
                    ]
                )

            templates = section.get("prompt_templates")
            if isinstance(templates, list):
                for index, template in enumerate(templates, start=1):
                    if not isinstance(template, dict):
                        continue
                    text = template.get("text")
                    if not isinstance(text, str) or not text.strip():
                        continue
                    name = template.get("name") or f"模板 {index}"
                    lines.extend(
                        [f"## {label}模板：{name}", "", "```text", text, "```", ""]
                    )

            context_settings = section.get("context_settings")
            if isinstance(context_settings, dict):
                history_prompt = context_settings.get("platform_history_prompt")
                if isinstance(history_prompt, str) and history_prompt.strip():
                    lines.extend(
                        [
                            f"## {label}平台流水提示词",
                            "",
                            "```text",
                            history_prompt,
                            "```",
                            "",
                        ]
                    )

        return "\n".join(lines)

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
