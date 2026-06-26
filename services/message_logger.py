"""SQLite based logger for group messages and lightweight member profiles."""

import json
import os
import re
import sqlite3
import threading
import time

from services.utils import clean_text

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HISTORY_DB = os.path.join(BASE_DIR, "data", "history.db")

MAX_MESSAGES_PER_GROUP = 5000
MAX_MESSAGES_PER_QUERY = 80
MAX_FACE_TRACK_PER_MESSAGE = 5
MAX_WORD_TRACK_PER_MESSAGE = 80
MAX_PROFILE_QUERY = 200
MAX_PROFILE_TOP = 200

_STOP_WORDS = {
    "the",
    "this",
    "that",
    "with",
    "then",
    "just",
    "really",
    "what",
    "你",
    "我",
    "他",
    "她",
    "它",
    "这",
    "那",
    "的",
    "了",
    "在",
    "有",
}

_WORD_RE = re.compile(r"[A-Za-z0-9\u4e00-\u9fff]{2,}")
_DB_LOCK = threading.Lock()


def _normalize_group_id(group_id: object) -> str:
    return str(group_id).strip() if str(group_id).strip() else ""


def _normalize_user_id(user_id: object) -> str:
    return str(user_id).strip() if str(user_id).strip() else ""


def _normalize_nickname(name: object) -> str:
    v = clean_text(str(name)) if name is not None else ""
    return v or "User"


def _normalize_text(text: object) -> str:
    return clean_text(str(text)) if text is not None else ""


