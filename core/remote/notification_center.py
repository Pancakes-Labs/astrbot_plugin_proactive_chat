"""通知中心。

由旧 core/notification_center.py 等价搬迁而来。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import aiofiles
import aiofiles.os as aio_os

from astrbot.api import logger

from ...utils.version_utils import get_plugin_version


class NotificationCenter:
    """负责远端通知拉取、本地缓存与已读状态维护。"""

    # 远端通知服务的基地址与应用标识。
    NOTIFICATION_BASE_URL = "https://plugincenter.aloys23.link"
    NOTIFICATION_APP_SLUG = "f1b3592e-be98-4398-a3be-596e4a1ea90f"

    def __init__(self, plugin: Any):
        self.plugin = plugin
        self.config = plugin.config
        self.data_dir = Path(plugin.data_dir)
        # 本地缓存文件：保存通知列表、已读映射与最近同步时间。
        self.cache_file = self.data_dir / "notifications_cache.json"
        # 保护 _cache 的读写锁。
        self._lock = asyncio.Lock()
        # 后台轮询任务句柄。
        self._poll_task: asyncio.Task | None = None
        # 防止并发的远端同步。
        self._sync_in_progress = False
        self._sync_state_lock = asyncio.Lock()
        # 内存缓存结构：last_sync_at 时间戳、items 通知列表、read_map 已读映射。
        self._cache: dict[str, Any] = {
            "last_sync_at": None,
            "items": [],
            "read_map": {},
        }

    def _get_settings(self) -> dict[str, Any]:
        return dict(self.config.get("notification_settings", {}))

    def is_enabled(self) -> bool:
        settings = self._get_settings()
        return bool(settings.get("enabled", True))

    async def _build_remote_url(self) -> str:
        base_url = self.NOTIFICATION_BASE_URL.strip().rstrip("/")
        app_slug = self.NOTIFICATION_APP_SLUG.strip().strip("/")
        if not base_url or not app_slug:
            return ""

        plugin_version = await self._get_plugin_version()
        query = urlencode({"plugin_version": plugin_version})
        return f"{base_url}/api/v1/{app_slug}/notifications/updates?{query}"

    async def _get_plugin_version(self) -> str:
        plugin_version = (
            getattr(self.plugin, "version", None)
            or getattr(self.plugin, "__version__", None)
            or get_plugin_version(default="0.0.0", strip_v_prefix=True)
        )
        normalized = str(plugin_version).strip().lstrip("vV")
        return normalized or "0.0.0"

    def _get_poll_interval_seconds(self) -> int:
        settings = self._get_settings()
        value = settings.get("poll_interval_seconds", 300)
        try:
            seconds = int(value)
        except (TypeError, ValueError):
            seconds = 300
        return max(30, seconds)

    async def load_cache(self) -> None:
        """加锁读取本地缓存文件到内存。"""
        async with self._lock:
            await self._load_cache_locked()

    async def _load_cache_locked(self) -> None:
        try:
            await aio_os.stat(self.cache_file)
        except FileNotFoundError:
            self._cache = {
                "last_sync_at": None,
                "items": [],
                "read_map": {},
            }
            return

        try:
            async with aiofiles.open(self.cache_file, encoding="utf-8") as f:
                content = await f.read()
            payload = await asyncio.to_thread(json.loads, content)
            if not isinstance(payload, dict):
                raise ValueError("通知缓存文件格式无效")
            self._cache = {
                "last_sync_at": payload.get("last_sync_at"),
                "items": payload.get("items")
                if isinstance(payload.get("items"), list)
                else [],
                "read_map": payload.get("read_map")
                if isinstance(payload.get("read_map"), dict)
                else {},
            }
        except Exception as e:
            logger.warning(f"[主动消息] 读取通知缓存失败喵: {e}，将使用空缓存继续。")
            self._cache = {
                "last_sync_at": None,
                "items": [],
                "read_map": {},
            }

    async def save_cache(self) -> None:
        """加锁把内存缓存写回本地文件。"""
        async with self._lock:
            await self._save_cache_locked()

    async def _save_cache_locked(self) -> None:
        try:
            await aio_os.makedirs(self.data_dir, exist_ok=True)
            async with aiofiles.open(self.cache_file, "w", encoding="utf-8") as f:
                content = await asyncio.to_thread(
                    json.dumps, self._cache, indent=4, ensure_ascii=False
                )
                await f.write(content)
        except Exception as e:
            logger.warning(f"[主动消息] 保存通知缓存失败喵: {e}")

    def _normalize_item(self, raw: Any) -> dict[str, Any] | None:
        """把远端返回的单条通知规范化；字段缺失或非法时返回 None 丢弃。"""
        if not isinstance(raw, dict):
            return None

        # 必填字段校验：任一缺失即视为无效条目。
        required_keys = {"id", "title", "content", "type", "created_at", "is_active"}
        if not required_keys.issubset(raw.keys()):
            return None

        try:
            notification_id = int(raw.get("id"))
        except (TypeError, ValueError):
            return None

        title = str(raw.get("title", "")).strip()
        content = str(raw.get("content", "")).strip()
        notification_type = str(raw.get("type", "")).strip().upper()
        created_at = str(raw.get("created_at", "")).strip()
        is_active = raw.get("is_active")
        # content_format 归一化：仅保留 text / markdown 两种取值。
        content_format = str(raw.get("content_format", "text")).strip().lower()
        if content_format in {"plain", "plaintext"}:
            content_format = "text"
        if content_format not in {"text", "markdown"}:
            content_format = "text"

        if (
            not title
            or not content
            or not notification_type
            or not isinstance(is_active, bool)
        ):
            return None

        try:
            normalized_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError:
            return None

        return {
            "id": notification_id,
            "app_id": raw.get("app_id"),
            "title": title,
            "content": content,
            "type": notification_type,
            "created_at": normalized_dt.astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "is_active": is_active,
            "content_format": content_format,
        }

    def _sort_items(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(
            items,
            key=lambda item: (item.get("created_at", ""), item.get("id", 0)),
            reverse=True,
        )

    async def _fetch_remote_items(self) -> list[dict[str, Any]]:
        """拉取远端通知并过滤出有效且启用中的条目。"""
        url = await self._build_remote_url()
        if not url:
            return []

        def _request() -> list[dict[str, Any]]:
            # 同步阻塞请求在独立线程中执行，避免阻塞事件循环。
            request = Request(
                url,
                method="GET",
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/133.0.0.0 Safari/537.36"
                    ),
                },
            )
            with urlopen(request, timeout=10) as response:
                body = response.read().decode("utf-8")
            payload = json.loads(body)
            if not isinstance(payload, list):
                raise ValueError("通知接口返回体不是数组")
            return payload

        try:
            raw_items = await asyncio.to_thread(_request)
        except HTTPError as e:
            raise RuntimeError(f"通知接口请求失败，HTTP {e.code}") from e
        except URLError as e:
            raise RuntimeError(f"通知接口连接失败: {e.reason}") from e
        except TimeoutError as e:
            raise RuntimeError("通知接口请求超时") from e

        # 逐条规范化并丢弃无效/已停用条目。
        normalized_items = []
        for raw in raw_items:
            item = self._normalize_item(raw)
            if not item:
                continue
            if not item.get("is_active", False):
                continue
            normalized_items.append(item)
        return self._sort_items(normalized_items)

    def _items_signature(self, items: list[dict[str, Any]]) -> str:
        try:
            return json.dumps(items, ensure_ascii=False, sort_keys=True)
        except TypeError as e:
            logger.warning(f"[主动消息] 创建通知签名时遇到不可序列化的项喵: {e}")
            return str(items)

    def _build_meta_locked(self) -> dict[str, Any]:
        """构建通知元信息（未读数、最近同步时间、总数）。调用方需持锁。"""
        unread_count = 0
        read_map = self._cache.get("read_map", {})
        for item in self._cache.get("items", []):
            if not read_map.get(str(item.get("id")), False):
                unread_count += 1
        return {
            "unread_count": unread_count,
            "last_sync_at": self._cache.get("last_sync_at"),
            "total_count": len(self._cache.get("items", [])),
        }

    async def get_meta(self) -> dict[str, Any]:
        async with self._lock:
            return self._build_meta_locked()

    async def get_payload(self) -> dict[str, Any]:
        async with self._lock:
            read_map = self._cache.get("read_map", {})
            items = [
                {
                    **item,
                    "_read": bool(read_map.get(str(item.get("id")), False)),
                }
                for item in self._cache.get("items", [])
            ]
            return {
                "items": items,
                "meta": self._build_meta_locked(),
            }

    async def mark_as_read(self, notification_id: int) -> dict[str, Any]:
        async with self._lock:
            self._cache.setdefault("read_map", {})[str(notification_id)] = True
            await self._save_cache_locked()
            return {
                "ok": True,
                "id": notification_id,
                "meta": self._build_meta_locked(),
            }

    async def mark_all_as_read(self) -> dict[str, Any]:
        async with self._lock:
            read_map = self._cache.setdefault("read_map", {})
            for item in self._cache.get("items", []):
                read_map[str(item.get("id"))] = True
            await self._save_cache_locked()
            return {
                "ok": True,
                "meta": self._build_meta_locked(),
            }

    async def refresh(self) -> bool:
        """拉取远端通知并与本地缓存合并，返回内容是否发生变化。

        并发调用时后到者直接返回 False；已读映射只保留仍存在的通知 ID。
        """
        async with self._sync_state_lock:
            if self._sync_in_progress:
                return False
            self._sync_in_progress = True

        try:
            remote_items = await self._fetch_remote_items()
            async with self._lock:
                old_signature = self._items_signature(self._cache.get("items", []))
                new_signature = self._items_signature(remote_items)
                changed = old_signature != new_signature

                current_read_map = self._cache.setdefault("read_map", {})
                active_ids = {str(item.get("id")) for item in remote_items}
                self._cache["read_map"] = {
                    key: value
                    for key, value in current_read_map.items()
                    if key in active_ids
                }
                self._cache["items"] = remote_items
                self._cache["last_sync_at"] = (
                    datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                )
                await self._save_cache_locked()
                return changed
        except Exception as e:
            logger.warning(f"[主动消息] 同步远端通知失败喵: {e} (可忽略)")
            return False
        finally:
            async with self._sync_state_lock:
                self._sync_in_progress = False

    async def start(self) -> None:
        """加载缓存并启动通知轮询任务。"""
        await self.load_cache()
        if not self.is_enabled():
            logger.info("[主动消息] 通知系统未启用或配置不完整喵。")
            return

        changed = await self.refresh()
        if getattr(self.plugin, "web_admin_server", None):
            if changed:
                await self.plugin.web_admin_server._broadcast_update("notifications")
            else:
                await self.plugin.web_admin_server._broadcast_notification_meta_update(
                    "notifications-meta"
                )

        async def _poll_loop() -> None:
            # 周期性同步远端通知，并按变更情况广播给 Web 端。
            while True:
                try:
                    await asyncio.sleep(self._get_poll_interval_seconds())
                    changed = await self.refresh()
                    if getattr(self.plugin, "web_admin_server", None):
                        if changed:
                            await self.plugin.web_admin_server._broadcast_update(
                                "notifications"
                            )
                        else:
                            await self.plugin.web_admin_server._broadcast_notification_meta_update(
                                "notifications-meta"
                            )
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.warning(f"[主动消息] 通知轮询任务异常喵: {e} (可忽略)")

        self._poll_task = asyncio.create_task(_poll_loop())
        logger.info("[主动消息] 通知系统已启动喵。")

    async def stop(self) -> None:
        """停止轮询任务并保存缓存。"""
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except Exception:
                pass
            self._poll_task = None
        await self.save_cache()
        logger.info("[主动消息] 通知系统已停止喵。")
