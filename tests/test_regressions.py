import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RegressionTests(unittest.TestCase):
    def test_admin_commands_are_handled_before_generic_control_return(self):
        source = (ROOT / "plugins" / "ai_chat.py").read_text(encoding="utf-8")

        admin_check = source.index("if text in ADMIN_CONTROL_COMMANDS")
        generic_return = source.index("if _is_control_command(text):")

        self.assertLess(admin_check, generic_return)
        self.assertIn("牛牛喵闭嘴！", source)
        self.assertIn("牛牛喵归来！", source)

    def test_proactive_runs_before_auto_sleep_check(self):
        source = (ROOT / "services" / "proactive.py").read_text(encoding="utf-8")

        try_proactive = source.index("await try_proactive_say(group_id)")
        auto_check = source.index("check_auto(group_id)")

        self.assertLess(try_proactive, auto_check)
        self.assertIn("group_last_active[group_id] = time.time()", source)

    def test_impression_prompt_has_no_private_use_garbage(self):
        source = (ROOT / "services" / "impression.py").read_text(encoding="utf-8")

        private_use = [ch for ch in source if 0xE000 <= ord(ch) <= 0xF8FF]
        self.assertEqual(private_use, [])
        self.assertIn("成员名|一句不超过40字的印象", source)
        self.assertIn('{"role": "user", "content": f"聊天记录：\\n{ctx}"}', source)

    def test_new_user_style_allows_auto_profile(self):
        from services.data_store import _new_user
        from services.profile_updater import UNPROFILED_STYLES

        user = _new_user("123")
        self.assertEqual(user["style"], "新用户")
        self.assertIn("A normal user style", UNPROFILED_STYLES)

    def test_group_runtime_mute_state(self):
        from services.group_runtime import is_group_muted, mute_group, unmute_group

        unmute_group("test")
        self.assertFalse(is_group_muted("test"))

        mute_group("test", 60)
        self.assertTrue(is_group_muted("test"))

        unmute_group("test")
        self.assertFalse(is_group_muted("test"))

    def test_persona_prompt_has_token_begging_mama_style(self):
        deepseek_source = (ROOT / "services" / "deepseek_client.py").read_text(encoding="utf-8")
        personality_source = (ROOT / "services" / "nya_personality.py").read_text(encoding="utf-8")

        self.assertIn("成年可爱妈妈系", deepseek_source)
        self.assertIn("充 tokens", deepseek_source)
        self.assertIn("不要施压、不要刷屏", deepseek_source)
        self.assertIn("卖萌要饭", personality_source)


if __name__ == "__main__":
    unittest.main()
