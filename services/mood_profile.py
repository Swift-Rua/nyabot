"""Behavioral mood tracking used by no-token fallback modules."""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import time


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATISTICS_DB = os.path.join(BASE_DIR, "data", "statistics.db")

_LOCK = threading.Lock()

DEFAULT_STATE = {
    "happy": 56,
    "tired": 26,
    "social": 58,
    "roast": 42,
    "energy": 68,
    "volatility": 0,
    "message_count": 0,
    "mention_count": 0,
    "image_count": 0,
    "face_count": 0,
    "total_length": 0,
    "active_minutes": 0.0,
    "activity_window": 0,
    "last_seen": 0.0,
}

_POS_WORDS = (
    "哈哈",
    "好",
    "棒",
    "厉害",
    "牛",
    "爱",
    "喜欢",
    "谢谢",
    "感谢",
    "天才",
    "可爱",
)
_NEG_WORDS = (
    "烦",
    "讨厌",
    "垃圾",
    "无语",
    "烂",
    "差",
    "丢人",
    "烦人",
    "傻",
)
_ROAST_WORDS = ("笑死", "呵呵", "白痴", "傻逼", "沙雕", "嘴臭")

_BASELINE = {
    "happy": 56,
    "tired": 26,
    "social": 58,
    "roast": 42,
    "energy": 68,
    "volatility": 0,
}

_CLAMP_MIN = 0
_CLAMP_MAX = 100
_KEEP_DAYS = 14


def _clamp(value: int) -> int:
    return max(_CLAMP_MIN, min(_CLAMP_MAX, value))


def _to_text(value: object) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return text


def _normalize_group_id(group_id: object) -> str:
    return str(group_id).strip() if str(group_id).strip() else ""


def _normalize_user_id(user_id: object) -> str:
    return str(user_id).strip() if str(user_id).strip() else ""


def _normalize_message(text: object) -> str:
    if text is None:
        return ""
    return str(text).strip()


