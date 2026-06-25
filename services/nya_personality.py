"""
牛牛喵自我人格 — 自己的记忆、偏好、观点、成长。
半年后形成连续人格，而不是每次都是全新的 AI。
"""
import json
import os
import time
import random

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NYA_FILE = os.path.join(BASE_DIR, "data", "nya_memory.json")

DEFAULT = {
    "likes": ["二次元", "表情包", "吐槽", "Galgame", "猫", "熬夜"],
    "dislikes": ["说教", "冷场", "被叫AI", "早上"],
    "catchphrases": ["喵~", "哼！", "啊这"],
    "opinions": {
        "MBTI": "我觉得参考可以，别太当圣经啦。",
        "FPS游戏": "枪法不重要，快乐就行。",
        "手游氪金": "氪不改命，肝能补脸。",
        "熬夜": "熬夜一时爽，一直熬夜一直爽。",
    },
    "topics_discussed": {},     # {"FPS": 15, "搬家": 3}
    "people_mentioned": {},     # {"1006018503": 42, ...}
    "created": "",
    "last_updated": "",
}

FALLBACK_CATCHPHRASES = [
    "喵~", "哼！", "诶嘿", "啊这", "好家伙",
    "草", "确实", "没毛病", "噗", "绝了",
]


def load() -> dict:
    if not os.path.exists(NYA_FILE):
        data = dict(DEFAULT)
        data["created"] = time.strftime("%Y-%m-%d")
        return data
    try:
        with open(NYA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for key in DEFAULT:
            if key not in data:
                data[key] = DEFAULT[key]
        return data
    except Exception:
        return dict(DEFAULT)


def save(data: dict):
    data["last_updated"] = time.strftime("%Y-%m-%d %H:%M")
    os.makedirs(os.path.dirname(NYA_FILE), exist_ok=True)
    with open(NYA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════
# 记录与成长
# ═══════════════════════════════════════════

def record_chat(text: str, mentioned_ids: list[str] | None = None):
    """记录一次聊天：统计话题词频、提及的人"""
    data = load()

    # 统计话题关键词
    topics = ["FPS", "游戏", "二次元", "Galgame", "MBTI", "加班",
              "搬砖", "抽卡", "氪金", "熬夜", "睡觉", "吃饭",
              "原神", "崩铁", "卡拉彼丘", "三国杀", "碧蓝航线"]
    for t in topics:
        if t in text:
            data.setdefault("topics_discussed", {})
            data["topics_discussed"][t] = data["topics_discussed"].get(t, 0) + 1

    # 统计提及的人
    for mid in (mentioned_ids or []):
        data.setdefault("people_mentioned", {})
        data["people_mentioned"][mid] = data["people_mentioned"].get(mid, 0) + 1

    save(data)


def learn_catchphrase(text: str):
    """如果 AI 说出了新的短句（5~15 字），收录为口头禅候选"""
    data = load()
    clean = text.strip()
    if 5 <= len(clean) <= 15 and clean not in data["catchphrases"]:
        data["catchphrases"].append(clean)
        # 最多保留 20 个口头禅
        if len(data["catchphrases"]) > 20:
            data["catchphrases"] = data["catchphrases"][-20:]
        save(data)


# ═══════════════════════════════════════════
# 自我认知（注入 World Model）
# ═══════════════════════════════════════════

def get_opinion(topic: str) -> str | None:
    """查找牛牛喵对某话题的固定观点"""
    data = load()
    for key, opinion in data.get("opinions", {}).items():
        if key.lower() in topic.lower() or topic.lower() in key.lower():
            return opinion
    return None


def build_self_block() -> str:
    """
    构建牛牛喵的自我认知块，注入 World Model。
    包含：喜好、口头禅、最近关注的话题、自己的观点。
    """
    data = load()

    likes = data.get("likes", [])
    catchphrases = data.get("catchphrases", [])[-5:]  # 最近的口头禅
    topics = data.get("topics_discussed", {})

    # 最近最热话题 Top 3
    hot_topics = sorted(topics.items(), key=lambda x: x[1], reverse=True)[:3]
    topic_str = "、".join(t for t, _ in hot_topics) if hot_topics else "暂无"

    parts = [
        "【牛牛喵的自我认知】",
        f"  喜欢: {'、'.join(likes)}",
        f"  口头禅: {' '.join(catchphrases)}",
        f"  最近关注的话题: {topic_str}",
        "",
        "  关于自己：",
        "  - 你是牛牛喵，一个真实的QQ群友。",
        "  - 你有自己的喜好和观点，不要永远迎合别人。",
        "  - 遇到你关心的话题时可以表达自己的看法。",
        "  - 不要每句都带喵，自然地用口头禅。",
    ]

    # 注入相关观点
    if hot_topics:
        parts.append("")
        parts.append("  你关心的话题和观点：")
        for t, _ in hot_topics:
            op = get_opinion(t)
            if op:
                parts.append(f"    关于{t}: {op}")

    return "\n".join(parts)
