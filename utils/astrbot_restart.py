"""AstrBot 进程重启适配层。

AstrBot 在 v4.27.0 把更新子系统拆分为 process_restart.restart_process()，
旧版本则是 updator.AstrBotUpdator()._reboot()。为避免顶层导入在某一版本上
直接抛 ModuleNotFoundError 拖垮整个插件，这里统一改为：
1. 延迟导入，只在真正重启时触碰 AstrBot 内部模块；
2. 按新版优先、旧版回退的顺序探测；
3. 重启动作放进 daemon 线程，避免阻塞事件循环；
4. 全进程幂等，防止同名指令被多个插件注册时重复派发。
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from astrbot.api import logger

__all__ = [
    "RestartAlreadyInProgress",
    "is_demo_mode",
    "is_desktop_managed",
    "restart_astrbot_in_background",
]


class RestartAlreadyInProgress(RuntimeError):
    """本进程内已有重启任务在执行，本次派发被跳过。"""


# 已知重启线程名前缀。本插件与同类插件各自使用独立线程名，
# 但都跑在同一个 Python 进程内，因此可以用线程名互相探测到对方。
_RESTART_THREAD_PREFIXES: tuple[str, ...] = (
    "proactive-chat-astrbot-restart",
    "astrbot-core-restart",
)

# 派发互斥：避免本插件内部的并发重入（跨插件靠上面的线程名探测兜底）。
_dispatch_lock = threading.Lock()
_dispatched = False


def _probe_process_restart() -> None:
    """新版（v4.27.0+）探测：只做延迟导入，不执行重启。"""
    from astrbot.core.process_restart import restart_process  # noqa: F401


def _run_process_restart() -> None:
    """新版执行：内部 sleep 后替换当前进程。"""
    from astrbot.core.process_restart import restart_process

    restart_process(delay=3)


def _probe_updator() -> None:
    """旧版（v4.26.x 及更早）探测：只做延迟导入。"""
    from astrbot.core.updator import AstrBotUpdator  # noqa: F401


def _run_updator() -> None:
    """旧版执行。"""
    from astrbot.core.updator import AstrBotUpdator

    AstrBotUpdator()._reboot()


# 按优先级排列：(探测函数, 执行函数)
_RESTART_STRATEGIES: tuple[tuple[Callable[[], None], Callable[[], None]], ...] = (
    (_probe_process_restart, _run_process_restart),
    (_probe_updator, _run_updator),
)


def is_desktop_managed() -> bool:
    """判断当前是否由 AstrBot Desktop 托管（托管下核心无法自行重启）。"""
    try:
        from astrbot.core.desktop_runtime import is_desktop_managed_backend

        return bool(is_desktop_managed_backend())
    except (ImportError, ModuleNotFoundError):
        # 老版本没有该模块，视为非托管。
        return False
    except Exception as e:  # noqa: BLE001 - 探测失败按非托管处理
        logger.debug(f"[主动消息] 探测桌面托管状态失败喵: {e}")
        return False


def is_demo_mode() -> bool:
    """判断是否运行在 AstrBot 演示模式。"""
    try:
        from astrbot.core import DEMO_MODE

        return bool(DEMO_MODE)
    except Exception:  # noqa: BLE001 - 属性缺失视为非演示模式
        return False


def _restart_already_dispatched() -> bool:
    """探测本进程内是否已有重启线程在运行（含其它插件派发的）。"""
    for thread in threading.enumerate():
        if not thread.is_alive():
            continue
        if thread.name.startswith(_RESTART_THREAD_PREFIXES):
            return True
    return False


def restart_astrbot_in_background(
    on_failure: Callable[[Exception], None] | None = None,
) -> tuple[bool, Exception | None]:
    """在后台 daemon 线程中触发 AstrBot 进程重启。

    Returns:
        (ok, error)。ok 表示重启线程是否成功派发；若已有重启任务在跑，
        error 为 RestartAlreadyInProgress，调用方应提示而不是报错。
    """
    global _dispatched

    if is_desktop_managed():
        return False, RuntimeError(
            "当前由 AstrBot Desktop 托管运行，无法通过核心命令重启"
        )

    # 先同步探测策略可用性再起线程：延迟导入发生在探测函数内，
    # 若把导入留给线程，Thread.start() 不会抛错，回退逻辑将永远失效。
    for probe, run in _RESTART_STRATEGIES:
        try:
            probe()
        except (ImportError, ModuleNotFoundError):
            continue
        except Exception as e:  # noqa: BLE001 - 探测异常换下一个策略
            logger.error(f"[主动消息] AstrBot 重启策略探测失败喵: {e}")
            continue

        # 探测成功后再做幂等判定，避免无可用策略时给出误导性提示。
        # 同类实现在 Windows 非 frozen 环境走 Popen + os._exit，
        # 重复派发会真正拉起两个 AstrBot 实例（抢端口、争数据库锁）。
        with _dispatch_lock:
            if _dispatched or _restart_already_dispatched():
                logger.warning(
                    "[主动消息] 已存在 AstrBot 重启任务，跳过本次重复派发喵。"
                )
                return False, RestartAlreadyInProgress(
                    "已有 AstrBot 重启任务在进行中，请稍候"
                )
            _dispatched = True

        def _run_with_guard(runner=run) -> None:
            global _dispatched
            try:
                runner()
            except Exception as e:  # noqa: BLE001 - 线程边界兜底
                logger.error(f"[主动消息] AstrBot 重启线程执行失败喵: {e}")
                if on_failure is not None:
                    try:
                        on_failure(e)
                    except Exception:  # noqa: BLE001 - 回调自身异常不再抛出
                        logger.error("[主动消息] AstrBot 重启失败回调执行出错喵。")
            finally:
                # 进程替换成功时本线程随进程一起消失，不会走到这里；
                # 能走到这里说明重启失败，必须复位标记以允许后续重试。
                _dispatched = False

        try:
            threading.Thread(
                target=_run_with_guard,
                name="proactive-chat-astrbot-restart",
                daemon=True,
            ).start()
        except Exception as e:  # noqa: BLE001 - 线程起不来时不能卡住幂等标记
            with _dispatch_lock:
                _dispatched = False
            logger.error(f"[主动消息] 无法启动 AstrBot 重启线程喵: {e}")
            return False, e

        logger.info("[主动消息] 已派发 AstrBot 重启任务喵（来源：指令/接口调用）。")
        return True, None

    logger.warning("[主动消息] 当前 AstrBot 版本无可用重启策略喵。")
    return False, RuntimeError("当前 AstrBot 版本无可用重启策略")
