"""Data store for nyabot user and group metadata.

All modules that read/write bot data should use this module.
"""

import asyncio
import json
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
DATA_FILE = os.path.join(DATA_DIR, "members.json")
GROUP_MEMBERS_FILE = os.path.join(DATA_DIR, "group_members.json")
CORE_MEMBERS_FILE = os.path.join(DATA_DIR, "core_members.json")

MAX_GROUP_MEMBERS_PER_GROUP = 500
MAX_CORE_MEMBERS_PER_GROUP = 200

_write_lock = asyncio.Lock()

DEFAULT_TAGS = {"core": [], "interest": [], "behavior": []}


def _load_json_file(path: str, fallback):
    if not os.path.exists(path):
        return fallback
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return fallback


def _save_json_file(path: str, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp_path = f"{path}.tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass


def _normalize_member_list(member_ids: object) -> list[str]:
    if not isinstance(member_ids, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for uid in member_ids:
        u = str(uid).strip()
        if not u or u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


def _trim_member_list(member_ids: list[str], limit: int) -> list[str]:
    if len(member_ids) <= limit:
        return member_ids
    return member_ids[-limit:]


def _ensure_core_members_file() -> dict[str, list[str]]:
    """Load core member list and normalize legacy formats."""
    raw = _load_json_file(CORE_MEMBERS_FILE, {"global": [], "groups": {}})
    if not isinstance(raw, dict):
        raw = {"global": [], "groups": {}}

    global_members = _normalize_member_list(raw.get("global", []))
    global_members = _trim_member_list(global_members, MAX_CORE_MEMBERS_PER_GROUP)

    groups = raw.get("groups", {})
    if not isinstance(groups, dict):
        groups = {}
    groups = {
        str(k): _trim_member_list(_normalize_member_list(v), MAX_CORE_MEMBERS_PER_GROUP)
        for k, v in groups.items()
        if isinstance(v, list)
    }

    cleaned = {"global": global_members, "groups": groups}
    if not os.path.exists(CORE_MEMBERS_FILE) or raw != cleaned:
        _save_json_file(CORE_MEMBERS_FILE, cleaned)
    return cleaned


def get_core_members(group_id: str | None = None) -> list[str]:
    """Return core members list, merged with optional group-level overrides."""
    data = _ensure_core_members_file()
    group_id = None if group_id is None else str(group_id)

    members: list[str] = []
    members.extend(data.get("global", []))
    if group_id:
        members.extend(data.get("groups", {}).get(group_id, []))
    return _normalize_member_list(members)


def is_core_member(user_id: str, group_id: str | None = None) -> bool:
    uid = str(user_id)
    return uid in set(get_core_members(group_id))


def _ensure_group_members_file(members_data: dict | None = None) -> dict[str, list[str]]:
    """Load group_members mapping and migrate legacy fields when needed."""
    data = _load_json_file(GROUP_MEMBERS_FILE, {})
    if not isinstance(data, dict):
        data = {}

    groups = data.get("groups")
    if not isinstance(groups, dict):
        groups = {}
    groups = {
        str(k): _trim_member_list(_normalize_member_list(v), MAX_GROUP_MEMBERS_PER_GROUP)
        for k, v in groups.items()
        if isinstance(v, list)
    }

    if members_data is None:
        members_data = _load_json_file(DATA_FILE, {})

    if isinstance(members_data, dict):
        legacy_groups = members_data.get("group_members")
        if isinstance(legacy_groups, dict):
            for gid, legacy_members in legacy_groups.items():
                merged = groups.get(str(gid), [])
                candidate = _normalize_member_list(legacy_members)
                if not merged:
                    merged = candidate
                else:
                    existing = set(merged)
                    for uid in candidate:
                        if uid not in existing:
                            merged.append(uid)
                            existing.add(uid)
                merged = _trim_member_list(merged, MAX_GROUP_MEMBERS_PER_GROUP)
                groups[str(gid)] = merged

            members_data = dict(members_data)
            members_data.pop("group_members", None)
            _save_json_file(DATA_FILE, members_data)

    if data.get("groups") != groups or not os.path.exists(GROUP_MEMBERS_FILE):
        _save_json_file(GROUP_MEMBERS_FILE, {"groups": groups})

    return groups


def _load_group_members_sync() -> dict[str, list[str]]:
    return _ensure_group_members_file()


def _write_data_file(data: dict):
    _save_json_file(DATA_FILE, data)


def _clean_aliases(aliases: object) -> list[str]:
    if not isinstance(aliases, list):
        return []

    out: list[str] = []
    seen: set[str] = set()
    for alias in aliases:
        if not isinstance(alias, str):
            continue
        alias = alias.strip()
        if not alias:
            continue
        if len(alias) < 2:
            continue
        if len(alias) == 1 and alias.isdigit():
            continue
        if alias in seen:
            continue
        seen.add(alias)
        out.append(alias)
    return out


def _with_clean_aliases(profile: dict | None) -> dict | None:
    if not isinstance(profile, dict):
        return profile
    profile = dict(profile)
    profile["aliases"] = _clean_aliases(profile.get("aliases", []))
    return profile


def _ensure_file():
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(DATA_FILE):
        _save_json_file(DATA_FILE, {"users": {}, "group_meta": {}})
        return

    data = _load_json_file(DATA_FILE, {})
    if not isinstance(data, dict):
        data = {"users": {}, "group_meta": {}}

    data.setdefault("users", {})
    if not isinstance(data["users"], dict):
        data["users"] = {}

    if "group_meta" not in data or not isinstance(data["group_meta"], dict):
        data["group_meta"] = {}

    _save_json_file(DATA_FILE, data)
    _ensure_core_members_file()
    _ensure_group_members_file(data)


def _deep_merge(target: dict, source: dict):
    for k, v in source.items():
        if isinstance(v, dict) and isinstance(target.get(k), dict):
            _deep_merge(target[k], v)
        elif isinstance(v, list) and isinstance(target.get(k), list):
            if k == "aliases":
                target[k] = _clean_aliases(v)
            else:
                for item in v:
                    if item not in target[k]:
                        target[k].append(item)
        else:
            target[k] = v


def _new_user(user_id: str) -> dict:
    return {
        "name": f"User{user_id}",
        "tags": {"core": [], "interest": [], "behavior": []},
        "style": "A normal user style",
        "meta": {},
        "relations": {},
        "affinity": {},
    }


def load_sync() -> dict:
    _ensure_file()
    data = _load_json_file(DATA_FILE, {"users": {}, "group_meta": {}})

    if not isinstance(data, dict):
        data = {"users": {}, "group_meta": {}}

    data.setdefault("users", {})
    if not isinstance(data["users"], dict):
        data["users"] = {}
    data.setdefault("group_meta", {})
    if not isinstance(data["group_meta"], dict):
        data["group_meta"] = {}

    return data


def get_users_sync() -> dict:
    users = load_sync().get("users", {})
    if not isinstance(users, dict):
        return {}
    cleaned: dict[str, dict] = {}
    for uid, profile in users.items():
        if not isinstance(profile, dict):
            continue
        cleaned[str(uid)] = _with_clean_aliases(profile)  # type: ignore[arg-type]
    return cleaned


def get_user_sync(user_id: str) -> dict | None:
    return _with_clean_aliases(load_sync().get("users", {}).get(str(user_id)))


def get_group_members(group_id: str, max_members: int | None = None) -> list[str]:
    """Get group member ids, optionally limited for safety."""
    group_id = str(group_id)
    members = _load_group_members_sync().get(group_id, [])
    if not isinstance(members, list):
        return []
    members = _normalize_member_list(members)
    if isinstance(max_members, int) and max_members > 0:
        return members[:max_members]
    return members


def get_group_name(group_id: str) -> str:
    group_id = str(group_id)
    meta = load_sync().get("group_meta", {}).get(group_id, {})
    if isinstance(meta, dict):
        return str(meta.get("name", "")).strip()
    return ""


async def ensure_group_meta(group_id: str, group_name: str | None):
    group_id = str(group_id)
    if not group_name:
        return

    group_name = str(group_name).strip()
    if not group_name:
        return

    async with _write_lock:
        _ensure_file()
        data = _load_json_file(DATA_FILE, {"users": {}, "group_meta": {}})
        data.setdefault("group_meta", {})
        meta = data["group_meta"].setdefault(group_id, {})
        if not isinstance(meta, dict):
            meta = {}

        meta["name"] = group_name
        data["group_meta"][group_id] = meta

        _write_data_file(data)


async def load() -> dict:
    async with _write_lock:
        return load_sync()


async def save(data: dict):
    async with _write_lock:
        _write_data_file(data)


async def ensure_user(user_id: str) -> dict:
    user_id = str(user_id)
    async with _write_lock:
        _ensure_file()
        data = _load_json_file(DATA_FILE, {"users": {}, "group_meta": {}})

        data.setdefault("users", {})
        if user_id not in data["users"]:
            data["users"][user_id] = _new_user(user_id)
            _write_data_file(data)

        return _with_clean_aliases(data["users"][user_id])  # type: ignore[arg-type]


async def update_user(user_id: str, patch: dict):
    user_id = str(user_id)
    async with _write_lock:
        _ensure_file()
        data = _load_json_file(DATA_FILE, {"users": {}, "group_meta": {}})

        data.setdefault("users", {})
        user = data["users"].setdefault(user_id, _new_user(user_id))
        _deep_merge(user, patch)

        _write_data_file(data)


async def ensure_member_in_group(group_id: str, user_id: str):
    group_id = str(group_id)
    user_id = str(user_id)
    async with _write_lock:
        members_by_group = _load_group_members_sync()
        member_list = members_by_group.setdefault(group_id, [])
        if not isinstance(member_list, list):
            member_list = []
        member_list = _normalize_member_list(member_list)

        if user_id in member_list:
            return

        member_list.append(user_id)
        member_list = _trim_member_list(member_list, MAX_GROUP_MEMBERS_PER_GROUP)
        members_by_group[group_id] = member_list
        _save_json_file(GROUP_MEMBERS_FILE, {"groups": members_by_group})


async def update_tags(user_id: str, tags: dict):
    user_id = str(user_id)
    async with _write_lock:
        _ensure_file()
        data = _load_json_file(DATA_FILE, {"users": {}, "group_meta": {}})

        data.setdefault("users", {})
        user = data["users"].setdefault(user_id, _new_user(user_id))

        for k, v in tags.items():
            if k not in user["tags"]:
                user["tags"][k] = []
            for item in v:
                if item not in user["tags"][k]:
                    user["tags"][k].append(item)

        _write_data_file(data)


async def update_affinity(user_a: str, user_b: str, delta: int):
    user_a, user_b = str(user_a), str(user_b)
    async with _write_lock:
        _ensure_file()
        data = _load_json_file(DATA_FILE, {"users": {}, "group_meta": {}})

        if user_a in data.get("users", {}):
            rels = data["users"][user_a].setdefault("relations", {})
            rel = rels.setdefault(
                user_b,
                {
                    "type": "acquaintance",
                    "affinity": 50,
                    "interaction": 0,
                    "last_chat": "",
                },
            )
            rel["affinity"] = rel.get("affinity", 50) + delta

            _write_data_file(data)


async def record_interaction(speaker_id: str, target_ids: list[str]):
    speaker_id = str(speaker_id)
    if not target_ids:
        return

    async with _write_lock:
        _ensure_file()
        data = _load_json_file(DATA_FILE, {"users": {}, "group_meta": {}})

        if speaker_id not in data.get("users", {}):
            return

        import time

        now = time.strftime("%m-%d %H:%M")

        for tid in target_ids:
            tid = str(tid)
            if tid == speaker_id:
                continue
            rels = data["users"][speaker_id].setdefault("relations", {})
            rel = rels.setdefault(
                tid,
                {
                    "type": "acquaintance",
                    "affinity": 50,
                    "interaction": 0,
                    "last_chat": "",
                },
            )
            rel["interaction"] = rel.get("interaction", 0) + 1
            rel["last_chat"] = now

        _write_data_file(data)


def get_affinity(user_a: str, user_b: str) -> int:
    u = get_user_sync(str(user_a))
    if not u:
        return 0
    return u.get("relations", {}).get(str(user_b), {}).get("affinity", 50)
