"""Offline bridge contracts; no device or public API calls."""
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


class ToolSet:
    """Minimal SDK-shaped test double."""

    def __init__(self, tools=None):
        self.func_list = tools or []

    def __bool__(self):
        return bool(self.func_list)


provider_api = types.ModuleType("astrbot.api.provider")
provider_api.ProviderRequest = types.SimpleNamespace
provider_api.LLMResponse = lambda **kw: types.SimpleNamespace(result_chain=None, **kw)
tool_api = types.ModuleType("astrbot.core.agent.tool")
tool_api.ToolSet = ToolSet
path = Path(__file__).resolve().parents[1] / "core/adapters/astrbot_provider.py"
spec = importlib.util.spec_from_file_location("bridge_adapter", path)
bridge = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {provider_api.__name__: provider_api}):
    spec.loader.exec_module(bridge)


def tool(name, active=True):
    """Return an inert tool definition."""
    return types.SimpleNamespace(name=name, active=active)


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    """Check execution routing without invoking any real tool."""

    def setUp(self):
        sdk = patch.dict(sys.modules, {tool_api.__name__: tool_api})
        sdk.start()
        self.addCleanup(sdk.stop)
        self.provider = types.SimpleNamespace(
            text_chat=AsyncMock(return_value="plain"),
            meta=lambda: types.SimpleNamespace(id="resolved-provider"),
        )
        self.context = types.SimpleNamespace(
            tool_loop_agent=AsyncMock(return_value="final-response"),
        )
        self.event = types.SimpleNamespace(
            get_extra=lambda key, default=False: True,
        )
        self.req = types.SimpleNamespace(
            prompt="hook-edited", contexts=[{"role": "user"}],
            system_prompt="system", extra_user_content_parts=["temp"],
            func_tool=ToolSet([
                tool("douyin_watch_recommended"),
                tool("douyin_comment_watched"), tool("execute_shell"),
                tool("inactive", False),
            ]),
        )
        self.kwargs = dict(
            context=self.context, event=self.event,
            session_config={"enable": True}, trigger="schedule",
        )

    async def test_framework_loop_and_exact_provider(self):
        result = await bridge.invoke_provider(
            self.provider, self.req, **self.kwargs,
        )
        self.assertEqual(result.completion_text, "final-response")
        args = self.context.tool_loop_agent.call_args.kwargs
        self.assertEqual(args["chat_provider_id"], "resolved-provider")
        self.assertIs(args["event"], self.event)
        self.assertEqual(args["max_steps"], 4)
        self.assertEqual(args["tool_call_timeout"], 180)
        self.assertEqual(args["prompt"], "hook-edited")
        self.assertEqual(args["extra_user_content_parts"], ["temp"])
        self.assertEqual(
            [t.name for t in args["tools"].func_list],
            ["douyin_watch_recommended"],
        )
        self.assertEqual(len(self.req.func_tool.func_list), 4)
        self.provider.text_chat.assert_not_awaited()

    async def fallback(self):
        result = await bridge.invoke_provider(
            self.provider, self.req, **self.kwargs,
        )
        self.assertEqual(result, "plain")
        self.context.tool_loop_agent.assert_not_awaited()
        self.assertTrue(all(
            t.name not in bridge.DOUYIN_TOOL_NAMES
            for t in getattr(self.provider.text_chat.call_args.kwargs["func_tool"], "func_list", [])
        ))

    async def test_disabled_session(self):
        self.kwargs["session_config"] = {"enable": False}
        await self.fallback()

    async def test_missing_configuration(self):
        self.kwargs["session_config"] = {}
        await self.fallback()

    async def test_missing_framework_loop(self):
        self.context.tool_loop_agent = None
        result = await bridge.invoke_provider(self.provider, self.req, **self.kwargs)
        self.assertEqual(result, "plain")
        self.assertEqual(
            [t.name for t in self.provider.text_chat.call_args.kwargs["func_tool"].func_list],
            ["execute_shell", "inactive"],
        )

    async def test_model_override_preserved_in_plain_path(self):
        self.req.model = "explicit-model"
        self.req.session_id = "test-session"
        self.req.conversation = types.SimpleNamespace(cid="conversation-id")
        await self.fallback()
        args = self.provider.text_chat.call_args.kwargs
        self.assertEqual(args["model"], "explicit-model")
        self.assertEqual(args["session_id"], "test-session")
        self.assertEqual(args["conversation_id"], "conversation-id")

    async def test_legacy_provider_signature(self):
        async def legacy(prompt, contexts, system_prompt):
            return prompt
        self.provider.text_chat = legacy
        self.kwargs["trigger"] = "manual"
        result = await bridge.invoke_provider(self.provider, self.req, **self.kwargs)
        self.assertEqual(result, "hook-edited")

    async def test_non_schedule(self):
        self.kwargs["trigger"] = "manual"
        await self.fallback()

    async def test_missing_event(self):
        self.kwargs["event"] = None
        await self.fallback()

    async def test_missing_schedule_marker(self):
        self.event.get_extra = lambda key, default=False: False
        await self.fallback()

    async def test_hook_removes_all_tools(self):
        self.req.func_tool = ToolSet()
        await self.fallback()

    async def test_comment_disabled_by_hook(self):
        self.req.func_tool = ToolSet([tool("douyin_watch_recommended")])
        await bridge.invoke_provider(self.provider, self.req, **self.kwargs)
        tools = self.context.tool_loop_agent.call_args.kwargs["tools"]
        self.assertEqual(
            [t.name for t in tools.func_list], ["douyin_watch_recommended"],
        )

    async def test_inactive_tools_excluded(self):
        self.req.func_tool = ToolSet([
            tool("douyin_watch_recommended", False),
        ])
        await self.fallback()

    async def test_loop_error_does_not_replay(self):
        self.context.tool_loop_agent.side_effect = TimeoutError("unknown")
        with self.assertRaises(TimeoutError):
            await bridge.invoke_provider(
                self.provider, self.req, **self.kwargs,
            )
        self.provider.text_chat.assert_not_awaited()


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        sdk = patch.dict(sys.modules, {tool_api.__name__: tool_api})
        sdk.start()
        self.addCleanup(sdk.stop)

    """Execute the actual service method with inert framework boundaries."""

    async def test_hook_failure_clears_partially_added_tools(self):
        import ast
        import logging

        source_path = path.parents[1] / "services/llm_service.py"
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef))
        method = next(n for n in cls.body
                      if getattr(n, "name", "") == "generate_llm_response")
        ns = {
            "Any": object, "logger": logging.getLogger("bridge-test"),
            "format_current_time": lambda tz: "now",
            "build_provider_request": lambda **kw: types.SimpleNamespace(
                **kw, func_tool=None,
            ),
            "invoke_provider": AsyncMock(return_value=None),
        }
        exec(compile(ast.Module(body=[method], type_ignores=[]),
                     str(source_path), "exec"), ns)
        extras = {}
        event = types.SimpleNamespace(
            set_extra=lambda key, value: extras.update({key: value}),
        )
        provider = object()
        plugin = types.SimpleNamespace(
            timezone=None, telemetry=None,
            context=types.SimpleNamespace(tool_loop_agent=AsyncMock()),
            context_service=types.SimpleNamespace(
                sanitize_history_content=lambda history: history,
            ),
        )

        async def broken_hook(ev, req):
            self.assertTrue(extras["douyin_proactive_tool_loop"])
            req.func_tool = ToolSet([tool("douyin_comment_watched")])
            raise RuntimeError("later permission hook failed")

        service = types.SimpleNamespace(
            plugin=plugin, build_extra_content_parts=lambda **kw: [],
            resolve_chat_provider=AsyncMock(return_value=provider),
            dispatch_llm_request_hooks=broken_hook,
        )
        await ns["generate_llm_response"](
            service, "session", {"enable": True}, [], "", 0,
            event=event, trigger="schedule",
        )
        request = ns["invoke_provider"].call_args.args[1]
        self.assertIsNone(request.func_tool)
        self.assertEqual(
            ns["invoke_provider"].call_args.kwargs["trigger"], "schedule",
        )


if __name__ == "__main__":
    unittest.main()
