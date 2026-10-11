"""版本探测工具。

由旧 utils/version.py 等价搬迁而来，仅更名以符合命名规范。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import metadata as importlib_metadata
from pathlib import Path

import astrbot
from astrbot.api import logger

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None


@dataclass(frozen=True)
class AstrBotVersionInfo:
    """AstrBot 版本探测结果。"""

    version: str
    source: str
    error: str | None = None


def get_plugin_root() -> Path:
    """获取插件根目录（version_utils 位于 utils/ 下，故上溯两级）。"""
    return Path(__file__).resolve().parent.parent


def get_metadata_path() -> Path:
    """获取插件 metadata.yaml 路径。"""
    return get_plugin_root() / "metadata.yaml"


def get_plugin_version(default: str = "unknown", strip_v_prefix: bool = False) -> str:
    """通过读取插件根目录中的 metadata.yaml 获取插件版本号。"""
    try:
        metadata_path = get_metadata_path()
        if metadata_path.exists():
            with open(metadata_path, encoding="utf-8") as f:
                for line in f:
                    match = re.match(r"^\s*version:\s*([^#\n]+)", line)
                    if match:
                        version = match.group(1).strip().strip('"').strip("'")
                        if strip_v_prefix:
                            version = version.lstrip("vV")
                        return version or default
        else:
            logger.debug(f"[主动消息] metadata.yaml 未找到喵: {metadata_path}")
    except Exception as e:
        logger.error(f"[主动消息] 获取插件版本失败喵: {e}")

    return default


def _build_unknown_version_info(
    default: str = "unknown", error: str = "all_methods_failed"
) -> AstrBotVersionInfo:
    """构造“探测失败”的统一版本信息。"""
    return AstrBotVersionInfo(version=default, source="unknown", error=error)


def _get_astrbot_version_from_core_config() -> AstrBotVersionInfo | None:
    """从 astrbot.core.config 模块的 VERSION 常量读取版本。"""
    module_candidates = (
        "astrbot.core.config",
        "astrbot.core.config.default",
    )

    for module_name in module_candidates:
        try:
            module = __import__(module_name, fromlist=["VERSION"])
            version = str(getattr(module, "VERSION", "")).strip()
            if version:
                return AstrBotVersionInfo(version=version, source="core_config")
        except Exception as exc:
            logger.debug(
                "[主动消息] 从 %s 读取 AstrBot VERSION 失败喵: %s",
                module_name,
                exc,
            )

    return None


def _get_astrbot_version_from_distribution() -> AstrBotVersionInfo | None:
    """从导入包的分发元数据读取版本。"""
    for dist_name in ("AstrBot", "astrbot"):
        try:
            version = str(importlib_metadata.version(dist_name)).strip()
            if version:
                return AstrBotVersionInfo(version=version, source="distribution")
        except importlib_metadata.PackageNotFoundError:
            logger.debug(
                "[主动消息] 未找到 AstrBot 分发元数据喵: %s",
                dist_name,
            )
        except Exception as exc:
            logger.debug(
                "[主动消息] 读取 AstrBot 分发元数据失败喵 (%s): %s",
                dist_name,
                exc,
            )

    return None


def _get_astrbot_version_from_cli_module() -> AstrBotVersionInfo | None:
    """从 astrbot.cli.__version__ 读取版本。"""
    try:
        from astrbot.cli import __version__ as cli_version

        version = str(cli_version).strip()
        if version:
            return AstrBotVersionInfo(version=version, source="cli_module")
        logger.debug("[主动消息] astrbot.cli.__version__ 为空喵。")
    except Exception as exc:
        logger.debug(f"[主动消息] 导入 astrbot.cli.__version__ 失败喵: {exc}")

    return None


def _get_astrbot_version_from_pyproject(default: str = "unknown") -> AstrBotVersionInfo:
    """从 AstrBot 包目录旁的 pyproject.toml 读取版本（最后兜底方案）。"""
    try:
        astrbot_path = Path(astrbot.__file__).resolve().parent.parent
        pyproject_path = astrbot_path / "pyproject.toml"

        if not pyproject_path.exists():
            logger.debug(
                f"[主动消息] 无法读取 AstrBot 版本喵，pyproject.toml 不存在: {pyproject_path}"
            )
            return _build_unknown_version_info(default, "pyproject_missing")

        if tomllib is None:
            logger.warning(
                "[主动消息] 未找到 tomllib 或 tomli 模块，无法解析 AstrBot 版本喵。"
            )
            return _build_unknown_version_info(default, "tomllib_unavailable")

        with open(pyproject_path, "rb") as f:
            data = tomllib.load(f)

        project_version = str(data.get("project", {}).get("version", "")).strip()
        if project_version:
            return AstrBotVersionInfo(version=project_version, source="pyproject")

        poetry_version = str(
            data.get("tool", {}).get("poetry", {}).get("version", "")
        ).strip()
        if poetry_version:
            return AstrBotVersionInfo(version=poetry_version, source="pyproject")

        logger.debug(
            f"[主动消息] pyproject.toml 中未找到可用的 AstrBot 版本字段喵: {pyproject_path}"
        )
        return _build_unknown_version_info(default, "pyproject_version_missing")
    except Exception as e:
        logger.debug(f"[主动消息] 获取 AstrBot 版本时出错喵: {e}")
        return _build_unknown_version_info(default, "pyproject_parse_failed")


def get_astrbot_version_info(default: str = "unknown") -> AstrBotVersionInfo:
    """获取带来源与错误码的 AstrBot 版本探测结果。

    依次尝试多种探测方式，全部失败时回退到 pyproject 再返回 unknown。
    """
    for resolver in (
        _get_astrbot_version_from_core_config,
        _get_astrbot_version_from_distribution,
        _get_astrbot_version_from_cli_module,
    ):
        version_info = resolver()
        if version_info is not None:
            return version_info

    return _get_astrbot_version_from_pyproject(default)


def get_astrbot_version(default: str = "unknown") -> str:
    """获取 AstrBot 版本号字符串（忽略来源与错误信息）。"""
    return get_astrbot_version_info(default).version
