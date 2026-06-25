"""
人设稳定器 — 检测 AI 输出是否违反牛牛喵人设，违规则自动重新生成。
防止突然切百科模式、承认自己是 AI、一本正经说教。
"""
import re
import random

_FALLBACK = ("喵~", "好家伙", "诶嘿", "啊这")

# ── 违禁模式 ──
FORBIDDEN_PATTERNS: list[tuple[str, str]] = [
    # (正则, 说明)
    (r"作为.{0,5}AI", "自称AI"),
    (r"作为.{0,5}人工智能", "自称人工智能"),
    (r"我是.{0,5}AI", "自称AI"),
    (r"我是.{0,5}人工智能", "自称人工智能"),
    (r"根据.{0,5}资料", "百科模式"),
    (r"根据.{0,5}数据", "百科模式"),
    (r"根据.{0,5}查询", "百科模式"),
    (r"搜索.{0,5}结果", "百科模式"),
    (r"据.{0,5}了解", "百科模式"),
    (r"回答你.{0,5}问题", "问答模式"),
    (r"我来.{0,5}解答", "问答模式"),
    (r"让我.{0,5}回答", "问答模式"),
    (r"这是.{0,5}定义", "词典模式"),
    (r"意思是.{0,10}指", "词典模式"),
    (r"您好.{0,5}我是", "客服模式"),
    (r"很高兴.{0,5}为您", "客服模式"),
    (r"请问.{0,5}需要", "客服模式"),
    (r"我.{0,3}无法.{0,5}回答", "拒绝回答"),
    (r"我.{0,3}不能.{0,5}回答", "拒绝回答"),
    (r"抱歉.{0,3}无法", "拒绝回答"),
]

# 自动重试时追加的警告
RETRY_HINT = (
    "\n\n⚠️ 你上一句回复违反了人设（听起来像AI/百科/客服）。"
    "你是QQ群成员牛牛喵，不是AI助手。用群友聊天语气重新说，不要超过50字。"
)


def check(text: str) -> list[str]:
    """
    检查回复是否违规。
    返回违规类型列表，空列表表示通过。
    """
    violations = []
    for pattern, label in FORBIDDEN_PATTERNS:
        if re.search(pattern, text):
            violations.append(label)
    return violations


async def safe_reply(
    ask_ai_func,
    message: str,
    user_id: str,
    group_id: str | None = None,
    sender_name: str | None = None,
    mentioned_ids: list[str] | None = None,
    mood_state: dict | None = None,
    max_retries: int = 2,
) -> str:
    """
    调用 AI 生成回复，自动检测并重试违规输出。
    最多重试 max_retries 次，全部失败则返回最后一次结果。
    """
    last_reply = ""
    attempt = 0

    while attempt <= max_retries:
        if attempt == 0:
            reply = await ask_ai_func(
                message=message,
                user_id=user_id,
                group_id=group_id,
                sender_name=sender_name,
                mentioned_ids=mentioned_ids,
                mood_state=mood_state,
            )
        else:
            # 重试：消息追加人设警告
            reply = await ask_ai_func(
                message=message + RETRY_HINT,
                user_id=user_id,
                group_id=group_id,
                sender_name=sender_name,
                mentioned_ids=mentioned_ids,
                mood_state=mood_state,
            )

        reply = (reply or "").strip()
        if not reply:
            return last_reply or random.choice(_FALLBACK)

        last_reply = reply
        violations = check(reply)

        if not violations:
            return reply

        print(f"[persona_guard] retry {attempt+1}: {violations}")
        attempt += 1

    return last_reply
