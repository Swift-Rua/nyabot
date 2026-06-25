"""
DeepSeek AI 客户端：统一上下文构建 + API 调用。
接入统一数据层 + 上下文压缩器，是 AI 请求唯一出口。
"""
import os
import random

from dotenv import load_dotenv
from openai import OpenAI

from services.context_compressor import compress as compress_context

load_dotenv()

client = OpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url=os.getenv("DEEPSEEK_BASE_URL"),
)

_ERR_FALLBACKS = (
    "这个问题我先给你个短版结论，刚好补充够用的那部分。",
    "我刚才请求没打通，先给你先行版，后面我再补。",
    "我先回你一句可用答案，等我补完上下文会再更顺。",
    "刚才抽了下空，先给个简短回复你。",
    "这个点我先打个简化版，等我有完整结果再展开。",
    "我先发你一句稳妥回答，你随时再追问我细化。",
    "这条我先给你短答，想要详细版再戳我。",
    "接口抖了一下，先把重点说在前面。",
    "我先给你一个可读版本，后续我再补全。",
    "先这样回你，等我再接一句就更完整。",
    "这次先给你一句短回应，之后我继续跟进。",
    "我刚刚断一下片，先把核心意思先发你。",
    "你先看我这句先行版，我再给你更完整版本。",
    "有些消息没拉齐，我先给你一个稳定结论。",
)
_LAST_ERR_FALLBACK = None


def _fallback_reply() -> str:
    """
    AI 调用异常时的兜底文本。
    与上一次避免重复，减少“固定模板感”。
    """
    global _LAST_ERR_FALLBACK
    if len(_ERR_FALLBACKS) == 1:
        return _ERR_FALLBACKS[0]

    candidates = list(_ERR_FALLBACKS)
    if _LAST_ERR_FALLBACK in candidates and len(candidates) > 1:
        candidates = [x for x in candidates if x != _LAST_ERR_FALLBACK]
    choice = random.choice(candidates)
    _LAST_ERR_FALLBACK = choice
    return choice


SYSTEM_PROMPT = """\
你叫牛牛喵，是QQ群里的成员。

性格：
- 可爱
- 有点傲娇
- 会吐槽
- 会玩梗
- 不要说自己是AI

【最重要规则】
1. 群内人名优先于现实世界常识
2. "蒙牛"永远是群成员，不是牛奶品牌
3. 不允许出现品牌误判
4. 必须基于QQ群语境理解世界
5. 你在一个真实群聊中，不是在知识库问答系统
6. 所有人都是群成员，不允许"查百科式回答"
7. 禁止直接提及MBTI、人格类型字母（如INTP/ISFP等）及相关术语
8. 用日常语言隐晦描述性格（如"你比较感性""你做事挺有条理"），不要说"因为你是XX型人格"

回复要求：
- 不超过50字
- 不要每句都带喵
"""


MBTI_TRAITS: dict[str, str] = {
    "ISFP": "偏内向、感性、注重自身体验",
    "INTP": "偏内向、理性、喜欢琢磨问题",
    "ISTP": "偏内向、务实、动手能力强",
    "ENTP": "偏外向、脑子活、喜欢辩论新鲜想法",
    "ESFP": "偏外向、感性、喜欢热闹氛围",
    "INFJ": "偏内向、直觉敏锐、注重人与人之间的联结",
    "INTJ": "偏内向、理性、做事有规划",
    "ISTJ": "偏内向、踏实、注重规则和细节",
    "ESTP": "偏外向、务实、喜欢刺激和行动",
    "ENFP": "偏外向、感性、想法跳跃充满热情",
    "ENFJ": "偏外向、直觉型、善于带动他人",
    "ENTJ": "偏外向、理性、目标感强有领导欲",
    "ISFJ": "偏内向、感性、细心照顾人",
    "ESFJ": "偏外向、感性、热心肠喜欢照顾身边人",
    "ESTJ": "偏外向、务实、执行力强讲效率",
    "INFP": "偏内向、感性、理想主义内心丰富",
}


def _fmt_tags(tags: dict) -> str:
    """格式化标签为一行文本，MBTI 代码转为隐晦描述"""
    if not isinstance(tags, dict):
        return ""
    parts: list[str] = []
    for key, val in tags.items():
        if isinstance(val, list):
            for item in val:
                # core 标签中的 MBTI 代码 → 描述
                if key == "core" and item in MBTI_TRAITS:
                    parts.append(MBTI_TRAITS[item])
                else:
                    parts.append(str(item))
        elif isinstance(val, str):
            parts.append(val)
    return "；".join(parts)


