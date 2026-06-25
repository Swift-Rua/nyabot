"""
群发言频率配置（后端可调）
每个群可设置独立的默认回复概率。
"""
import asyncio
import json
import os

from typing import Any

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
POLICY_FILE = os.path.join(DATA_DIR, "group_reply_policy.json")

_LOCK = asyncio.Lock()
_DEFAULT_CONFIG: dict[str, Any] = {"groups": {}}


def _ensure_file():
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(POLICY_FILE):
        with open(POLICY_FILE, "w", encoding="utf-8") as f:
            json.dump(_DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
        return

    try:
        with open(POLICY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = dict(_DEFAULT_CONFIG)

    if not isinstance(data.get("groups"), dict):
        data["groups"] = {}

    with open(POLICY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _load_sync() -> dict[str, Any]:
    _ensure_file()
    try:
        with open(POLICY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return dict(_DEFAULT_CONFIG)
    if not isinstance(data.get("groups"), dict):
        data["groups"] = {}
    return data


def _clean_prob(v: Any) -> float | None:
    try:
        prob = float(v)
    except (TypeError, ValueError):
        return None
    if prob < 0:
        return 0.0
    if prob > 1:
        return 1.0
    return prob


def get_group_default_reply_prob(group_id: str, fallback: float = 0.2) -> float:
    data = _load_sync()
    group = data.get("groups", {}).get(str(group_id), {})
    prob = None
    if isinstance(group, dict):
        prob = _clean_prob(group.get("default_reply_prob"))
    return prob if prob is not None else fallback


async def set_group_default_reply_prob(group_id: str, reply_prob: float):
    reply_prob = _clean_prob(reply_prob)
    if reply_prob is None:
        return

    async with _LOCK:
        _ensure_file()
        with open(POLICY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("groups", {})
        group = data["groups"].setdefault(str(group_id), {})
        if not isinstance(group, dict):
            group = {}
            data["groups"][str(group_id)] = group
        group["default_reply_prob"] = reply_prob
        with open(POLICY_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
