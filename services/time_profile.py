"""Group time-profile statistics for no-token behavior."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta

from services.message_logger import get_top_words

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATISTICS_DB = os.path.join(BASE_DIR, "data", "statistics.db")

_LOCK = threading.Lock()

_KEEP_DAYS = 14


def _connect():
    conn = sqlite3.connect(
        STATISTICS_DB,
        timeout=10,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    return conn


def _normalize_group_id(group_id: object) -> str:
    return str(group_id).strip() if str(group_id).strip() else ""


def _normalize_user_id(user_id: object) -> str:
    return str(user_id).strip() if str(user_id).strip() else ""


def _day_key(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def _hour_now(ts: float) -> int:
    return int(time.localtime(ts).tm_hour)


def _ensure_db():
    os.makedirs(os.path.dirname(STATISTICS_DB), exist_ok=True)
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS group_hour_profiles (
                group_id TEXT NOT NULL,
                day_key TEXT NOT NULL,
                hour INTEGER NOT NULL,
                message_count INTEGER NOT NULL DEFAULT 0,
                members TEXT NOT NULL DEFAULT '[]',
                last_seen REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (group_id, day_key, hour)
            )
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_group_hour_profiles_day
            ON group_hour_profiles(day_key, group_id)
            """
        )
        conn.commit()
    finally:
        conn.close()


def _cleanup(conn: sqlite3.Connection, now_ts: float | None = None) -> None:
    now_ts = float(now_ts or time.time())
    cutoff = (datetime.fromtimestamp(now_ts) - timedelta(days=_KEEP_DAYS)).strftime("%Y-%m-%d")
    conn.execute("DELETE FROM group_hour_profiles WHERE day_key < ?", (cutoff,))


def _parse_members(raw: str) -> set[str]:
    try:
        data = json.loads(raw or "[]")
        return {str(i) for i in data if str(i).strip()}
    except Exception:
        return set()


def _dump_members(members: set[str]) -> str:
    # keep member cache short and deterministic.
    return json.dumps(sorted(list(members))[:100], ensure_ascii=False)


def _to_int(v: object) -> int:
    try:
        return int(v)
    except Exception:
        return 0


def _to_float(v: object) -> float:
    try:
        return float(v)
    except Exception:
        return 0.0


def record_message(group_id: str, user_id: str | None = None, *, when: float | None = None) -> None:
    gid = _normalize_group_id(group_id)
    if not gid:
        return

    uid = _normalize_user_id(user_id)
    ts = float(when if when is not None else time.time())
    day_key = _day_key(ts)
    hour = _hour_now(ts)

    _ensure_db()
    with _LOCK:
        conn = _connect()
        try:
            _cleanup(conn, ts)
            cur = conn.cursor()
            row = cur.execute(
                """
                SELECT message_count, members, last_seen
                FROM group_hour_profiles
                WHERE group_id = ? AND day_key = ? AND hour = ?
                """,
                (gid, day_key, hour),
            ).fetchone()

            members = _parse_members(row["members"]) if row else set()
            if uid:
                members.add(uid)

            if row is None:
                cur.execute(
                    """
                    INSERT INTO group_hour_profiles (
                        group_id, day_key, hour, message_count, members, last_seen
                    ) VALUES (?, ?, ?, 1, ?, ?)
                    """,
                    (gid, day_key, hour, _dump_members(members), ts),
                )
            else:
                cur.execute(
                    """
                    UPDATE group_hour_profiles
                    SET message_count = message_count + 1,
                        members = ?,
                        last_seen = ?
                    WHERE group_id = ? AND day_key = ? AND hour = ?
                    """,
                    (_dump_members(members), ts, gid, day_key, hour),
                )
            conn.commit()
        finally:
            conn.close()