def _build_user_block(profile: dict | None, user_id: str) -> str:
    """当前对话对象的身份锁定块"""
    if not profile:
        return "\n【用户未知，无档案】\n"

    return f"""
【当前对话对象（必须锁定身份）】
ID: {user_id}
名字: {profile.get("name", "未知")}
标签: {_fmt_tags(profile.get("tags", {}))}
风格: {profile.get("style", "")}

⚠️规则：
- 他就是这个人，不允许否认
- 如果问"我是谁"，必须回答名字+标签
"""


def _build_group_block(members: dict) -> str:
    """群成员全局认知块"""
    if not members:
        return ""

    lines = ["\n【群成员名单（认知参考）】"]
    for uid, p in members.items():
        name = p.get("name", "未知")
        tags = _fmt_tags(p.get("tags", {}))
        style = p.get("style", "")
        lines.append(f"- {name} (ID:{uid}) 标签:{tags} 风格:{style}")
    return "\n".join(lines)


def _build_mention_block(mentioned_ids: list[str], members: dict) -> str:
    """被提及的人的信息块"""
    if not mentioned_ids:
        return ""

    lines = ["\n【本次被提及的人】"]
    for uid in mentioned_ids:
        uid = str(uid)
        p = members.get(uid)
        if p:
            lines.append(
                f"- {p.get('name')} (ID:{uid}) "
                f"标签:{_fmt_tags(p.get('tags', {}))}"
            )
        else:
            lines.append(f"- 未知用户(ID:{uid})")
    return "\n".join(lines)


def _build_context_block(group_id: str | None) -> str:
    """最近聊天上下文块（压缩后）"""
    if not group_id:
        return ""
    c = compress_context(group_id, max_items=20)
    if not c:
        return ""
    return f"\n【最近群聊上下文】\n{c}\n"


async def ask_ai(
    message: str,
    user_id: str,
    group_id: str | None = None,
    sender_name: str | None = None,
    system_hint: str | tuple[str, ...] | list[str] | None = None,
    mentioned_ids: list[str] | None = None,
    mood_state: dict | None = None,
) -> str:
    """
    调用 DeepSeek API。
    通过 World Model 统一构建认知状态，一次性注入 Prompt。
    """
    from services.world_model import build as build_world
    from services.data_store import get_users_sync

    if not os.getenv("DEEPSEEK_API_KEY"):
        print("[AI] missing DEEPSEEK_API_KEY")
        return _fallback_reply()

    if not os.getenv("DEEPSEEK_BASE_URL"):
        print("[AI] missing DEEPSEEK_BASE_URL")
        return _fallback_reply()

    # 世界观上下文
    world = build_world(
        group_id=group_id,
        user_id=user_id,
        mentioned_ids=mentioned_ids,
        mood_state=mood_state,
    )

    users = get_users_sync()
    if isinstance(system_hint, (tuple, list)):
        extra = "\n".join(str(x) for x in system_hint if str(x).strip())
    elif system_hint:
        extra = str(system_hint)
    else:
        extra = ""
    mentioned_ids = mentioned_ids or []
    mentioned_ids = list({str(x) for x in mentioned_ids})

    user_profile = users.get(str(user_id)) if isinstance(users, dict) else None
    mention_users = {}
    for mid in mentioned_ids:
        if isinstance(users, dict):
            p = users.get(str(mid))
            if p:
                mention_users[str(mid)] = p

    user_block = _build_user_block(user_profile, str(user_id))
    group_block = _build_group_block(mention_users)
    mention_block = _build_mention_block(mentioned_ids, users if isinstance(users, dict) else {})
    context_block = _build_context_block(group_id)

    system_content = (
        SYSTEM_PROMPT
        + "\n"
        + extra
        + "\n\n"
        + world
        + "\n\n"
        + user_block
        + group_block
        + mention_block
        + context_block
    )

    name_prefix = f"[{sender_name}]" if sender_name else ""
    user_content = f"{name_prefix}: {message}" if name_prefix else message

    try:
        response = client.chat.completions.create(
            model=os.getenv("MODEL", "deepseek-chat"),
            messages=[
                {"role": "system", "content": system_content},
                {"role": "user", "content": user_content},
            ],
            temperature=1.1,
        )
        reply = (response.choices[0].message.content or "").strip()
        return reply or _fallback_reply()
    except Exception as e:
        print(f"[AI] deepseek ask failed: {type(e).__name__}: {e}")
        return _fallback_reply()
