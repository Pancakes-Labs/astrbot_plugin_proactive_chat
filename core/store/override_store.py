"""会话差异配置存储。

由旧 core/session_override_manager.py 等价搬迁而来。
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

from astrbot.api import logger


class OverrideStore:
    """负责会话级差异配置的加载、存储与合并。"""

    OVERRIDES_FILE = "session_overrides.json"

    # 仅允许会话覆写这些根字段，避免污染全局配置
    ALLOWED_ROOT_KEYS = {
        "enable",
        "session_name",
        "proactive_prompt",
        "context_settings",
        "auto_trigger_settings",
        "schedule_settings",
        "tts_settings",
        "segmented_reply_settings",
        "group_idle_trigger_minutes",
    }

    def __init__(self, storage_dir: Path):
        self.storage_dir = Path(storage_dir)
        self.overrides_file = self.storage_dir / self.OVERRIDES_FILE
        self._overrides: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._load()

    def _ensure_storage_dir(self) -> None:
        """确保存储目录存在。"""
        self.storage_dir.mkdir(parents=True, exist_ok=True)

    def _load(self) -> None:
        """同步加载差异配置文件；文件缺失或损坏时回退为空配置。"""
        self._ensure_storage_dir()

        if not self.overrides_file.exists():
            self._overrides = {}
            return

        try:
            with self.overrides_file.open("r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                self._overrides = {
                    str(k): v for k, v in raw.items() if isinstance(v, dict)
                }
            else:
                self._overrides = {}
        except Exception as e:
            logger.warning(f"[主动消息] 读取会话差异配置失败喵，将使用空配置: {e}")
            self._overrides = {}

    async def _save(self) -> None:
        """异步保存会话差异配置到磁盘，避免阻塞事件循环。"""
        self._ensure_storage_dir()
        temp_file = self.overrides_file.with_suffix(self.overrides_file.suffix + ".tmp")
        snap_overrides = copy.deepcopy(self._overrides)

        def _do_write():
            with temp_file.open("w", encoding="utf-8") as f:
                json.dump(snap_overrides, f, ensure_ascii=False, indent=2)
            temp_file.replace(self.overrides_file)

        try:
            await asyncio.to_thread(_do_write)
        except Exception as e:
            logger.error(f"[主动消息] 保存会话差异配置失败喵: {e}")
            try:
                if temp_file.exists():
                    temp_file.unlink()
            except Exception:
                pass

    def list_sessions(self) -> list[str]:
        """返回所有存在差异配置的会话标识（已排序）。"""
        return sorted(self._overrides.keys())

    def get_override(self, session_id: str) -> dict[str, Any]:
        """读取会话差异配置（返回深拷贝，调用方修改不影响内部状态）。"""
        return copy.deepcopy(self._overrides.get(session_id, {}))

    async def set_override(
        self, session_id: str, override_patch: dict[str, Any]
    ) -> None:
        """设置会话差异配置；清理后为空则等同于删除该会话配置。"""
        if not isinstance(override_patch, dict):
            raise ValueError("override_patch 必须是对象")

        patch = self._sanitize_patch(copy.deepcopy(override_patch))
        async with self._lock:
            if patch:
                self._overrides[session_id] = patch
            else:
                self._overrides.pop(session_id, None)

            await self._save()

    async def delete_override(self, session_id: str) -> None:
        """删除指定会话的差异配置。"""
        async with self._lock:
            self._overrides.pop(session_id, None)
            await self._save()

    def get_effective(
        self, session_id: str, base_config: dict[str, Any] | None
    ) -> dict[str, Any]:
        """把全局基础配置与会话差异配置深合并，得到最终生效配置。"""
        base = copy.deepcopy(base_config or {})
        override = self._overrides.get(session_id, {})
        return self.deep_merge(base, override)

    async def update_session_from_effective(
        self,
        session_id: str,
        base_config: dict[str, Any],
        effective_config: dict[str, Any],
    ) -> None:
        """由前端提交的最终生效配置反推差异配置并保存。"""
        if not isinstance(effective_config, dict):
            raise ValueError("effective_config 必须是对象")

        sanitized_base = self._sanitize_patch(copy.deepcopy(base_config)) or {}
        sanitized_effective = (
            self._sanitize_patch(copy.deepcopy(effective_config)) or {}
        )

        patch = self.compute_diff(sanitized_base, sanitized_effective)
        patch = self._sanitize_patch(patch)
        await self.set_override(session_id, patch or {})

    @classmethod
    def deep_merge(cls, base: Any, patch: Any) -> Any:
        """递归合并两个结构；非字典值以 patch 覆盖 base。"""
        if isinstance(base, dict) and isinstance(patch, dict):
            merged = copy.deepcopy(base)
            for key, value in patch.items():
                if key in merged:
                    merged[key] = cls.deep_merge(merged[key], value)
                else:
                    merged[key] = copy.deepcopy(value)
            return merged

        return copy.deepcopy(patch)

    @classmethod
    def compute_diff(cls, default_obj: Any, target_obj: Any) -> Any:
        """计算 target 相对 default 的差异；无差异时返回 None。"""
        if isinstance(default_obj, dict) and isinstance(target_obj, dict):
            result: dict[str, Any] = {}
            for key, value in target_obj.items():
                if key not in default_obj:
                    result[key] = copy.deepcopy(value)
                    continue

                diff_val = cls.compute_diff(default_obj[key], value)
                if diff_val is not None:
                    result[key] = diff_val

            return result if result else None

        if default_obj != target_obj:
            return copy.deepcopy(target_obj)

        return None

    def _sanitize_patch(self, patch: Any, depth: int = 0) -> Any:
        """清洗差异补丁：仅保留白名单根字段并剔除空子树。"""
        if patch is None:
            return None

        if not isinstance(patch, dict):
            return patch

        sanitized: dict[str, Any] = {}
        for key, value in patch.items():
            if depth == 0 and key not in self.ALLOWED_ROOT_KEYS:
                continue
            child = self._sanitize_patch(value, depth + 1)
            if isinstance(child, dict) and not child:
                continue
            if child is not None:
                sanitized[key] = child

        return sanitized