def _normalize_at_list(at_list: object) -> list[str]:
    if not isinstance(at_list, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in at_list:
        uid = _normalize_user_id(item)
        if not uid or uid in seen:
            continue
        seen.add(uid)
        out.append(uid)
    return out


def _normalize_face_ids(face_ids: object) -> list[int]:
    if not face_ids:
        return []
    out: list[int] = []
    seen: set[int] = set()
    for item in face_ids:
        try:
            fid = int(item)
        except (TypeError, ValueError):
            continue
        if fid <= 0 or fid in seen:
            continue
        seen.add(fid)
        out.append(fid)
    return out[:MAX_FACE_TRACK_PER_MESSAGE]


def _extract_tokens(message: str) -> list[str]:
    if not message:
        return []
    raw = _WORD_RE.findall(str(message).strip().lower())
    out: list[str] = []
    seen: set[str] = set()
    for token in raw:
        token = token.strip()
        if len(token) < 2:
            continue
        if token in _STOP_WORDS:
            continue
        if token in seen:
            continue
        seen.add(token)
        out.append(token)
        if len(out) >= MAX_WORD_TRACK_PER_MESSAGE:
            break
    return out


def _ensure_db() -> None:
    os.makedirs(os.path.dirname(HISTORY_DB), exist_ok=True)
    conn = sqlite3.connect(HISTORY_DB)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id TEXT,
                group_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                user_name TEXT,
                ts REAL NOT NULL,
                message TEXT NOT NULL,
                reply_to TEXT,
                at_list TEXT,
                message_type TEXT,
                length INTEGER NOT NULL,
                has_image INTEGER NOT NULL DEFAULT 0,
                has_face INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_messages_group_ts ON messages(group_id, ts)")

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS member_profiles (
                group_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                user_name TEXT,
                first_seen INTEGER NOT NULL,
                last_seen INTEGER NOT NULL,
                message_count INTEGER NOT NULL DEFAULT 0,
                total_length INTEGER NOT NULL DEFAULT 0,
                image_count INTEGER NOT NULL DEFAULT 0,
                face_count INTEGER NOT NULL DEFAULT 0,
                mention_count INTEGER NOT NULL DEFAULT 0,
                active_days INTEGER NOT NULL DEFAULT 1,
                streak_days INTEGER NOT NULL DEFAULT 1,
                last_active_day INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (group_id, user_id)
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_profiles_group_user ON member_profiles(group_id, user_id)")

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS member_hour_stats (
                group_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                hour INTEGER NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (group_id, user_id, hour)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS member_word_stats (
                group_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                token TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (group_id, user_id, token)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS member_target_stats (
                group_id TEXT NOT NULL,
                source_user_id TEXT NOT NULL,
                target_user_id TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (group_id, source_user_id, target_user_id)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS member_face_stats (
                group_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                face_id INTEGER NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (group_id, user_id, face_id)
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(HISTORY_DB, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _update_existing_profile(cur: sqlite3.Cursor, row: sqlite3.Row, now: float, user_name: str, msg_length: int, has_image: bool, has_face: bool, at_count: int, message_day: int) -> None:
    now_int = int(now)
    last_active_day = int(row["last_active_day"] or 0)
    active_days = int(row["active_days"] or 1)
    streak_days = int(row["streak_days"] or 1)
    message_count = int(row["message_count"] or 0) + 1
    total_length = int(row["total_length"] or 0) + msg_length

    if last_active_day <= 0:
        new_active_days = active_days
        new_streak = streak_days
    elif message_day == last_active_day:
        new_active_days = active_days
        new_streak = streak_days
    elif message_day == last_active_day + 1:
        new_active_days = active_days + 1
        new_streak = max(1, streak_days + 1)
    else:
        new_active_days = active_days + 1
        new_streak = 1

    cur.execute(
        """
        UPDATE member_profiles
        SET
            user_name = ?,
            first_seen = CASE
                WHEN ? < first_seen OR first_seen = 0 THEN ?
                ELSE first_seen
            END,
            last_seen = ?,
            message_count = ?,
            total_length = ?,
            image_count = image_count + ?,
            face_count = face_count + ?,
            mention_count = mention_count + ?,
            active_days = ?,
            streak_days = ?,
            last_active_day = ?,
            updated_at = ?
        WHERE group_id = ? AND user_id = ?
        """,
        (
            user_name,
            now_int,
            now_int,
            now_int,
            message_count,
            total_length,
            1 if has_image else 0,
            1 if has_face else 0,
            at_count,
            new_active_days,
            new_streak,
            message_day,
            now_int,
            str(row["group_id"]),
            str(row["user_id"]),
        ),
    )


def log_message(
    group_id: str,
    user_id: str,
    user_name: str,
    message: str,
    *,
    message_id: str | int | None = None,
    reply_to: str | int | None = None,
    at_list: list[str] | None = None,
    message_type: str | None = "group",
    has_image: bool = False,
    has_face: bool = False,
    face_ids: list[int] | None = None,
) -> None:
    """Store a message and update a simple group/user profile."""
    group_id = _normalize_group_id(group_id)
    user_id = _normalize_user_id(user_id)
    if not group_id or not user_id:
        return

    msg = _normalize_text(message)
    if not msg and not has_image and not has_face:
        return

    now = time.time()
    at_ids = _normalize_at_list(at_list)
    face_ids = _normalize_face_ids(face_ids)
    tokens = _extract_tokens(msg)
    msg_type = str(message_type or "group").strip() or "group"
    at_payload = json.dumps(at_ids, ensure_ascii=False)
    msg_length = len(msg)
    message_day = int(time.strftime("%Y%m%d", time.localtime(now)))
    message_hour = int(time.strftime("%H", time.localtime(now)))

    _ensure_db()

    with _DB_LOCK:
        conn = _connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO messages (
                    message_id, group_id, user_id, user_name, ts, message,
                    reply_to, at_list, message_type, length, has_image, has_face
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(message_id) if message_id is not None else None,
                    group_id,
                    user_id,
                    _normalize_nickname(user_name),
                    now,
                    msg,
                    str(reply_to) if reply_to is not None else None,
                    at_payload,
                    msg_type,
                    msg_length,
                    1 if has_image else 0,
                    1 if has_face else 0,
                ),
            )

            cur.execute(
                """
                SELECT
                    group_id, user_id, first_seen, last_seen, message_count,
                    total_length, image_count, face_count, mention_count,
                    active_days, streak_days, last_active_day
                FROM member_profiles
                WHERE group_id = ? AND user_id = ?
                """,
                (group_id, user_id),
            )
            profile = cur.fetchone()
            if profile:
                _update_existing_profile(
                    cur,
                    profile,
                    now,
                    _normalize_nickname(user_name),
                    msg_length,
                    has_image,
                    has_face,
                    len(at_ids),
                    message_day,
                )
            else:
                now_int = int(now)
                cur.execute(
                    """
                    INSERT INTO member_profiles (
                        group_id, user_id, user_name, first_seen, last_seen,
                        message_count, total_length, image_count, face_count, mention_count,
                        active_days, streak_days, last_active_day, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, 1, 1, ?, ?)
                    """,
                    (
                        group_id,
                        user_id,
                        _normalize_nickname(user_name),
                        now_int,
                        now_int,
                        msg_length,
                        1 if has_image else 0,
                        1 if has_face else 0,
                        len(at_ids),
                        message_day,
                        now_int,
                    ),
                )

            cur.execute(
                """
                INSERT INTO member_hour_stats (group_id, user_id, hour, count)
                VALUES (?, ?, ?, 1)
                ON CONFLICT(group_id, user_id, hour) DO UPDATE SET
                    count = count + 1
                """,
                (group_id, user_id, message_hour),
            )

            for target_id in at_ids:
                if target_id == user_id:
                    continue
                cur.execute(
                    """
                    INSERT INTO member_target_stats (
                        group_id, source_user_id, target_user_id, count
                    ) VALUES (?, ?, ?, 1)
                    ON CONFLICT(group_id, source_user_id, target_user_id) DO UPDATE SET
                        count = count + 1
                    """,
                    (group_id, user_id, target_id),
                )

            for token in tokens:
                cur.execute(
                    """
                    INSERT INTO member_word_stats (
                        group_id, user_id, token, count
                    ) VALUES (?, ?, ?, 1)
                    ON CONFLICT(group_id, user_id, token) DO UPDATE SET
                        count = count + 1
                    """,
                    (group_id, user_id, token),
                )

            for face_id in face_ids:
                cur.execute(
                    """
                    INSERT INTO member_face_stats (
                        group_id, user_id, face_id, count
                    ) VALUES (?, ?, ?, 1)
                    ON CONFLICT(group_id, user_id, face_id) DO UPDATE SET
                        count = count + 1
                    """,
                    (group_id, user_id, face_id),
                )

            cur.execute(
                """
                DELETE FROM messages
                WHERE id NOT IN (
                    SELECT id FROM messages
                    WHERE group_id = ?
                    ORDER BY ts DESC, id DESC
                    LIMIT ?
                ) AND group_id = ?
                """,
                (group_id, MAX_MESSAGES_PER_GROUP, group_id),
            )

            conn.commit()
        finally:
            conn.close()




def fetch_recent_messages(group_id: str | None = None, limit: int = 80) -> list[dict]:
    if group_id is not None:
        group_id = _normalize_group_id(group_id)
    limit = max(1, min(MAX_MESSAGES_PER_QUERY, int(limit)))
    if group_id is not None and not group_id:
        return []
    _ensure_db()

    conn = _connect()
    try:
        cur = conn.cursor()
        if group_id is None:
            cur.execute(
                """
                SELECT id, message_id, group_id, user_id, user_name, ts, message,
                       reply_to, at_list, message_type, length, has_image, has_face
                FROM messages
                ORDER BY ts DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            )
        else:
            cur.execute(
                """
                SELECT id, message_id, group_id, user_id, user_name, ts, message,
                       reply_to, at_list, message_type, length, has_image, has_face
                FROM messages
                WHERE group_id = ?
                ORDER BY ts DESC, id DESC
                LIMIT ?
                """,
                (group_id, limit),
            )
        rows = cur.fetchall()
    finally:
        conn.close()

    out: list[dict] = []
    for row in reversed(rows):
        raw_at = row["at_list"]
        if raw_at:
            try:
                at_data = json.loads(str(raw_at))
                at_list = _normalize_at_list(at_data if isinstance(at_data, list) else [])
            except Exception:
                at_list = []
        else:
            at_list = []

        out.append(
            {
                "id": int(row["id"]),
                "message_id": row["message_id"],
                "group_id": row["group_id"],
                "user_id": row["user_id"],
                "user_name": row["user_name"] or "User",
                "timestamp": float(row["ts"]),
                "message": row["message"] or "",
                "reply_to": row["reply_to"],
                "at_list": at_list,
                "message_type": row["message_type"] or "group",
                "length": int(row["length"] or 0),
                "has_image": bool(int(row["has_image"] or 0)),
                "has_face": bool(int(row["has_face"] or 0)),
            }
        )
    return out


def get_latest_message_time(group_id: str) -> float | None:
    group_id = _normalize_group_id(group_id)
    if not group_id:
        return None
    _ensure_db()
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT MAX(ts) as latest FROM messages WHERE group_id = ?", (group_id,))
        row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return None
    latest = row["latest"] if isinstance(row, sqlite3.Row) else row[0]  # type: ignore[index]
    if latest is None:
        return None
    return float(latest)


def get_active_group_ids() -> list[str]:
    _ensure_db()
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT group_id FROM messages")
        rows = cur.fetchall()
    finally:
        conn.close()
    return [str(r[0]) for r in rows if r[0]]


def _pick_scalar(cur: sqlite3.Cursor, sql: str, params: tuple) -> tuple[str | None, int]:
    cur.execute(sql, params)
    row = cur.fetchone()
    if not row:
        return None, 0
    return (str(row[0]) if row[0] is not None else None, int(row[1] or 0))


def get_member_profile(group_id: str, user_id: str) -> dict | None:
    group_id = _normalize_group_id(group_id)
    user_id = _normalize_user_id(user_id)
    if not group_id or not user_id:
        return None

    _ensure_db()
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                user_name, first_seen, last_seen, message_count,
                total_length, image_count, face_count, mention_count,
                active_days, streak_days, last_active_day
            FROM member_profiles
            WHERE group_id = ? AND user_id = ?
            """,
            (group_id, user_id),
        )
        row = cur.fetchone()
        if not row:
            return None

        most_active_hour, _ = _pick_scalar(
            cur,
            """
            SELECT hour, count
            FROM member_hour_stats
            WHERE group_id = ? AND user_id = ?
            ORDER BY count DESC, hour DESC
            LIMIT 1
            """,
            (group_id, user_id),
        )
        most_common_word, word_count = _pick_scalar(
            cur,
            """
            SELECT token, count
            FROM member_word_stats
            WHERE group_id = ? AND user_id = ?
            ORDER BY count DESC, token ASC
            LIMIT 1
            """,
            (group_id, user_id),
        )
        most_mentioned_user, mention_target_count = _pick_scalar(
            cur,
            """
            SELECT target_user_id, count
            FROM member_target_stats
            WHERE group_id = ? AND source_user_id = ?
            ORDER BY count DESC, target_user_id ASC
            LIMIT 1
            """,
            (group_id, user_id),
        )
        most_used_face_row = None
        cur.execute(
            """
            SELECT face_id, count
            FROM member_face_stats
            WHERE group_id = ? AND user_id = ?
            ORDER BY count DESC, face_id ASC
            LIMIT 1
            """,
            (group_id, user_id),
        )
        most_used_face = cur.fetchone()
    finally:
        conn.close()

    message_count = int(row["message_count"] or 0)
    total_length = int(row["total_length"] or 0)
    avg_length = round(total_length / message_count, 2) if message_count > 0 else 0.0

    face_id: int | None = int(most_used_face["face_id"]) if most_used_face and most_used_face["face_id"] is not None else None  # type: ignore[index]
    face_count = int(most_used_face["count"]) if most_used_face and most_used_face["count"] is not None else 0  # type: ignore[index]

    return {
        "user_name": row["user_name"] or "User",
        "first_seen": float(row["first_seen"] or 0),
        "last_seen": float(row["last_seen"] or 0),
        "message_count": message_count,
        "total_length": total_length,
        "average_length": avg_length,
        "image_count": int(row["image_count"] or 0),
        "face_count": int(row["face_count"] or 0),
        "mention_count": int(row["mention_count"] or 0),
        "active_days": int(row["active_days"] or 0),
        "streak_days": int(row["streak_days"] or 0),
        "most_active_hour": int(most_active_hour) if most_active_hour else None,
        "most_common_word": most_common_word or "",
        "most_common_word_count": int(word_count or 0),
        "most_mentioned_user": str(most_mentioned_user or ""),
        "most_mentioned_user_count": int(mention_target_count or 0),
        "most_used_face_id": face_id,
        "most_used_face_count": face_count,
        "last_active_day": int(row["last_active_day"] or 0),
    }


def _build_profile_rows(group_id: str, limit: int, order_by: str) -> list[dict]:
    group_id = _normalize_group_id(group_id)
    if not group_id:
        return []

    if order_by not in {
        "message_count",
        "total_length",
        "average_length",
        "image_count",
        "face_count",
        "mention_count",
        "active_days",
        "streak_days",
    }:
        order_by = "message_count"

    order_map = {
        "message_count": "message_count",
        "total_length": "total_length",
        "average_length": "(CASE WHEN message_count > 0 THEN CAST(total_length AS REAL)/message_count ELSE 0 END)",
        "image_count": "image_count",
        "face_count": "face_count",
        "mention_count": "mention_count",
        "active_days": "active_days",
        "streak_days": "streak_days",
    }

    limit = max(1, min(MAX_PROFILE_TOP, int(limit)))

    _ensure_db()
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT user_id, user_name, first_seen, last_seen,
                   message_count, total_length, image_count, face_count,
                   mention_count, active_days, streak_days, last_active_day
            FROM member_profiles
            WHERE group_id = ?
            ORDER BY {order_map[order_by]} DESC, total_length DESC, last_seen DESC
            LIMIT ?
            """,
            (group_id, limit),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    out: list[dict] = []
    for row in rows:
        msg_count = int(row["message_count"] or 0)
        total_length = int(row["total_length"] or 0)
        avg_length = round(total_length / msg_count, 2) if msg_count > 0 else 0.0
        out.append(
            {
                "user_id": str(row["user_id"]),
                "user_name": row["user_name"] or "User",
                "first_seen": float(row["first_seen"] or 0),
                "last_seen": float(row["last_seen"] or 0),
                "message_count": msg_count,
                "average_length": avg_length,
                "total_length": total_length,
                "image_count": int(row["image_count"] or 0),
                "face_count": int(row["face_count"] or 0),
                "mention_count": int(row["mention_count"] or 0),
                "active_days": int(row["active_days"] or 0),
                "streak_days": int(row["streak_days"] or 0),
                "last_active_day": int(row["last_active_day"] or 0),
            }
        )
    return out


def get_top_member_profiles(group_id: str, limit: int = 20) -> list[dict]:
    return _build_profile_rows(group_id, limit=limit, order_by="message_count")


def get_top_profiles_by(group_id: str, *, limit: int = 20, order_by: str = "message_count") -> list[dict]:
    return _build_profile_rows(group_id, limit=limit, order_by=order_by)


def get_group_member_profiles(group_id: str, limit: int = 200) -> list[dict]:
    return _build_profile_rows(group_id, limit=limit, order_by="message_count")


