"""Exercise mute through the real message handler without QQ, AI or data writes."""
import asyncio
import importlib
import time
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import nonebot
from nonebot.adapters.onebot.v11 import Message, MessageSegment
from nonebot.exception import FinishedException


class ChatMuteTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        nonebot.init()
        plugin = nonebot.load_plugin("plugins.ai_chat")
        if plugin is None:
            raise RuntimeError("Could not load chat plugin")
        cls.chat = plugin.module
        cls.summon = importlib.import_module("plugins.summon")
        cls.proactive = importlib.import_module("services.proactive")

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for state in (
            self.summon._group_mute_until,
            self.summon.group_state,
            self.summon.group_start_time,
            self.summon.group_last_active,
            self.summon.group_voice_mode,
            self.chat._FOLLOWUP_REPLY_LEFT,
        ):
            self.stack.enter_context(patch.dict(state, {}, clear=True))

        self.bot = SimpleNamespace(send_group_msg=AsyncMock())
        for name, value in {
            "get_bot": Mock(return_value=self.bot),
            "_resolve_group_name": AsyncMock(return_value="test"),
            "record_member": AsyncMock(),
            "updater": SimpleNamespace(ensure_user=AsyncMock(), auto_profile=AsyncMock()),
            "_get_name": Mock(return_value="tester"),
            "collect_from_event": AsyncMock(),
            "sticker_detect": Mock(return_value={}),
            "add_quote": AsyncMock(),
            "record_message": Mock(),
            "get_group_default_reply_prob": Mock(return_value=1.0),
            "resolve_mentions": Mock(return_value=[]),
            "update_mood": Mock(),
            "get_mood": Mock(return_value={}),
            "nya_record": Mock(),
            "learn_catchphrase": Mock(),
            "_safe_ask": AsyncMock(return_value=("test reply", True)),
        }.items():
            self.stack.enter_context(patch.object(self.chat, name, value))
        self.stack.enter_context(patch("services.data_store.get_user_sync", return_value=None))
        self.stack.enter_context(patch("services.data_store.update_user", new_callable=AsyncMock))
        self.stack.enter_context(patch.object(self.chat.gate, "should_reply", return_value=True))
        self.finish = self.stack.enter_context(
            patch.object(self.chat.chat, "finish", new_callable=AsyncMock,
                         side_effect=FinishedException)
        )

    def event(self, text, *, group="123", at=False):
        message = Message(text)
        if at:
            message = MessageSegment.at("370727979") + message
        return SimpleNamespace(
            group_id=int(group), user_id=456, self_id=370727979,
            sender=SimpleNamespace(card="tester", nickname="tester"),
            raw_message=str(message), message_id=789, message_type="group",
            get_plaintext=message.extract_plain_text, get_message=lambda: message,
        )

    async def receive(self, text, **kwargs):
        try:
            await self.chat.chat.handlers[0].call(self.event(text, **kwargs))
        except FinishedException:
            pass
        await asyncio.sleep(0)

    async def test_mute_command_sets_twelve_hours_and_acknowledges(self):
        before = time.time()
        self.chat._FOLLOWUP_REPLY_LEFT["123"] = 1
        await self.receive("牛牛喵闭嘴！")
        self.assertTrue(self.summon.is_group_muted("123"))
        self.assertAlmostEqual(self.summon._group_mute_until["123"] - before, 43200, delta=2)
        self.assertNotIn("123", self.chat._FOLLOWUP_REPLY_LEFT)
        self.finish.assert_awaited_once_with("好，我会安静12小时喵，不会主动说话。")
        self.chat._safe_ask.assert_not_awaited()
        self.chat._resolve_group_name.assert_not_awaited()

    async def test_at_mute_is_handled_before_mention_decoration(self):
        await self.receive("牛牛喵闭嘴！", at=True)
        self.assertTrue(self.summon.is_group_muted("123"))
        self.finish.assert_awaited_once()

    async def test_english_mute_still_works(self):
        await self.receive("bot mute 12h")
        self.assertTrue(self.summon.is_group_muted("123"))

    async def test_mute_ends_summoned_session(self):
        self.summon.activate("123")
        await self.receive("牛牛喵闭嘴！")
        self.assertNotIn("123", self.summon.group_state)
        self.assertNotIn("123", self.summon.group_last_active)
        await self.receive("今天有什么好玩的游戏")
        self.chat._safe_ask.assert_not_awaited()
        self.bot.send_group_msg.assert_not_awaited()

    async def test_muted_summon_cannot_bypass_mute(self):
        await self.receive("牛牛喵闭嘴！")
        self.summon.activate("123")
        self.chat._FOLLOWUP_REPLY_LEFT["123"] = 1
        await self.receive("今天有什么好玩的游戏")
        self.chat._safe_ask.assert_not_awaited()
        self.bot.send_group_msg.assert_not_awaited()

    async def test_called_reply_does_not_enable_followup_while_muted(self):
        for text, at in (("牛牛喵你好", False), ("你好", True)):
            with self.subTest(text=text, at=at):
                await self.receive("牛牛喵闭嘴！")
                self.bot.send_group_msg.reset_mock()
                await self.receive(text, at=at)
                self.bot.send_group_msg.assert_awaited_once()
                self.assertNotIn("123", self.chat._FOLLOWUP_REPLY_LEFT)
                self.chat._safe_ask.reset_mock()
                await self.receive("顺便问问你觉得怎么样")
                self.chat._safe_ask.assert_not_awaited()

    async def test_unmute_command_restores_normal_replies(self):
        await self.receive("牛牛喵闭嘴！")
        await self.receive("牛牛喵归来！")
        self.assertFalse(self.summon.is_group_muted("123"))
        self.finish.assert_awaited_with("喵喵回来了，解除12小时静音。")
        await self.receive("今天有什么好玩的游戏")
        self.bot.send_group_msg.assert_awaited_once()

    async def test_mute_expires_and_other_groups_are_unaffected(self):
        await self.receive("牛牛喵闭嘴！")
        await self.receive("今天有什么好玩的游戏", group="124")
        self.bot.send_group_msg.assert_awaited_once()
        self.bot.send_group_msg.reset_mock()
        with patch.object(self.summon.time, "time", return_value=time.time() + 43201):
            await self.receive("今天有什么好玩的游戏")
        self.bot.send_group_msg.assert_awaited_once()
        self.assertFalse(self.summon.is_group_muted("123"))

    async def test_pending_automatic_reply_is_dropped_on_mute(self):
        async def reply_after_mute(*args):
            await self.receive("牛牛喵闭嘴！")
            return "late reply", True

        self.chat._safe_ask.side_effect = reply_after_mute
        await self.receive("今天有什么好玩的游戏")
        self.bot.send_group_msg.assert_not_awaited()

    async def test_split_reply_stops_after_mute(self):
        async def mute_after_first_chunk(**kwargs):
            self.summon.mute_group("123", 43200)

        self.bot.send_group_msg.side_effect = mute_after_first_chunk
        with patch.object(self.chat.random, "random", return_value=0), \
                patch.object(self.chat, "_build_typing_chunks", return_value=["first", "second"]), \
                patch.object(self.chat.random, "uniform", return_value=0):
            await self.chat._send_reply("123", "first second", self.event("hello"), typing_habit=True)
        self.bot.send_group_msg.assert_awaited_once_with(group_id=123, message="first")

    async def test_proactive_reply_observes_mute_before_model_call(self):
        self.summon.activate("123")
        self.summon._group_mute_until["123"] = time.time() + 43200
        with patch.object(self.proactive, "ask_ai", new_callable=AsyncMock) as ask:
            await self.proactive.try_proactive_say("123")
            ask.assert_not_awaited()

    async def test_pending_proactive_reply_is_dropped_on_mute(self):
        self.summon.activate("123")
        self.summon.group_last_active["123"] = time.time() - 181

        async def reply_after_mute(*args, **kwargs):
            await self.receive("牛牛喵闭嘴！")
            return "late proactive reply"

        with patch.object(self.proactive, "pick_target", return_value=("456", "random")), \
                patch.object(self.proactive, "get_users_sync", return_value={}), \
                patch.object(self.proactive, "_build_proactive_prompt", return_value="test"), \
                patch.object(self.proactive, "get_mood", return_value={}), \
                patch.object(self.proactive, "ask_ai", side_effect=reply_after_mute), \
                patch.object(self.proactive, "get_bot", return_value=self.bot):
            await self.proactive.try_proactive_say("123")
        self.bot.send_group_msg.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
