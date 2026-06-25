"""Helpers for extracting @ mentions from OneBot messages."""

import re
from services.data_store import get_users_sync, get_user_sync


_CQ_AT_RE = re.compile(r"\[CQ:at,qq=(\d+)\]")
_BOT_NAME_HINTS = ("牛牛喵", "牛奶喵", "牛牛", "喵")


def _normalize_text(value: object) -> str:
    return "" if value is None else str(value)


def _append_unique(target: list[str], qq: str):
    if qq and qq not in target:
        target.append(qq)


def _extract_cq_at_ids(raw_message: str) -> list[str]:
    if not raw_message:
        return []
    return [qq for qq in _CQ_AT_RE.findall(raw_message) if qq]


def _compact_plaintext(text: str) -> str:
    if not text:
        return ""
    compact = text.replace("＠", "@")
    return re.sub(r"\s+", "", compact)


def parse_at_from_event(event) -> tuple[list[str], str]:
    """
    Parse OneBot event to extract mentioned qq ids and printable text.

    Returns:
        (mentioned qq id list, display text)
    """
    at_ids: list[str] = []
    text_parts: list[str] = []
    self_id = str(event.self_id)
    raw_message = _normalize_text(getattr(event, "raw_message", ""))

    for seg in event.get_message():
        if seg.type == "at":
            qq = str(seg.data.get("qq", ""))
            if qq and qq != "all":
                _append_unique(at_ids, qq)
                if qq == self_id:
                    text_parts.append("@牛牛喵")
                else:
                    user = get_user_sync(qq)
                    name = user["name"] if user else f"用户{qq}"
                    text_parts.append(f"@{name}")
            else:
                text_parts.append("@全体成员")
        elif seg.type == "text":
            text_parts.append(seg.data.get("text", ""))
        else:
            pass

    for qq in _extract_cq_at_ids(raw_message):
        _append_unique(at_ids, qq)

    display_text = "".join(text_parts).strip()
    if not display_text:
        display_text = _normalize_text(event.get_plaintext()).strip()

    compact = _compact_plaintext(display_text or _normalize_text(event.get_plaintext()))
    if self_id not in at_ids:
        for alias in _BOT_NAME_HINTS:
            if f"@{alias}" in compact:
                _append_unique(at_ids, self_id)
                break

    return at_ids, display_text


def resolve_mentions(text: str) -> list[str]:
    """
    Resolve plaintext mentions to known user IDs by @name / alias matching.
    """
    users = get_users_sync()
    if not users:
        return []

    found_ids: list[str] = []
    clean_text = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9@]", "", text)

    for uid, profile in users.items():
        name = profile.get("name", "")
        if not name:
            continue

        if f"@{name}" in text:
            found_ids.append(uid)
            continue

        aliases = profile.get("aliases", [])
        alias_matched = False
        for alias in aliases:
            if len(alias) >= 2 and (
                f"@{alias}" in text or alias in clean_text
            ):
                found_ids.append(uid)
                alias_matched = True
                break
        if alias_matched:
            continue

        candidates: list[str] = []
        clean_name = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", name)
        if len(clean_name) >= 2:
            candidates.append(clean_name)

        for part in re.split(r"[（）()]", name):
            part = part.strip()
            if len(part) >= 2 and part != clean_name:
                candidates.append(part)

        matched = False
        for candidate in candidates:
            if len(candidate) >= 2 and candidate in clean_text:
                found_ids.append(uid)
                matched = True
                break
        if matched:
            continue

        if clean_name == "牛牛喵":
            if re.search(r"(?:^|[^\u4e00-\u9fff])牛牛喵(?:$|[^\u4e00-\u9fff])", text):
                found_ids.append(uid)

    return list(set(found_ids))
