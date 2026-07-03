"""Permission helpers for high-impact group commands."""

import os
import re


ADMIN_ENV_KEYS = ("BOT_ADMINS", "SUPERUSERS")
ADMIN_ROLES = {"owner", "admin"}
DENY_TEXT = "这条指令需要群主或管理员来用喵。"


def _configured_admin_ids() -> set[str]:
    ids: set[str] = set()
    for key in ADMIN_ENV_KEYS:
        raw = os.getenv(key, "")
        for part in raw.replace(";", ",").replace(" ", ",").split(","):
            uid = part.strip()
            if uid:
                ids.add(uid)
        ids.update(re.findall(r"\d+", raw))
    return ids


def is_group_admin(event) -> bool:
    user_id = str(getattr(event, "user_id", "")).strip()
    if user_id and user_id in _configured_admin_ids():
        return True

    sender = getattr(event, "sender", None)
    role = str(getattr(sender, "role", "") or "").strip().lower()
    return role in ADMIN_ROLES
