"""
AI 回复门控 — 决定是否触发 AI。
冷却为每人独立：同一个人 5 秒内不重复触发。
"""
import time
import random
from services.utils import is_noise


class AIGate:
    """回复门控：每人独立冷却 + 噪声过滤 + 随机概率"""

    def __init__(self, cooldown: float = 5.0, reply_prob: float = 0.15):
        self._last: dict[str, float] = {}   # "group_id:user_id" → last reply time
        self.cooldown = cooldown
        self.reply_prob = reply_prob
        self._group_default_prob: dict[str, float] = {}
        self._group_reply_prob: dict[str, float] = {}

    def _key(self, group_id: str, user_id: str) -> str:
        return f"{group_id}:{user_id}"

    # ── 冷却检查 ──
    def is_cooldown(self, group_id: str, user_id: str) -> bool:
        key = self._key(group_id, user_id)
        now = time.time()
        last = self._last.get(key, 0)
        return (now - last) < self.cooldown

    # ── 记录回复时间 ──
    def touch(self, group_id: str, user_id: str):
        self._last[self._key(group_id, user_id)] = time.time()

    # ── 综合判断（非召唤模式） ──
    def should_reply(self, group_id: str, user_id: str, text: str,
                     force_reply: bool = False) -> bool:
        """
        返回 True 表示应该触发 AI 回复。
        force_reply=True（如含关键词）跳过概率，但仍受冷却限制。
        """
        # ① 每人独立冷却（同一个人 5s 内不重复触发）
        if self.is_cooldown(group_id, user_id):
            return False

        # ② 噪声过滤
        if is_noise(text):
            return False

        # ③ 强制回复：跳过概率
        if force_reply:
            self.touch(group_id, user_id)
            return True

        # ④ 随机概率
        prob = self._group_reply_prob.get(
            group_id,
            self._group_default_prob.get(group_id, self.reply_prob),
        )
        if random.random() < prob:
            self.touch(group_id, user_id)
            return True

        return False

    def set_group_reply_prob(self, group_id: str, reply_prob: float):
        """设置群临时回复概率（命令降频/调试用）。"""
        reply_prob = max(0.0, min(1.0, reply_prob))
        self._group_reply_prob[group_id] = reply_prob

    def set_group_default_prob(self, group_id: str, reply_prob: float):
        """设置某群默认回复概率（后台配置生效目标）。"""
        reply_prob = max(0.0, min(1.0, reply_prob))
        self._group_default_prob[group_id] = reply_prob

    def reset_group_reply_prob(self, group_id: str):
        """恢复某群为其默认随机概率（移除临时覆盖）。"""
        self._group_reply_prob.pop(group_id, None)
