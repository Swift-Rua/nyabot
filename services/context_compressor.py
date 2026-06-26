"""Context history buffer used for AI prompt compression."""

import time
from collections import defaultdict

from services.message_logger import (
    fetch_recent_messages,
    get_active_group_ids,
    get_latest_message_time,
    log_message,
)
from services.utils import clean_text, is_noise, short

# per-group short in-memory cache
MAX_HISTORY = 80
_group_history: dict[str, list[dict]] = defaultdict(list)


def _to_bool(v: object) -> bool:
    return bool(v)


def _normalize_at_list(at_list: object) -> list[str]:
    if not isinstance(at_list, list):
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for item in at_list:
        uid = str(item).strip()
        if not uid or uid in seen:
            continue
        normalized.append(uid)
        seen.add(uid)
    return normalized


def record_message(
    group_id: str,
    user_name: str,
    text: str,
    *,
    user_id: str | None = None,
    message_id: str | int | None = None,
    reply_to: str | int | None = None,
    at_list: list[str] | None = None,
    message_type: str | None = "group",
    has_image: bool = False,
    has_face: bool = False,
    face_ids: list[int] | None = None,
):
    """Record one message into rolling cache and SQLite history."""
    t = clean_text(text)
    now = time.time()
    content = t

    # keep in-process fallback
    _group_history[group_id].append({
        "role": user_name,
        "content": content,
        "time": now,
        "user_id": user_id,
        "has_image": _to_bool(has_image),
        "has_face": _to_bool(has_face),
        "message_id": str(message_id) if message_id is not None else None,
        "reply_to": str(reply_to) if reply_to is not None else None,
        "message_type": str(message_type or "group").strip() or "group",
        "at_list": _normalize_at_list(at_list),
        "length": len(content),
        "face_ids": list(face_ids or []),
    })
    if len(_group_history[group_id]) > MAX_HISTORY:
        _group_history[group_id] = _group_history[group_id][-MAX_HISTORY:]

    if not group_id or not user_id:
        return
    if not content and not has_image and not has_face:
        return

    try:
        log_message(
            group_id=group_id,
            user_id=user_id,
            user_name=user_name,
            message=content,
            message_id=message_id,
            reply_to=reply_to,
            at_list=at_list,
            message_type=message_type,
            has_image=_to_bool(has_image),
            has_face=_to_bool(has_face),
            face_ids=face_ids,
        )
    except Exception as e:
        print(f"[context] history logger failed: {type(e).__name__}: {e}")


def compress(group_id: str, max_items: int = 20) -> str:
    """Build a concise context block from most recent group messages."""
    messages = fetch_recent_messages(group_id, max_items=max_items)
    if not messages:
        messages = _group_history.get(group_id, [])

    if not messages:
        return ""

    recent = messages[-max_items:]
    lines: list[str] = []

    for m in recent:
        role = m.get("role", m.get("user_name", "?"))
        content = clean_text(m.get("content", m.get("message", "")))

        if not content:
            has_image = bool(m.get("has_image"))
            has_face = bool(m.get("has_face"))
            if has_image and has_face:
                content = "[图片+表情]"
            elif has_image:
                content = "[图片]"
            elif has_face:
                content = "[表情]"
            else:
                continue

        if is_noise(content):
            continue

        content = short(content, 80)
        lines.append(f"{role}: {content}")

    return "\n".join(lines)


def get_group_state(group_id: str) -> str:
    """
    Get group activity state from latest message timestamp.
    """
    last = get_latest_message_time(group_id)
    if last is None:
        msgs = _group_history.get(group_id, [])
        if not msgs:
            return "cold"
        last = msgs[-1].get("time", 0)

    diff = time.time() - float(last or 0)
    if diff < 60:
        return "hot"
    if diff < 180:
        return "normal"
    return "cold"


def get_group_ids() -> list[str]:
    """Return group ids seen by in-memory cache or history DB."""
    return list(set(_group_history.keys()) | set(get_active_group_ids()))
