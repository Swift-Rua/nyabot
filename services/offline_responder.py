"""Offline no-token response helpers."""

from __future__ import annotations

import json
import os
import random

from services.message_logger import fetch_recent_messages, get_member_profile
from services.relation import get_top_friends, get_top_rivals
from services.data_store import get_users_sync
from services.utils import clean_text


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES_FILE = os.path.join(BASE_DIR, "data", "templates.json")

_DEFAULT_TEMPLATES = [
    "{speaker} 的发言在最近 {active_days} 天里比较稳定，当前互动强度 {message_count}。",
    "{speaker} 最近常提到“{topic}”，最近一个热点方向是 {topic}。",
    "看起来这个群里 {best_friend} 最近互动很频繁，{speaker} 的回复节奏也不错。",
    "系统观测：{speaker} 在 {active_hour} 点最活跃，最近平均长度 {average_length} 字。",
    "无token 下的回退回复：{speaker} 给出的线索是“{topic}”，你接着聊就好。",
]

_DEFAULT_STOP_TOPICS = {"群", "表情", "这个", "那个", "那个", "你", "我", "他", "她", "它"}
_MAX_MARKOV_STEPS = 3
_MAX_MARKOV_POOL = 360


def _load_templates() -> list[str]:
    if not os.path.exists(TEMPLATES_FILE):
        return list(_DEFAULT_TEMPLATES)

    try:
        with open(TEMPLATES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            templates = data.get("templates")
        else:
            templates = data
        if not isinstance(templates, list):
            return list(_DEFAULT_TEMPLATES)
        cleaned = [str(t).strip() for t in templates if str(t).strip()]
        if not cleaned:
            return list(_DEFAULT_TEMPLATES)
        return cleaned
    except Exception:
        return list(_DEFAULT_TEMPLATES)


def _safe_value(value: object, fallback: str = "") -> str:
    if value is None:
        return fallback
    s = str(value).strip()
    return s if s else fallback


def _extract_topic(profile: dict | None, recent_msgs: list[str]) -> str:
    if profile:
        topic = _safe_value(profile.get("most_common_word"), "")
        if topic and topic not in _DEFAULT_STOP_TOPICS:
            return topic

    for text in recent_msgs:
        if text:
            token = clean_text(text).split()
            if token:
                for t in token:
                    if len(t) >= 2 and t not in _DEFAULT_STOP_TOPICS:
                        return t
    return "讨论"


def _build_markov_chain(messages: list[str]) -> dict[str, list[str]]:
    chain: dict[str, list[str]] = {}
    if len(messages) < 2:
        return chain

    for first, second in zip(messages, messages[1:]):
        chain.setdefault(first, []).append(second)
    return chain


def _build_context(group_id: str | None, user_id: str, mentioned_ids: list[str] | None = None) -> dict[str, str]:
    users = get_users_sync()
    profile = get_member_profile(group_id, user_id) if group_id else None
    friends = get_top_friends(user_id, group_id=group_id, limit=1)
    rivals = get_top_rivals(user_id, group_id=group_id, limit=1)

    top_friend_id = friends[0].get("target_id") if friends else ""
    top_rival_id = rivals[0].get("target_id") if rivals else ""

    context = {
        "speaker": str(users.get(user_id, {}).get("name", user_id)),
        "sender_id": str(user_id),
        "group_id": _safe_value(group_id, "global"),
        "active_days": _safe_value((profile or {}).get("active_days"), "0"),
        "streak_days": _safe_value((profile or {}).get("streak_days"), "0"),
        "message_count": _safe_value((profile or {}).get("message_count"), "0"),
        "average_length": _safe_value((profile or {}).get("average_length"), "0"),
        "active_hour": _safe_value((profile or {}).get("most_active_hour"), "12"),
        "best_friend": _safe_value(users.get(top_friend_id, {}).get("name") if isinstance(users, dict) else "", top_friend_id),
        "worst_friend": _safe_value(users.get(top_rival_id, {}).get("name") if isinstance(users, dict) else "", top_rival_id),
        "mentioned_count": str(len(mentioned_ids or [])),
        "mentioned": ", ".join(_safe_value(m) for m in (mentioned_ids or [])),
    }

    return context


def _select_template(context: dict[str, str]) -> str:
    templates = _load_templates()
    if not templates:
        return ""
    template = random.choice(templates)
    class _SafeFormatDict(dict):
        def __missing__(self, key):
            return ""

    return template.format_map(_SafeFormatDict(context))


def _collect_recent_messages(group_id: str | None) -> list[str]:
    rows = fetch_recent_messages(group_id=group_id, limit=_MAX_MARKOV_POOL)
    texts: list[str] = []
    for row in rows:
        text = _safe_value(row.get("message"), "").strip()
        if not text:
            continue
        texts.append(clean_text(text))
    return texts


def build_markov_reply(group_id: str | None = None, seed: str | None = None) -> str | None:
    texts = _collect_recent_messages(group_id)
    if len(texts) < 2:
        return None

    chain = _build_markov_chain(texts)
    if not chain:
        return None

    starts = texts[:] if not seed else [t for t in texts if seed and seed in t]
    if not starts:
        starts = [texts[0], texts[min(len(texts) // 2, len(texts) - 1)], texts[-1]]
    current = random.choice(starts)
    out = [current]

    for _ in range(_MAX_MARKOV_STEPS - 1):
        next_candidates = chain.get(current)
        if not next_candidates:
            break
        current = random.choice(next_candidates)
        if not current or current in out:
            if len(out) >= 2:
                break
        out.append(current)

    reply = "\n".join([item for item in out if item]).strip()
    return reply or None


def _build_topic_text(group_id: str | None, user_id: str, seed: str | None = None) -> str:
    recent = _collect_recent_messages(group_id)
    profile = get_member_profile(group_id, user_id) if group_id else None
    topic = _extract_topic(profile, recent)
    if seed:
        topic_seed = clean_text(seed).strip()
        if topic_seed and topic_seed not in topic:
            return f"{topic_seed} {topic}"
    return topic


def build_no_token_reply(
    group_id: str | None,
    user_id: str,
    mentioned_ids: list[str] | None = None,
    seed: str | None = None,
    mood_state: dict | None = None,
) -> str | None:
    if mood_state and isinstance(mood_state, dict):
        try:
            # 简单地加入情绪因子，不影响生成稳定性
            if int(mood_state.get("social", 50)) >= 80:
                pass
        except Exception:
            pass

    context = _build_context(group_id, user_id, mentioned_ids=mentioned_ids)
    recent = _collect_recent_messages(group_id)
    context["topic"] = _build_topic_text(group_id, user_id, seed=seed)

    reply = _select_template(context)
    if reply and not reply.isspace():
        return reply

    markov = build_markov_reply(group_id=group_id, seed=seed)
    if markov:
        return markov

    return None
