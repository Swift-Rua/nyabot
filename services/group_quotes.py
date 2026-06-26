"""Store and sample group message snippets for offline fallback replies."""

import asyncio
import json
import os
import random
import re
import time

from services.utils import clean_text

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QUOTE_FILE = os.path.join(BASE_DIR, "data", "group_quotes.json")
MAX_QUOTES_PER_GROUP = 5000

_LOCK = asyncio.Lock()
_AT_TOKEN_RE = re.compile(r"\[CQ:at,qq=\d+\]")
_NAME_PREFIX_RE_TEMPLATE = r"^\s*{name}\s*[:：]\s*"


def _normalize_quote_text(text: str, speaker_name: str | None = None) -> str:
    text = clean_text(text or "")
    if not text:
        return ""

    text = _AT_TOKEN_RE.sub("", text)
    text = re.sub(r"@[^\s,。！？、~]*", "", text)
    if speaker_name:
        name_pattern = _NAME_PREFIX_RE_TEMPLATE.format(name=re.escape(str(speaker_name).strip()))
        text = re.sub(name_pattern, "", text)
        text = re.sub(rf"^\s*{re.escape(str(speaker_name).strip())}\b\s*", "", text)

    text = text.strip(" .,:;!?~[]()")
    return text.strip()

def _load() -> dict[str, dict[str, list[dict]]]:
    if not os.path.exists(QUOTE_FILE):
        return {"groups": {}}
    try:
        with open(QUOTE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                return {"groups": {}}
            return data
    except Exception:
        return {"groups": {}}


def _save(data: dict):
    os.makedirs(os.path.dirname(QUOTE_FILE), exist_ok=True)
    with open(QUOTE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _trim(items: list[dict]) -> list[dict]:
    if len(items) < MAX_QUOTES_PER_GROUP:
        return items
    if not items:
        return items

    idx = random.randrange(len(items))
    return items[:idx] + items[idx + 1 :]


def _to_text(item: object) -> str:
    if not isinstance(item, dict):
        return ""
    return clean_text(item.get("text") or "")


def get_quotes(group_id: str | None = None, seed: str | None = None, limit: int = 6) -> list[str]:
    data = _load()
    groups = data.get("groups")
    if not isinstance(groups, dict):
        return []

    pool: list[dict] = []
    if group_id:
        raw = groups.get(str(group_id).strip())
        if isinstance(raw, list):
            pool.extend(raw)
    else:
        for raw in groups.values():
            if isinstance(raw, list):
                pool.extend(raw)

    if not pool:
        return []

    normalized = [q for q in pool if _to_text(q)]
    if not normalized:
        return []

    if seed:
        key = clean_text(seed).lower()
        if key:
            tokens = [tok for tok in key.split() if tok]
            if not tokens:
                tokens = [key]

            matched: list[dict] = []
            for quote in normalized:
                text = _to_text(quote).lower()
                if any(token in text for token in tokens):
                    matched.append(quote)
            if matched:
                normalized = matched

    if not normalized:
        return []

    random.shuffle(normalized)
    max_items = max(1, min(int(limit), len(normalized)))
    return [_to_text(item) for item in normalized[:max_items]]


async def add_quote(group_id: str, user_id: str, user_name: str, text: str):
    group_id = str(group_id).strip()
    user_name = str(user_name or f"User{user_id}").strip()
    text = _normalize_quote_text(text, speaker_name=user_name)
    if not group_id or not text:
        return
    user_id = str(user_id).strip()

    async with _LOCK:
        data = _load()
        groups = data.get("groups")
        if not isinstance(groups, dict):
            groups = {}

        quotes = groups.get(group_id)
        if not isinstance(quotes, list):
            quotes = []

        quote = {
            "text": text,
            "user_id": user_id,
            "user_name": user_name,
            "created": time.strftime("%Y-%m-%d %H:%M"),
        }

        if not quotes or quotes[-1].get("text") != text or quotes[-1].get("user_id") != user_id:
            quotes.append(quote)
            quotes = _trim(quotes)
            groups[group_id] = quotes
            data["groups"] = groups
            _save(data)


async def get_random_quote(group_id: str | None = None) -> str | None:
    candidates = get_quotes(group_id=group_id, limit=1)
    if not candidates:
        return None
    return candidates[0]
