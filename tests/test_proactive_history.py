import unittest

from core.adapters.astrbot_conversation import add_proactive_message_pair


class FakeConversationManager:
    def __init__(self):
        self.calls = []

    async def add_message_pair(self, cid, user_message, assistant_message):
        self.calls.append((cid, user_message, assistant_message))


def text_of(message):
    content = message.content
    return content[0].text if isinstance(content, list) else content


class ProactiveHistoryTest(unittest.IsolatedAsyncioTestCase):
    async def test_uses_neutral_user_placeholder(self):
        manager = FakeConversationManager()

        await add_proactive_message_pair(manager, "conv-1", "hello")

        self.assertEqual(len(manager.calls), 1)
        cid, user, assistant = manager.calls[0]
        self.assertEqual(cid, "conv-1")
        self.assertEqual(text_of(user), "[主动消息]")
        self.assertEqual(text_of(assistant), "hello")
