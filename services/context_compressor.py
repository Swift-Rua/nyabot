"""
上下文压缩器 + 群聊历史追踪
- 记录每条消息到内存历史
- 构建 AI 上下文时压缩历史
"""
import time
from collections import defaultdict
from services.utils import is_noise, clean_text, short

# 每个群保留最近 N 条消息
MAX_HISTORY = 80

# { group_id: [ {role, content, time}, ... ] }
_group_history: dict[str, list[dict]] = defaultdict(list)


# ═══════════════════════════════════════════
# 消息记录（每次消息都调）
# ═══════════════════════════════════════════

def record_message(group_id: str, user_name: str, text: str):
    """记录一条消息到群历史"""
    t = clean_text(text)
    if not t:
        return

    _group_history[group_id].append({
        "role": user_name,
        "content": t,
        "time": time.time(),
    })

    # 裁剪
    if len(_group_history[group_id]) > MAX_HISTORY:
        _group_history[group_id] = _group_history[group_id][-MAX_HISTORY:]


# ═══════════════════════════════════════════
# 上下文压缩（构建 AI prompt 时调）
# ═══════════════════════════════════════════

def compress(group_id: str, max_items: int = 20) -> str:
    """
    从群历史中取最近 max_items 条，
    过滤噪声、标记事件，压缩为 AI 可读的上下文字符串。
    """
    messages = _group_history.get(group_id, [])
    if not messages:
        return ""

    recent = messages[-max_items:]
    lines = []

    for m in recent:
        role = m.get("role", "?")
        content = m.get("content", "")

        content = clean_text(content)
        if not content:
            continue

        # ── 标记强事件 ──
        if "召唤术" in content:
            lines.append(f"[🔥召唤] {role}: {content}")
            continue

        # ── 过滤噪声 ──
        if is_noise(content):
            continue

        # ── 截断 ──
        content = short(content, 80)

        lines.append(f"{role}: {content}")

    return "\n".join(lines)


# ═══════════════════════════════════════════
# 群状态推断
# ═══════════════════════════════════════════

def get_group_state(group_id: str) -> str:
    """
    根据最近消息时间判断群活跃状态。
    hot  (<60s)  — 群很活跃，不宜插话
    normal       — 一般
    cold  (>180s) — 冷场，可以主动发言
    """
    msgs = _group_history.get(group_id, [])
    if not msgs:
        return "cold"

    last = msgs[-1].get("time", 0)
    diff = time.time() - last

    if diff < 60:
        return "hot"
    elif diff < 180:
        return "normal"
    else:
        return "cold"


def get_group_ids() -> list[str]:
    """返回当前内存中存在历史记录的群号列表。"""
    return list(_group_history.keys())