def _connect():
    conn = sqlite3.connect(
        STATISTICS_DB,
        timeout=10,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_db():
    os.makedirs(os.path.dirname(STATISTICS_DB), exist_ok=True)
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS mood_profiles (
                scope TEXT NOT NULL,
                group_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                happy INTEGER NOT NULL DEFAULT 56,
                tired INTEGER NOT NULL DEFAULT 26,
                social INTEGER NOT NULL DEFAULT 58,
                roast INTEGER NOT NULL DEFAULT 42,
                energy INTEGER NOT NULL DEFAULT 68,
                volatility INTEGER NOT NULL DEFAULT 0,
                message_count INTEGER NOT NULL DEFAULT 0,
                mention_count INTEGER NOT NULL DEFAULT 0,
                image_count INTEGER NOT NULL DEFAULT 0,
                face_count INTEGER NOT NULL DEFAULT 0,
                total_length INTEGER NOT NULL DEFAULT 0,
                active_minutes REAL NOT NULL DEFAULT 0,
                activity_window INTEGER NOT NULL DEFAULT 0,
                last_seen REAL NOT NULL DEFAULT 0,
                created_at REAL NOT NULL DEFAULT (strftime('%s','now')),
                PRIMARY KEY (scope, group_id, user_id)
            )
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_mood_profiles_scope_group
            ON mood_profiles(scope, group_id, user_id)
            """
        )
        conn.commit()
    finally:
        conn.close()


def _extract_signal(
    text: str,
    has_image: bool,
    has_face: bool,
    mentioned_count: int,
) -> tuple[int, int, int, int]:
    msg = _normalize_message(text).lower()
    pos = sum(1 for token in _POS_WORDS if token in msg)
    neg = sum(1 for token in _NEG_WORDS if token in msg)
    roast = sum(1 for token in _ROAST_WORDS if token in msg)

    dhappy = max(-3, min(3, pos - neg))
    dsocial = max(-2, min(4, mentioned_count))
    droast = max(-2, min(3, roast))
    dtired = -1 if has_face else 0
    denergy = 1 + (1 if has_image else 0) + (1 if has_face else 0)
    if re.search(r"晚|累|困|睡", msg):
        dtired += 2
        denergy -= 1
    if len(msg) <= 2:
        denergy -= 1

    return dhappy, dsocial, droast, denergy, dtired


def _label_mood(score: int, low: int, high: int, low_desc: str, mid_desc: str, high_desc: str) -> str:
    if score >= high:
        return high_desc
    if score <= low:
        return low_desc
    return mid_desc


def _activity_level(value: int) -> str:
    if value >= 80:
        return "high"
    if value >= 50:
        return "medium"
    return "low"


def _activity_text(value: int) -> str:
    if value >= 80:
        return "热闹"
    if value >= 55:
        return "活跃"
    if value >= 35:
        return "一般"
    return "低速"


def _load_profile(cur: sqlite3.Cursor, scope: str, group_id: str, user_id: str) -> dict:
    cur.execute(
        """
        SELECT happy, tired, social, roast, energy, volatility, message_count, mention_count,
               image_count, face_count, total_length, active_minutes, activity_window, last_seen
        FROM mood_profiles
        WHERE scope = ? AND group_id = ? AND user_id = ?
        """,
        (scope, group_id, user_id),
    )
    row = cur.fetchone()
    if not row:
        return dict(DEFAULT_STATE)

    return {
        "happy": int(row["happy"] or 0),
        "tired": int(row["tired"] or 0),
        "social": int(row["social"] or 0),
        "roast": int(row["roast"] or 0),
        "energy": int(row["energy"] or 0),
        "volatility": int(row["volatility"] or 0),
        "message_count": int(row["message_count"] or 0),
        "mention_count": int(row["mention_count"] or 0),
        "image_count": int(row["image_count"] or 0),
        "face_count": int(row["face_count"] or 0),
        "total_length": int(row["total_length"] or 0),
        "active_minutes": float(row["active_minutes"] or 0.0),
        "activity_window": int(row["activity_window"] or 0),
        "last_seen": float(row["last_seen"] or 0.0),
    }


def _upsert_profile(
    cur: sqlite3.Cursor,
    scope: str,
    group_id: str,
    user_id: str,
    profile: dict,
) -> None:
    cur.execute(
        """
        INSERT INTO mood_profiles (
            scope,
            group_id,
            user_id,
            happy,
            tired,
            social,
            roast,
            energy,
            volatility,
            message_count,
            mention_count,
            image_count,
            face_count,
            total_length,
            active_minutes,
            activity_window,
            last_seen
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(scope, group_id, user_id) DO UPDATE SET
            happy = excluded.happy,
            tired = excluded.tired,
            social = excluded.social,
            roast = excluded.roast,
            energy = excluded.energy,
            volatility = excluded.volatility,
            message_count = excluded.message_count,
            mention_count = excluded.mention_count,
            image_count = excluded.image_count,
            face_count = excluded.face_count,
            total_length = excluded.total_length,
            active_minutes = excluded.active_minutes,
            activity_window = excluded.activity_window,
            last_seen = excluded.last_seen
        """,
        (
            scope,
            group_id,
            user_id,
            int(profile["happy"]),
            int(profile["tired"]),
            int(profile["social"]),
            int(profile["roast"]),
            int(profile["energy"]),
            int(profile["volatility"]),
            int(profile["message_count"]),
            int(profile["mention_count"]),
            int(profile["image_count"]),
            int(profile["face_count"]),
            int(profile["total_length"]),
            float(profile["active_minutes"]),
            int(profile["activity_window"]),
            float(profile["last_seen"]),
        ),
    )


def _record_single(
    cur: sqlite3.Cursor,
    scope: str,
    group_id: str,
    user_id: str,
    text: str,
    mentions: int,
    has_image: bool,
    has_face: bool,
    now: float,
) -> dict:
    current = _load_profile(cur, scope, group_id, user_id)
    dhappy, dsocial, droast, denergy, dtired = _extract_signal(
        text,
        has_image=has_image,
        has_face=has_face,
        mentioned_count=mentions,
    )

    last_seen = float(current.get("last_seen", 0.0))
    active_gap = max(0.0, now - last_seen) if last_seen else 0.0
    active_add = 0.0
    if active_gap > 0:
        if active_gap <= 300:
            active_add = min(5.0, active_gap / 60.0)
        elif active_gap > 1800:
            # long silent period, reset burst state
            current["activity_window"] = max(0, current.get("activity_window", 0) - 20)

    current["happy"] = _clamp(current.get("happy", 56) + dhappy)
    current["tired"] = _clamp(current.get("tired", 26) + dtired)
    current["social"] = _clamp(current.get("social", 58) + dsocial)
    current["roast"] = _clamp(current.get("roast", 42) + droast)
    current["energy"] = _clamp(current.get("energy", 68) + denergy)
    current["volatility"] = _clamp(int(current.get("volatility", 0) * 0.75 + abs(dhappy + dsocial + droast)))

    current["message_count"] = current.get("message_count", 0) + 1
    current["mention_count"] = current.get("mention_count", 0) + max(0, mentions)
    if has_image:
        current["image_count"] = current.get("image_count", 0) + 1
    if has_face:
        current["face_count"] = current.get("face_count", 0) + 1
    current["total_length"] = current.get("total_length", 0) + len(_normalize_message(text))

    window = max(0, current.get("activity_window", 0))
    window = min(100, window + 6 + min(mentions, 6))
    current["activity_window"] = min(100, window)
    current["active_minutes"] = current.get("active_minutes", 0.0) + active_add
    current["last_seen"] = now

    _upsert_profile(cur, scope, group_id, user_id, current)
    return current


def record_message(
    group_id: str,
    user_id: str,
    text: str,
    *,
    mentioned_count: int = 0,
    has_image: bool = False,
    has_face: bool = False,
) -> None:
    gid = _normalize_group_id(group_id)
    uid = _normalize_user_id(user_id)
    if not gid or not uid:
        return

    message = _normalize_message(text)
    _ensure_db()
    now = time.time()

    mentions = max(0, int(mentioned_count))
    with _LOCK:
        conn = _connect()
        try:
            cur = conn.cursor()
            _record_single(cur, "group", gid, "", message, mentions, has_image, has_face, now)
            _record_single(cur, "user", gid, uid, message, mentions, has_image, has_face, now)
            conn.commit()
        finally:
            conn.close()


def _fetch(scope: str, group_id: str, user_id: str) -> dict:
    gid = _normalize_group_id(group_id)
    uid = _normalize_user_id(user_id) if scope == "user" else ""
    if not gid or (scope == "user" and not uid):
        return dict(DEFAULT_STATE)

    _ensure_db()
    conn = _connect()
    try:
        cur = conn.cursor()
        row = cur.execute(
            """
            SELECT happy, tired, social, roast, energy, volatility, message_count, mention_count,
                   image_count, face_count, total_length, active_minutes, activity_window, last_seen
            FROM mood_profiles
            WHERE scope = ? AND group_id = ? AND user_id = ?
            """,
            (scope, gid, uid),
        ).fetchone()

        if not row:
            return dict(DEFAULT_STATE)
        return {
            "happy": int(row["happy"] or 0),
            "tired": int(row["tired"] or 0),
            "social": int(row["social"] or 0),
            "roast": int(row["roast"] or 0),
            "energy": int(row["energy"] or 0),
            "volatility": int(row["volatility"] or 0),
            "message_count": int(row["message_count"] or 0),
            "mention_count": int(row["mention_count"] or 0),
            "image_count": int(row["image_count"] or 0),
            "face_count": int(row["face_count"] or 0),
            "total_length": int(row["total_length"] or 0),
            "active_minutes": float(row["active_minutes"] or 0.0),
            "activity_window": int(row["activity_window"] or 0),
            "last_seen": float(row["last_seen"] or 0.0),
        }
    finally:
        conn.close()


def get_group_profile(group_id: str) -> dict:
    return _fetch("group", group_id, "")


def get_user_profile(group_id: str, user_id: str) -> dict:
    return _fetch("user", group_id, user_id)


def get_context(group_id: str, user_id: str) -> dict[str, str]:
    group_profile = get_group_profile(group_id)
    user_profile = get_user_profile(group_id, user_id)

    g_happy = _label_mood(
        group_profile.get("happy", 56),
        32,
        72,
        "低沉",
        "普通",
        "开朗",
    )
    g_mood_desc = f"{g_happy}，{_activity_text(group_profile.get('activity_window', 0))}"

    u_happy = _label_mood(
        user_profile.get("happy", 56),
        32,
        72,
        "低沉",
        "普通",
        "开朗",
    )
    u_tired = "疲惫" if user_profile.get("tired", 26) >= 65 else "清醒"

    return {
        "group_mood_label": g_happy,
        "group_mood_summary": g_mood_desc,
        "group_activity": _activity_text(group_profile.get("activity_window", 0)),
        "group_activity_level": _activity_level(group_profile.get("activity_window", 0)),
        "group_mood": _to_text(group_profile.get("happy", 0)),
        "group_social": _to_text(group_profile.get("social", 0)),
        "group_roast": _to_text(group_profile.get("roast", 0)),
        "group_tired": _to_text(group_profile.get("tired", 0)),
        "group_energy": _to_text(group_profile.get("energy", 0)),
        "group_volatility": _to_text(group_profile.get("volatility", 0)),
        "group_messages": _to_text(group_profile.get("message_count", 0)),
        "user_mood_label": u_happy,
        "user_activity": _activity_text(user_profile.get("activity_window", 0)),
        "user_state": f"{u_happy}且{u_tired}",
        "user_mood": _to_text(user_profile.get("happy", 0)),
        "user_social": _to_text(user_profile.get("social", 0)),
        "user_energy": _to_text(user_profile.get("energy", 0)),
        "user_volatility": _to_text(user_profile.get("volatility", 0)),
        "user_messages": _to_text(user_profile.get("message_count", 0)),
    }


def decay() -> None:
    """Periodic fallback profile decay, preventing scores from locking at extremes."""
    _ensure_db()
    cutoff = time.time() - 60 * 60 * 24 * 14
    with _LOCK:
        conn = _connect()
        try:
            cur = conn.cursor()
            # Remove dormant very old rows to keep behavior fresh.
            cur.execute(
                "DELETE FROM mood_profiles WHERE last_seen > 0 AND last_seen < ?",
                (cutoff,),
            )
            rows = cur.execute("SELECT rowid, happy, tired, social, roast, energy, volatility FROM mood_profiles").fetchall()
            for row in rows:
                rowid = int(row["rowid"])
                happy = row["happy"]
                tired = row["tired"]
                social = row["social"]
                roast = row["roast"]
                energy = row["energy"]
                volatility = row["volatility"]

                happy = _clamp(int(round((happy * 0.85) + (_BASELINE["happy"] * 0.15))))
                tired = _clamp(int(round((tired * 0.85) + (_BASELINE["tired"] * 0.15))))
                social = _clamp(int(round((social * 0.9) + (_BASELINE["social"] * 0.1))))
                roast = _clamp(int(round((roast * 0.9) + (_BASELINE["roast"] * 0.1))))
                energy = _clamp(int(min(_CLAMP_MAX, energy + 2)))
                volatility = max(0, int(volatility * 0.7))

                cur.execute(
                    """
                    UPDATE mood_profiles
                    SET happy = ?, tired = ?, social = ?, roast = ?, energy = ?,
                        volatility = CASE WHEN volatility > ? THEN volatility - 1 ELSE 0 END,
                        activity_window = CASE WHEN activity_window > 0 THEN activity_window - 2 ELSE 0 END
                    WHERE rowid = ?
                    """,
                    (happy, tired, social, roast, energy, 0, rowid),
                )
            conn.commit()
        finally:
            conn.close()

