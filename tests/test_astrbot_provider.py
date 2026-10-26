"""Provider 适配器的对话标识传递与旧接口兼容性回归测试。"""

import importlib.util
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch


# 仅替代类型导入，测试直接加载真实适配器，无需启动 AstrBot。
provider_module = ModuleType("astrbot.api.provider")
provider_module.ProviderRequest = SimpleNamespace
spec = importlib.util.spec_from_file_location(
    "astrbot_provider_under_test",
    Path(__file__).resolve().parents[1] / "core/adapters/astrbot_provider.py",
)
adapter = importlib.util.module_from_spec(spec)
with patch.dict(
    sys.modules,
    {
        "astrbot": ModuleType("astrbot"),
        "astrbot.api": ModuleType("astrbot.api"),
        "astrbot.api.provider": provider_module,
    },
):
    spec.loader.exec_module(adapter)


class InvokeProviderTests(unittest.IsolatedAsyncioTestCase):
    """覆盖真实对话 ID、缺省对话及不同 Provider 签名。"""

    async def test_passes_conversation_id_and_preserves_request_fields(self):
        """对话标识与原有请求内容一起传给支持 kwargs 的 Provider。"""
        provider = SimpleNamespace(text_chat=AsyncMock(return_value="response"))
        req = SimpleNamespace(
            prompt="hello",
            contexts=[{"role": "user", "content": "previous"}],
            system_prompt="persona",
            func_tool=object(),
            extra_user_content_parts=[{"type": "text", "text": "context"}],
            model="test-model",
            session_id="platform:FriendMessage:user",
            conversation=SimpleNamespace(cid="conversation-a"),
        )

        response = await adapter.invoke_provider(provider, req)

        self.assertEqual(response, "response")
        provider.text_chat.assert_awaited_once_with(
            prompt=req.prompt,
            contexts=req.contexts,
            system_prompt=req.system_prompt,
            func_tool=req.func_tool,
            extra_user_content_parts=req.extra_user_content_parts,
            model=req.model,
            conversation_id="conversation-a",
        )

    async def test_keeps_conversation_ids_separate_and_stable(self):
        """同一对话 ID 保持稳定，不同对话不共用标识。"""
        provider = SimpleNamespace(text_chat=AsyncMock())
        for cid in ("conversation-a", "conversation-a", "conversation-b"):
            req = SimpleNamespace(conversation=SimpleNamespace(cid=cid))
            await adapter.invoke_provider(provider, req)

        self.assertEqual(
            [
                call.kwargs["conversation_id"]
                for call in provider.text_chat.await_args_list
            ],
            ["conversation-a", "conversation-a", "conversation-b"],
        )

    async def test_omits_missing_or_empty_conversation_id(self):
        """无有效对话 ID 时不伪造标识，也不使用平台会话 ID 替代。"""
        requests = [
            SimpleNamespace(),
            SimpleNamespace(conversation=None),
            SimpleNamespace(conversation=SimpleNamespace()),
            SimpleNamespace(conversation=SimpleNamespace(cid=None)),
            SimpleNamespace(conversation=SimpleNamespace(cid="")),
        ]
        for req in requests:
            with self.subTest(request=req):
                req.session_id = "platform:FriendMessage:user"
                provider = SimpleNamespace(text_chat=AsyncMock())
                await adapter.invoke_provider(provider, req)
                self.assertNotIn(
                    "conversation_id", provider.text_chat.await_args.kwargs
                )

    async def test_supports_explicit_conversation_id_parameter(self):
        """不使用 kwargs 的新 Provider 也能接收对话 ID。"""

        async def text_chat(prompt, conversation_id=None):
            return prompt, conversation_id

        provider = SimpleNamespace(text_chat=text_chat)
        req = SimpleNamespace(
            prompt="hello", conversation=SimpleNamespace(cid="conversation-a")
        )
        self.assertEqual(
            await adapter.invoke_provider(provider, req), ("hello", "conversation-a")
        )

    async def test_filters_conversation_id_for_legacy_provider(self):
        """旧 Provider 不接受新参数时仍正常调用。"""

        calls = []
        response = object()

        async def text_chat(prompt, contexts, system_prompt, func_tool):
            calls.append(
                {
                    "prompt": prompt,
                    "contexts": contexts,
                    "system_prompt": system_prompt,
                    "func_tool": func_tool,
                }
            )
            return response

        provider = SimpleNamespace(text_chat=text_chat)
        req = SimpleNamespace(
            prompt="hello",
            contexts=[{"role": "user", "content": "previous"}],
            system_prompt="persona",
            func_tool=object(),
            conversation=SimpleNamespace(cid="conversation-a"),
        )
        self.assertIs(await adapter.invoke_provider(provider, req), response)
        self.assertEqual(
            calls,
            [
                {
                    "prompt": req.prompt,
                    "contexts": req.contexts,
                    "system_prompt": req.system_prompt,
                    "func_tool": req.func_tool,
                }
            ],
        )

    async def test_omits_optional_positional_only_conversation_id(self):
        """不将仅限位置的可选参数误作关键字传入。"""

        async def text_chat(conversation_id=None, /, *, prompt):
            return prompt, conversation_id

        provider = SimpleNamespace(text_chat=text_chat)
        req = SimpleNamespace(
            prompt="hello", conversation=SimpleNamespace(cid="conversation-a")
        )
        self.assertEqual(await adapter.invoke_provider(provider, req), ("hello", None))

    async def test_preserves_kwargs_with_positional_only_name(self):
        """签名含 kwargs 时，同名关键字仍可由 kwargs 接收。"""

        async def text_chat(conversation_id=None, /, **kwargs):
            return conversation_id, kwargs

        provider = SimpleNamespace(text_chat=text_chat)
        req = SimpleNamespace(
            prompt="hello", conversation=SimpleNamespace(cid="conversation-a")
        )
        self.assertEqual(
            await adapter.invoke_provider(provider, req),
            (
                None,
                {
                    "prompt": "hello",
                    "contexts": None,
                    "system_prompt": None,
                    "func_tool": None,
                    "conversation_id": "conversation-a",
                },
            ),
        )

    async def test_preserves_provider_exception(self):
        """适配器不吞掉模型服务的原始异常。"""
        provider = SimpleNamespace(
            text_chat=AsyncMock(side_effect=RuntimeError("error"))
        )
        req = SimpleNamespace(conversation=SimpleNamespace(cid="conversation-a"))
        with self.assertRaisesRegex(RuntimeError, "error"):
            await adapter.invoke_provider(provider, req)