def get_time_profile(group_id: str, *, window_days: int = 7) -> dict:
    gid = _normalize_group_id(group_id)
    if not gid:
        return {}

    _ensure_db()
    now = time.time()
    start = (datetime.fromtimestamp(now) - timedelta(days=max(1, min(30, int(window_days))))) .strftime("%Y-%m-%d")

    conn = _connect()
    try:
        _cleanup(conn, now)
        rows = conn.execute(
            """
            SELECT day_key, hour, message_count, members
            FROM group_hour_profiles
            WHERE group_id = ? AND day_key >= ?
            ORDER BY day_key ASC, hour ASC
            """,
            (gid, start),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return {
            "group_id": gid,
            "window_days": max(1, int(window_days)),
            "total_messages": 0,
            "active_days": 0,
            "active_hours": 0,
            "activity_level": "low",
            "peak_hour": 22,
            "peak_count": 0,
            "active_members": 0,
        }

    hourly = [0] * 24
    active_days: set[str] = set()
    members: set[str] = set()
    total_messages = 0

    for r in rows:
        hour = int(r["hour"] or 0)
        cnt = int(r["message_count"] or 0)
        hourly[hour] += cnt
        total_messages += cnt
        active_days.add(str(r["day_key"]))
        members.update(_parse_members(r["members"]))

    max_hour = max(range(24), key=lambda h: hourly[h], default=0)
    peak_count = hourly[max_hour]
    avg_per_hour = total_messages / max(1, len(active_days) * 24)

    if avg_per_hour >= 18:
        level = "hot"
    elif avg_per_hour >= 8:
        level = "warm"
    elif avg_per_hour >= 3:
        level = "normal"
    else:
        level = "cold"

    return {
        "group_id": gid,
        "window_days": max(1, min(30, int(window_days))),
        "total_messages": total_messages,
        "active_days": len(active_days),
        "active_hours": sum(1 for c in hourly if c > 0),
        "activity_level": level,
        "peak_hour": max_hour,
        "peak_count": peak_count,
        "active_members": len(members),
        "current_hour": _hour_now(now),
        "hourly": hourly,
    }


def get_context(group_id: str, *, window_days: int = 7) -> dict[str, str]:
    profile = get_time_profile(group_id, window_days=window_days)
    if not profile:
        return {}

    total = int(profile.get("total_messages", 0))
    if total <= 0:
        return {
            "time_hour": str(_hour_now(time.time())),
            "time_active_level": "cold",
            "time_peak_hour": "22",
            "time_active_members": "0",
            "time_active_days": "0",
            "time_summary": "群聊近期消息较少，时序画像不足",
            "time_remark": "",
        }

    activity = str(profile.get("activity_level", "cold"))
    peak = int(profile.get("peak_hour", 22))
    members = int(profile.get("active_members", 0))
    active_days = int(profile.get("active_days", 0))
    hour = _to_int(profile.get("current_hour"))
    level_cn = {
        "hot": "火爆",
        "warm": "热络",
        "normal": "中等",
        "cold": "冷清",
    }.get(activity, "未知")

    # top words are helpful for context and reminder rules
    topics = get_top_words(group_id=group_id, limit=3)
    top_word = ""
    if topics:
        first = str(topics[0].get("token", "")).strip()
        if first:
            top_word = first

    remark = ""
    if hour >= 22 and total > 0:
        remark = "现在已经很晚了，尽量别太晚熬夜，早点休息。"
    elif hour <= 6 and total > 0:
        remark = "凌晨时间段，群里更偏安静聊天，注意身体。"
    elif 11 <= hour <= 13 and total > 0:
        remark = "中午时段，活跃话题更容易被快速接住。"
    elif 18 <= hour <= 22 and total > 0:
        remark = "晚间通常更容易接上梗，回复节奏可以慢一点。"

    return {
        "time_hour": str(hour),
        "time_active_level": activity,
        "time_peak_hour": str(peak),
        "time_active_members": str(members),
        "time_active_days": str(active_days),
        "time_level_text": level_cn,
        "time_top_word": top_word,
        "time_summary": f"最近{active_days}天内高峰在{peak}:00，活跃度{level_cn}。",
        "time_remark": remark,
    }

