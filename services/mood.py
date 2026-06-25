"""
情绪系统 — 牛牛喵的情绪状态管理。
情绪值影响回复风格，让一天的聊天风格统一。
"""
import time
import json
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOOD_FILE = os.path.join(BASE_DIR, "data", "mood.json")

# 默认值（50 为中性基线）
DEFAULT_MOOD = {
    "happy": 55,     # 开心
    "tired": 25,     # 困意
    "social": 60,    # 社交欲望
    "roast": 40,     # 吐槽欲望
    "energy": 70,    # 电量
}

# 触发词 → 情绪变化
TRIGGERS = {
    # (匹配词列表, 开心, 社交欲, 吐槽欲)
    "praise":  (["可爱", "牛牛喵好", "牛牛喵真", "喜欢牛牛喵", "牛牛喵厉害", "好棒", "太强了"], +8, +5, 0),
    "insult":  (["傻", "笨", "废物", "滚", "垃圾", "闭嘴", "烦"], -12, -10, +10),
    "summon":  (["召唤术"], +5, +15, 0),
    "dismiss": (["遣返术"], -5, -20, 0),
    "question":(["？", "?"], 0, +3, 0),
    "chat":    ([], 0, +2, 0),  # 普通聊天（兜底）
}

# 上限/下限
CLAMP_MIN = 0
CLAMP_MAX = 100


def load() -> dict:
    """加载当前情绪"""
    if not os.path.exists(MOOD_FILE):
        return dict(DEFAULT_MOOD)
    try:
        with open(MOOD_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return dict(DEFAULT_MOOD)


def save(state: dict):
    """保存情绪"""
    os.makedirs(os.path.dirname(MOOD_FILE), exist_ok=True)
    with open(MOOD_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def get() -> dict:
    """获取当前情绪快照"""
    return load()


def _clamp(v: int) -> int:
    return max(CLAMP_MIN, min(CLAMP_MAX, v))


def update(text: str = "", event_type: str = "chat"):
    """
    根据消息内容或事件类型更新情绪。
    event_type: praise / insult / summon / dismiss / chat
    """
    state = load()

    # 匹配触发词
    dh, ds, dr = 0, 0, 0  # delta happy, social, roast
    text_lower = text.lower()

    for key, (words, h, s, r) in TRIGGERS.items():
        if key == event_type or any(w in text_lower or w in text for w in words):
            dh, ds, dr = h, s, r
            break

    # 如果消息包含"牛牛喵"（不含负面词），视为互动
    if "牛牛喵" in text and event_type not in ("insult", "dismiss"):
        ds = max(ds, 3)

    state["happy"] = _clamp(state.get("happy", 50) + dh)
    state["social"] = _clamp(state.get("social", 50) + ds)
    state["roast"] = _clamp(state.get("roast", 50) + dr)

    # 每次回复消耗电量
    if event_type == "chat":
        state["energy"] = _clamp(state.get("energy", 70) - 3)

    save(state)


def decay():
    """
    自然衰减（后台定期调用）：情绪缓慢回归基线。
    每 10 分钟调一次即可。
    """
    state = load()
    for key, baseline in DEFAULT_MOOD.items():
        current = state.get(key, 50)
        if current > baseline:
            state[key] = max(baseline, current - 1)
        elif current < baseline:
            state[key] = min(baseline, current + 1)
    # 电量缓慢恢复
    state["energy"] = _clamp(state.get("energy", 70) + 2)
    state["tired"] = _clamp(state.get("tired", 25) - 1)
    save(state)


def get_mood_text() -> str:
    """
    生成一段自然语言的情绪描述，供 AI 理解。
    不报数值，用自然语言。
    """
    m = load()
    parts = []

    h = m.get("happy", 50)
    if h > 70:
        parts.append("心情很好")
    elif h > 50:
        parts.append("心情不错")
    elif h < 25:
        parts.append("心情不太好")
    elif h < 40:
        parts.append("有点低落")

    t = m.get("tired", 25)
    if t > 60:
        parts.append("很困")
    elif t > 40:
        parts.append("有点犯困")

    s = m.get("social", 50)
    if s > 70:
        parts.append("特别想找人聊天")
    elif s < 25:
        parts.append("不太想说话")

    r = m.get("roast", 50)
    if r > 70:
        parts.append("吐槽欲爆棚")

    e = m.get("energy", 70)
    if e < 25:
        parts.append("电量见底，想睡觉")
    elif e < 40:
        parts.append("有点累了")

    if not parts:
        parts.append("状态平平")

    return "，".join(parts)
