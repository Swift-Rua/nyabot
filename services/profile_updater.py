"""
用户画像管理 — 基于统一数据层的画像读写与自动标签推断。
合并了原 member_profile.py 的自动标签生成逻辑。
"""
from services.data_store import ensure_user, update_user, update_tags, get_user_sync, get_users_sync


class ProfileUpdater:
    """画像更新器（所有方法均为 async，因为底层有异步写锁）"""

    # ═══════════════════════════════════════
    # 确保用户存在
    # ═══════════════════════════════════════

    async def ensure_user(self, user_id: str) -> dict:
        return await ensure_user(user_id)

    @staticmethod
    def _clean_aliases(aliases: list[str] | None = None) -> list[str]:
        """过滤别名：去重 + 去首尾空白 + 禁止单字/单数字。"""
        out: list[str] = []
        seen: set[str] = set()
        for alias in aliases or []:
            if not isinstance(alias, str):
                continue
            alias = alias.strip()
            if len(alias) < 2:
                continue
            if len(alias) == 1 and alias.isdigit():
                continue
            if alias in seen:
                continue
            seen.add(alias)
            out.append(alias)
        return out

    # ═══════════════════════════════════════
    # 安全更新
    # ═══════════════════════════════════════

    async def update(self, user_id: str, patch: dict):
        await update_user(user_id, patch)

    async def update_tags(self, user_id: str, tags: dict):
        await update_tags(user_id, tags)

    # ═══════════════════════════════════════
    # 自动标签推断（合并自 member_profile.py）
    # ═══════════════════════════════════════

    def infer_tags(self, message: str) -> dict:
        """根据消息内容推断初始标签"""
        text = message.lower()
        tags: dict[str, list[str]] = {
            "core": [],
            "interest": [],
            "behavior": [],
        }

        if any(x in text for x in ["哈哈", "hhh", "笑死", "233"]):
            tags["behavior"].append("情绪活跃")

        if any(x in text for x in ["蠢", "傻", "离谱"]):
            tags["behavior"].append("吐槽倾向")

        if "?" in text or "？" in text:
            tags["behavior"].append("提问型")

        if not any(tags.values()):
            tags["behavior"].append("普通用户")

        return tags

    @staticmethod
    def infer_style(tags: dict) -> str:
        behavior = tags.get("behavior", [])
        if "吐槽倾向" in behavior:
            return "喜欢玩梗和互动"
        if "提问型" in behavior:
            return "偏理性，需要解释"
        if "情绪活跃" in behavior:
            return "情绪表达强"
        return "普通用户"

    @staticmethod
    def generate_aliases(name: str) -> list[str]:
        """从显示名自动生成别名：仅 2 字及以上片段（单字太容易误匹配）"""
        import re
        parts = re.split(r"[（）()]", name)
        result = []
        seen = set()

        for p in parts:
            p = p.strip()
            if len(p) >= 2 and p != name and p not in seen:
                seen.add(p)
                result.append(p)

        return ProfileUpdater._clean_aliases(result)

    async def rebuild_aliases(self, user_id: str | None = None):
        """按当前规则重建别名（可传用户ID重建单人，或重建全部）。"""
        if user_id:
            users = {str(user_id): get_user_sync(user_id)}
        else:
            users = get_users_sync()

        for uid, profile in users.items():
            if not isinstance(profile, dict):
                continue
            name = str(profile.get("name", ""))
            aliases = self.generate_aliases(name)
            await update_user(uid, {"aliases": aliases})

    async def auto_profile(self, user_id: str, message: str):
        """新用户首次发言时自动生成画像 + 别名"""
        user = get_user_sync(user_id)
        if user and user.get("style") != "新用户":
            return  # 已有画像，不覆盖

        tags = self.infer_tags(message)
        name = (user or {}).get("name", "")
        await update_user(user_id, {
            "tags": tags,
            "style": self.infer_style(tags),
            "aliases": self.generate_aliases(name),
        })
