"""
主动发言模块 — 后台循环，在群冷场时主动找话题。
使用统一数据层 + 异步安全写入。
"""
import time
import random
import asyncio

from nonebot import get_bot

from plugins.summon import group_state, group_last_active, check_auto, group_voice_mode
from services.data_store import get_users_sync, update_affinity, ensure_user, get_affinity, ensure_member_in_group, ensure_group_meta
from services.deepseek_client import ask_ai
from services.tts import send_voice_reply
from services.mood import get as get_mood, decay as decay_mood

_BOT_API_TIMEOUT = 8.0


# ═══════════════════════════════════════════
# 成员记录（有新消息时调用）
# ═══════════════════════════════════════════

async def record_member(group_id: str, user_id: str, group_name: str | None = None):
    """记录群成员存在，不存在则自动创建"""
    await ensure_user(user_id)
    await ensure_member_in_group(group_id, user_id)
    await ensure_group_meta(group_id, group_name)


# ═══════════════════════════════════════════
# 选择目标（v0.8 升级：定向搭话）
# ═══════════════════════════════════════════

def _days_since(date_str: str) -> int:
    """从日期字符串计算距今天数"""
    if not date_str:
        return 99
    try:
        t = time.strptime(date_str[:5], "%m-%d")  # "06-23 14:30" → (6,23)
        now = time.localtime()
        days = (now.tm_yday - t.tm_yday) % 365
        return days
    except Exception:
        return 99


def pick_target() -> tuple[str | None, str]:
    """
    从已知用户中选一个发言目标。
    返回 (user_id, 原因标签): "missing"/"friend"/"random"
    优先选很久没出现的人。
    """
    users = get_users_sync()
    if not users:
        return None, ""

    user_ids = list(users.keys())
    weights: list[float] = []
    reasons: list[str] = []

    for u in user_ids:
        score = 1.0
        reason = "random"

        # 很久没出现（3天+）→ 高权重
        last_seen = users[u].get("meta", {}).get("last_seen", "")
        days = _days_since(last_seen)
        if days >= 3:
            score += 20.0
            reason = "missing"

        # 亲密度高 → 中等权重
        for v in user_ids:
            if u == v:
                continue
            aff = get_affinity(u, v)
            if aff > 70:
                score += 5.0
                if reason == "random":
                    reason = "friend"

        weights.append(score)
        reasons.append(reason)

    idx = random.choices(range(len(user_ids)), weights=weights, k=1)[0]
    return user_ids[idx], reasons[idx]


# ═══════════════════════════════════════════
# 群状态
# ═══════════════════════════════════════════

def _group_state_str(group_id: str) -> str:
    """根据最后活跃时间判断冷热"""
    last = group_last_active.get(group_id, 0)
    diff = time.time() - last
    if diff < 60:
        return "hot"
    elif diff < 180:
        return "normal"
    else:
        return "cold"


# ═══════════════════════════════════════════
# 构建主动发言 prompt（v0.8 升级：上下文感知）
# ═══════════════════════════════════════════

def _build_proactive_prompt(user_id: str, reason: str) -> str:
    user = get_users_sync().get(user_id, {})
    name = user.get("name", "某人")
    impression = user.get("meta", {}).get("impression", "")
    last_seen = user.get("meta", {}).get("last_seen", "")
    days = _days_since(last_seen)

    # 获取最近的长期记忆
    from services.memory import get_for as memory_for
    memories = memory_for(user_id, limit=2)
    memory_text = ""
    if memories:
        memory_text = "\n你知道关于他的事：\n" + "\n".join(
            f"  - {m['text']}" for m in memories
        )

    if reason == "missing":
        return f"""\
你在QQ群中扮演真实群友。

{name} 已经 {days} 天没出现{('了，他之前：' + impression) if impression else '了'}。{memory_text}

请自然地问候或调侃他：
- 不超过30字
- 像真人关心朋友一样
- 参考你知道的事来问（如"搬家收拾完了吗"而不是"你搬家了吗"）
- 不要说你是AI
"""
    else:
        return f"""\
你在QQ群中扮演真实群友。

你当前要对 {name} 说话。{('他：' + impression) if impression else ''}{memory_text}

请结合群内关系自然说一句话：
- 不超过30字
- 可以调侃/站队/吐槽
- 不要说你是AI
"""


# ═══════════════════════════════════════════
# 主动发言逻辑
# ═══════════════════════════════════════════

async def try_proactive_say(group_id: str):
    group_id = str(group_id)

    # 未召唤时不发言
    if not group_state.get(group_id, False):
        return

    # 群很活跃时不插话
    state = _group_state_str(group_id)
    if state == "hot":
        return

    # 最近有消息不插话
    now = time.time()
    last = group_last_active.get(group_id, now)
    if now - last < 180:
        return

    # 选目标（v0.8：定向搭话）
    target_user, reason = pick_target()
    if not target_user:
        return

    # 随机选一个"互动对象"，更新亲密度
    users = get_users_sync()
    other_ids = [u for u in users if u != target_user]
    if other_ids:
        related = random.choice(other_ids)
        await update_affinity(target_user, related, random.randint(1, 5))

    prompt = _build_proactive_prompt(target_user, reason)

    try:
        mood = get_mood()
        reply = await ask_ai(prompt, target_user, group_id=group_id, mood_state=mood)
    except Exception as e:
        print(f"[proactive] AI error: {e}")
        return

    if not reply:
        return

    try:
        if group_voice_mode.get(group_id, False):
            await asyncio.wait_for(send_voice_reply(int(group_id), reply), timeout=_BOT_API_TIMEOUT)
        else:
            bot = get_bot()
            await asyncio.wait_for(
                bot.send_group_msg(group_id=int(group_id), message=reply),
                timeout=_BOT_API_TIMEOUT,
            )
    except Exception as e:
        print(f"[proactive] send error: {e}")


# ═══════════════════════════════════════════
# 后台主循环
# ═══════════════════════════════════════════

async def proactive_loop():
    await asyncio.sleep(5)
    decay_counter = 0

    while True:
        await asyncio.sleep(60)
        # 每 10 分钟衰减一次亲密度
        decay_counter += 1
        if decay_counter >= 10:
            decay_counter = 0
            from services.relation import decay_all
            await decay_all()
            decay_mood()

        for group_id in list(group_state.keys()):
            try:
                check_auto(group_id)
                await try_proactive_say(group_id)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[proactive] loop error: {e}")
