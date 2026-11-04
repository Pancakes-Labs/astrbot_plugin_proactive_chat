"""Review regressions execute real source functions with isolated SDK boundaries."""
import ast
import asyncio
import importlib.util
import inspect
import logging
import sys
import traceback
import types
import unittest
from enum import Enum
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]


def function(file, name, **namespace):
    tree = ast.parse((ROOT / file).read_text(encoding="utf-8"))
    node = next(n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    node.decorator_list = []
    namespace.update(Any=object)
    exec(compile(ast.Module(body=[node], type_ignores=[]), file, "exec"), namespace)
    return namespace[name]


class ToolSet:
    def __init__(self, tools=()):
        self.func_list = list(tools)

    def __bool__(self):
        return bool(self.func_list)


class ReviewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        provider_module = types.ModuleType("astrbot.api.provider")
        provider_module.ProviderRequest = types.SimpleNamespace
        provider_module.LLMResponse = lambda **kw: types.SimpleNamespace(result_chain=None, **kw)
        tool_module = types.ModuleType("astrbot.core.agent.tool")
        tool_module.ToolSet = ToolSet
        self.patch = patch.dict(sys.modules, {
            provider_module.__name__: provider_module,
            tool_module.__name__: tool_module,
        })
        self.patch.start()
        self.addCleanup(self.patch.stop)
        spec = importlib.util.spec_from_file_location("review_adapter", ROOT / "core/adapters/astrbot_provider.py")
        self.bridge = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.bridge)
        self.provider = types.SimpleNamespace(text_chat=AsyncMock(return_value="plain"),
                                              meta=lambda: types.SimpleNamespace(id="provider"))
        async def modern_loop(*, event, chat_provider_id, prompt=None,
                              contexts=None, system_prompt=None,
                              extra_user_content_parts=None, tools=None,
                              max_steps=30, tool_call_timeout=120):
            return "final"
        loop = AsyncMock(return_value="final")
        loop.__signature__ = inspect.signature(modern_loop)
        self.context = types.SimpleNamespace(tool_loop_agent=loop)
        self.event = types.SimpleNamespace(get_extra=lambda key, default=False: True)
        self.kwargs = dict(context=self.context, event=self.event,
                           session_config={"enable": True}, trigger="schedule")

    async def test_single_shot_keeps_other_hook_tools_without_mutation(self):
        other = types.SimpleNamespace(name="weather", active=True)
        comment = types.SimpleNamespace(name="douyin_comment_watched", active=True)
        req = types.SimpleNamespace(func_tool=ToolSet([other, comment]))
        await self.bridge.invoke_provider(self.provider, req, **self.kwargs)
        self.assertEqual(self.provider.text_chat.call_args.kwargs["func_tool"].func_list, [other])
        self.assertEqual(req.func_tool.func_list, [other, comment])

    async def test_comment_only_never_executes_even_on_retries(self):
        req = types.SimpleNamespace(func_tool=ToolSet([
            types.SimpleNamespace(name="douyin_comment_watched", active=True)]))
        for _ in range(2):
            await self.bridge.invoke_provider(self.provider, req, **self.kwargs)
        self.context.tool_loop_agent.assert_not_awaited()
        self.assertIsNone(self.provider.text_chat.call_args.kwargs["func_tool"])

    async def test_string_response_is_normalized_before_response_hooks(self):
        req = types.SimpleNamespace(func_tool=ToolSet([
            types.SimpleNamespace(name="douyin_watch_recommended", active=True)]))
        result = await self.bridge.invoke_provider(self.provider, req, **self.kwargs)
        self.assertEqual(result.completion_text, "final")
        self.assertEqual(result.result_chain, None)

    async def test_real_dispatcher_propagates_partial_request_failure(self):
        async def bad_handler(event, req):
            req.func_tool = ToolSet([object()])
            raise RuntimeError("permission failed")
        registry = types.SimpleNamespace(get_handlers_by_event_type=lambda *a, **kw:
                                        [types.SimpleNamespace(handler=bad_handler)])
        dispatch = function("core/adapters/astrbot_event.py", "dispatch_event_hook",
                            inspect=inspect, traceback=traceback,
                            logger=logging.getLogger("review"), star_handlers_registry=registry)
        with self.assertRaisesRegex(RuntimeError, "permission failed"):
            await dispatch(types.SimpleNamespace(is_stopped=lambda: False), "request",
                           types.SimpleNamespace(func_tool=None), propagate_errors=True)

    async def test_request_service_enables_strict_dispatch_for_both_stages(self):
        dispatch = AsyncMock(return_value=False)
        event_type = types.SimpleNamespace(
            OnWaitingLLMRequestEvent="waiting", OnLLMRequestEvent="request")
        entry = function("core/services/llm_service.py", "dispatch_llm_request_hooks",
                         dispatch_event_hook=dispatch, EventType=event_type)
        event = types.SimpleNamespace(is_stopped=lambda: False)
        req = object()
        await entry(object(), event, req)
        self.assertEqual(dispatch.await_count, 2)
        for call in dispatch.await_args_list:
            self.assertTrue(call.kwargs["propagate_errors"])

    async def test_sdk_module_scope_is_restored_after_test_module_import(self):
        names = ["astrbot.api.provider", "astrbot.core.agent.tool"]
        before = {name: sys.modules.get(name) for name in names}
        spec = importlib.util.spec_from_file_location(
            "isolation_probe", ROOT / "tests/test_proactive_bridge.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual({name: sys.modules.get(name) for name in names}, before)

    async def test_manual_source_is_preserved_by_public_entry(self):
        class TriggerSource(Enum):
            SCHEDULE = "schedule"
            MANUAL = "manual"
        entry = function("main.py", "check_and_chat", TriggerSource=TriggerSource)
        plugin = types.SimpleNamespace(orchestrator=types.SimpleNamespace(run=AsyncMock()))
        await entry(plugin, "session", trigger=TriggerSource.MANUAL)
        plugin.orchestrator.run.assert_awaited_once_with("session", TriggerSource.MANUAL)

    async def test_manual_api_reaches_entry_with_manual_source(self):
        tree = ast.parse((ROOT / "core/console/api_routes.py").read_text(encoding="utf-8"))
        node = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)
                    and any(isinstance(c, ast.Attribute) and c.attr == "check_and_chat"
                            for c in ast.walk(n)))
        calls = [n for n in ast.walk(node) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute) and n.func.attr == "check_and_chat"]
        self.assertEqual(len(calls), 1)
        keyword = next((k for k in calls[0].keywords if k.arg == "trigger"), None)
        self.assertIsNotNone(keyword)
        self.assertEqual(ast.unparse(keyword.value), "TriggerSource.MANUAL")


if __name__ == "__main__":
    unittest.main()
