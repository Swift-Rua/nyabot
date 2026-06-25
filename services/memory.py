"""Memory storage and retrieval for long-term user-context facts."""

import json
import os
import time

from services.data_store import get_core_members

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEMORY_FILE = os.path.join(BASE_DIR, "data", "memory.json")

MAX_FACTS = 200


def load() -> dict:
    if not os.path.exists(MEMORY_FILE):
        return {"facts": []}
    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if "facts" not in data:
                data["facts"] = []
            return data
    except Exception:
        return {"facts": []}


def save(data: dict):
    os.makedirs(os.path.dirname(MEMORY_FILE), exist_ok=True)
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def add(
    text: str,
    related_users: list[str] | None = None,
    group_id: str | None = None,
):
    """Add one memory fact, mark core-related facts as higher priority."""
    related_users = [str(uid).strip() for uid in (related_users or []) if str(uid).strip()]
    core_members = set(get_core_members(group_id))
    importance = 2 if any(uid in core_members for uid in related_users) else 0

    data = load()
    data["facts"].append(
        {
            "text": text,
            "related_users": related_users,
            "importance": importance,
            "created": time.strftime("%Y-%m-%d %H:%M"),
        }
    )

    if len(data["facts"]) > MAX_FACTS:
        data["facts"] = data["facts"][-MAX_FACTS:]
    save(data)


def _reorder_facts(facts: list[dict]) -> list[dict]:
    return sorted(
        facts,
        key=lambda f: (int(f.get("importance", 0)), str(f.get("created", ""))),
        reverse=True,
    )


def get_for(related_user: str, limit: int = 5) -> list[dict]:
    """Get related user memories, prioritized by importance then time."""
    data = load()
    uid = str(related_user)
    matches = [f for f in data["facts"] if uid in f.get("related_users", [])]
    return _reorder_facts(matches)[:limit]


def get_recent(limit: int = 10) -> list[dict]:
    data = load()
    return data["facts"][-limit:]


def build_context(user_id: str | None = None, mentioned_ids: list[str] | None = None) -> str:
    """
    Build a compact memory block for World Model.
    """
    seen: set[str] = set()
    facts: list[dict] = []

    if user_id:
        for f in get_for(user_id, limit=3):
            key = f["text"]
            if key not in seen:
                seen.add(key)
                facts.append(f)

    for mid in (mentioned_ids or []):
        for f in get_for(mid, limit=2):
            key = f["text"]
            if key not in seen:
                seen.add(key)
                facts.append(f)

    if not facts:
        return ""

    lines = ["[Memory]"]
    for f in facts[:8]:
        lines.append(f"  - {f['text']} ({f.get('created', '')})")
    return "\n".join(lines)
