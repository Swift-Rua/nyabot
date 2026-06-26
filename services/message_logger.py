"""SQLite based logger for group messages and lightweight member profiles."""

import json
import os
import re
import sqlite3
import random
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
MAX_TERM_TRACK_PER_MESSAGE = 40
HISTORY_POOL_500 = 500
HISTORY_POOL_1000 = 1000

GLOBAL_SCOPE = "global"
SCOPE_GLOBAL_TERMS = "global_terms"
SCOPE_GROUP_TERMS = "group_terms"

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
_CHAR_TERM_RE = re.compile(r"[A-Za-z0-9\u4e00-\u9fff]+")
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


def _extract_phrases(message: str) -> list[str]:
    text = clean_text(message).lower()
    if not text:
        return []

    compact = re.sub(r"[\s\[\]\(\)！!，,。.、：:；;?？~～\-_=+|<>【】“”‘’\"'`·]+", "", text)
    compact = compact.replace("&amp;", "").replace("amp;", "")
    if len(compact) < 2:
        return []

    terms: list[str] = []
    seen: set[str] = set()
    for length in (2, 3, 4):
        if len(compact) < length:
            continue
        window_step = 1 if len(compact) <= 16 else 2
        for idx in range(0, len(compact) - length + 1, window_step):
            piece = compact[idx : idx + length]
            if piece in _STOP_WORDS or piece in seen:
                continue
            seen.add(piece)
            terms.append(piece)
            if len(terms) >= MAX_TERM_TRACK_PER_MESSAGE:
                return terms

    for segment in _CHAR_TERM_RE.findall(compact):
        seg = segment.strip()
        if len(seg) < 2 or seg in _STOP_WORDS:
            continue
        candidates = [seg]
        if len(seg) > 4:
            candidates.extend((seg[:4], seg[-4:]))
        for piece in candidates:
            if not piece or piece in seen or piece in _STOP_WORDS:
                continue
            seen.add(piece)
            terms.append(piece)
            if len(terms) >= MAX_TERM_TRACK_PER_MESSAGE:
                return terms

    return terms


_FILLER_MARKERS = (
    "哈哈",
    "哈哈哈",
    "哈哈哈哈",
    "哈哈哈哈哈",
    "草",
    "卧槽",
    "绷",
    "笑死",
    "真的假的",
    "牛逼",
    "666",
    "？？？",
    "...",
    "。。。",
)


def _extract_fillers(message: str) -> list[str]:
    text = clean_text(message).lower()
    if not text:
        return []

    out: list[str] = []
    seen: set[str] = set()
    for token in _FILLER_MARKERS:
        if token in text and token not in seen:
            seen.add(token)
            out.append(token)
            if len(out) >= 12:
                break
    return out


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


def _normalize_pool_size(size: object | None) -> int:
    try:
        value = int(size)
    except (TypeError, ValueError):
        return MAX_MESSAGES_PER_GROUP
    if value <= 0:
        return 1
    return max(1, min(MAX_MESSAGES_PER_GROUP, value))


def _parse_at_payload(raw_at: object) -> list[str]:
    if not raw_at:
        return []
    if isinstance(raw_at, list):
        return _normalize_at_list(raw_at)
    try:
        return _normalize_at_list(json.loads(str(raw_at)))
    except Exception:
        return []


def _to_message_dict(row: sqlite3.Row) -> dict:
    return {
        "id": int(row["id"]),
        "message_id": row["message_id"],
        "group_id": row["group_id"],
        "user_id": row["user_id"],
        "user_name": row["user_name"] or "User",
        "timestamp": float(row["ts"]),
        "message": row["message"] or "",
        "reply_to": row["reply_to"],
        "at_list": _parse_at_payload(row["at_list"]),
        "message_type": row["message_type"] or "group",
        "length": int(row["length"] or 0),
        "has_image": bool(int(row["has_image"] or 0)),
        "has_face": bool(int(row["has_face"] or 0)),
    }


def _track_term_counts(
    cur: sqlite3.Cursor,
    scope: str,
    group_id: str,
    token_type: str,
    terms: list[str],
    now: int,
) -> None:
    safe_group = str(group_id).strip() if str(group_id).strip() else ""
    for token in terms:
        if not token:
            continue
        cur.execute(
            """
            INSERT INTO global_terms (
                scope, group_id, token_type, token, count, first_seen, last_seen
            ) VALUES (?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(scope, group_id, token_type, token) DO UPDATE SET
                count = count + 1,
                last_seen = excluded.last_seen
            """,
            (scope, safe_group, token_type, _normalize_text(token), now, now),
        )


def _decay_terms(cur: sqlite3.Cursor, now: int) -> None:
    cur.execute("SELECT COUNT(*) FROM global_terms")
    total = int(cur.fetchone()[0] or 0)
    if total <= MAX_PROFILE_TOP * 20:
        return

    cutoff = now - 30 * 24 * 3600
    cur.execute("DELETE FROM global_terms WHERE last_seen < ?", (cutoff,))


def _get_term_rows(cur: sqlite3.Cursor, scope: str, group_id: str, token_type: str, limit: int) -> list[dict]:
    if scope not in {SCOPE_GLOBAL_TERMS, SCOPE_GROUP_TERMS}:
        scope = SCOPE_GLOBAL_TERMS
    limit = max(1, min(300, int(limit)))
    cur.execute(
        """
        SELECT token, count, first_seen, last_seen
        FROM global_terms
        WHERE scope = ? AND token_type = ? AND group_id = ?
        ORDER BY count DESC, token ASC
        LIMIT ?
        """,
        (scope, token_type, str(group_id).strip() if str(group_id).strip() else GLOBAL_SCOPE, limit),
    )
    rows = cur.fetchall()
    out: list[dict] = []
    for row in rows:
        out.append(
            {
                "token": str(row["token"]),
                "count": int(row["count"] or 0),
                "first_seen": float(row["first_seen"] or 0),
                "last_seen": float(row["last_seen"] or 0),
            }
        )
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
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS global_terms (
                scope TEXT NOT NULL,
                group_id TEXT NOT NULL,
                token_type TEXT NOT NULL,
                token TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                first_seen INTEGER NOT NULL,
                last_seen INTEGER NOT NULL,
                PRIMARY KEY (scope, group_id, token_type, token)
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_global_terms_scope ON global_terms(scope, group_id, token_type, count DESC)")
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

            phrases = _extract_phrases(msg)
            fillers = _extract_fillers(msg)

            now_int = int(now)
            _track_term_counts(cur, SCOPE_GROUP_TERMS, group_id, "word", tokens, now_int)
            _track_term_counts(cur, SCOPE_GROUP_TERMS, group_id, "phrase", phrases, now_int)
            _track_term_counts(cur, SCOPE_GROUP_TERMS, group_id, "filler", fillers, now_int)
            _track_term_counts(cur, SCOPE_GLOBAL_TERMS, GLOBAL_SCOPE, "word", tokens, now_int)
            _track_term_counts(cur, SCOPE_GLOBAL_TERMS, GLOBAL_SCOPE, "phrase", phrases, now_int)
            _track_term_counts(cur, SCOPE_GLOBAL_TERMS, GLOBAL_SCOPE, "filler", fillers, now_int)

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

            _decay_terms(cur, now_int)
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

    out: list[dict] = [_to_message_dict(row) for row in reversed(rows)]
    return out


def get_message_pool(group_id: str, pool_size: int = MAX_MESSAGES_PER_GROUP) -> list[dict]:
    """Get latest messages from one group by pool size (500/1000/5000)."""
    group_id = _normalize_group_id(group_id)
    if not group_id:
        return []

    size = _normalize_pool_size(pool_size)
    _ensure_db()

    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, message_id, group_id, user_id, user_name, ts, message,
                   reply_to, at_list, message_type, length, has_image, has_face
            FROM messages
            WHERE group_id = ?
            ORDER BY ts DESC, id DESC
            LIMIT ?
            """,
            (group_id, size),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    return [_to_message_dict(row) for row in reversed(rows)]


def sample_message_pool(
    group_id: str,
    *,
    pool_size: int = MAX_MESSAGES_PER_GROUP,
    sample_size: int = 1,
) -> list[dict]:
    """Randomly sample message entries from a group's recent pool."""
    pool = get_message_pool(group_id=group_id, pool_size=pool_size)
    if not pool:
        return []

    sample_size = max(1, min(int(sample_size), len(pool)))
    return random.sample(pool, sample_size)


def get_messages_by_user(
    group_id: str,
    user_id: str,
    *,
    pool_size: int = MAX_MESSAGES_PER_GROUP,
    limit: int = 80,
) -> list[dict]:
    """Get recent messages from one user inside a group's recent pool."""
    group_id = _normalize_group_id(group_id)
    uid = _normalize_user_id(user_id)
    if not group_id or not uid:
        return []

    size = _normalize_pool_size(pool_size)
    limit = max(1, min(300, int(limit)))

    _ensure_db()
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT m.id, m.message_id, m.group_id, m.user_id, m.user_name, m.ts, m.message,
                   m.reply_to, m.at_list, m.message_type, m.length, m.has_image, m.has_face
            FROM messages m
            JOIN (
                SELECT id
                FROM messages
                WHERE group_id = ?
                ORDER BY ts DESC, id DESC
                LIMIT ?
            ) AS latest ON latest.id = m.id
            WHERE m.group_id = ? AND m.user_id = ?
            ORDER BY m.ts DESC, m.id DESC
            LIMIT ?
            """,
            (group_id, size, group_id, uid, limit),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    return [_to_message_dict(row) for row in reversed(rows)]


def get_messages_by_keyword(
    group_id: str,
    keyword: str,
    *,
    pool_size: int = MAX_MESSAGES_PER_GROUP,
    limit: int = 40,
) -> list[dict]:
    """Get recent messages that contain specific keyword in a group's pool."""
    group_id = _normalize_group_id(group_id)
    key = _normalize_text(keyword)
    if not group_id or not key:
        return []

    size = _normalize_pool_size(pool_size)
    limit = max(1, min(300, int(limit)))
    pattern = f"%{key}%"

    _ensure_db()
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT m.id, m.message_id, m.group_id, m.user_id, m.user_name, m.ts, m.message,
                   m.reply_to, m.at_list, m.message_type, m.length, m.has_image, m.has_face
            FROM messages m
            JOIN (
                SELECT id
                FROM messages
                WHERE group_id = ?
                ORDER BY ts DESC, id DESC
                LIMIT ?
            ) AS latest ON latest.id = m.id
            WHERE m.group_id = ? AND lower(m.message) LIKE lower(?)
            ORDER BY m.ts DESC, m.id DESC
            LIMIT ?
            """,
            (group_id, size, group_id, pattern, limit),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    return [_to_message_dict(row) for row in reversed(rows)]


def get_messages_by_time(
    group_id: str,
    *,
    start_ts: float | int,
    end_ts: float | int,
    pool_size: int = MAX_MESSAGES_PER_GROUP,
    limit: int = 80,
) -> list[dict]:
    """Get messages inside a unix-time window for one group."""
    group_id = _normalize_group_id(group_id)
    if not group_id:
        return []

    try:
        start = float(start_ts)
        end = float(end_ts)
    except (TypeError, ValueError):
        return []
    if end <= start:
        return []

    size = _normalize_pool_size(pool_size)
    limit = max(1, min(300, int(limit)))

    _ensure_db()
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT m.id, m.message_id, m.group_id, m.user_id, m.user_name, m.ts, m.message,
                   m.reply_to, m.at_list, m.message_type, m.length, m.has_image, m.has_face
            FROM messages m
            JOIN (
                SELECT id
                FROM messages
                WHERE group_id = ?
                ORDER BY ts DESC, id DESC
                LIMIT ?
            ) AS latest ON latest.id = m.id
            WHERE m.group_id = ? AND m.ts BETWEEN ? AND ?
            ORDER BY m.ts DESC, m.id DESC
            LIMIT ?
            """,
            (group_id, size, group_id, start, end, limit),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    return [_to_message_dict(row) for row in reversed(rows)]


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


def get_message_snapshot(group_id: str, message_id: str) -> dict | None:
    """Return cached message row by group/message id."""
    group_id = _normalize_group_id(group_id)
    message_id = _normalize_text(message_id)
    if not group_id or not message_id:
        return None

    _ensure_db()
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT user_id, user_name, ts, message, reply_to, at_list, message_type
            FROM messages
            WHERE group_id = ? AND message_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (group_id, message_id),
        )
        row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        return None

    return {
        "user_id": str(row["user_id"] or ""),
        "user_name": row["user_name"] or "",
        "timestamp": float(row["ts"] or 0),
        "message": _normalize_text(row["message"] or ""),
        "reply_to": str(row["reply_to"]) if row["reply_to"] is not None else "",
        "at_list": _normalize_at_list(json.loads(str(row["at_list"])) if row["at_list"] else []),
        "message_type": str(row["message_type"] or "group").strip() or "group",
    }


def get_top_words(group_id: str | None = None, limit: int = 20) -> list[dict]:
    """Get top keywords by usage count in member_word_stats."""
    limit = max(1, min(300, int(limit)))

    if group_id is not None:
        group_id = _normalize_group_id(group_id)
        if not group_id:
            return []

    _ensure_db()
    conn = _connect()
    try:
        cur = conn.cursor()
        if group_id is None:
            cur.execute(
                """
                SELECT token, SUM(count) as total_count
                FROM member_word_stats
                GROUP BY token
                HAVING total_count > 0
                ORDER BY total_count DESC, token ASC
                LIMIT ?
                """,
                (limit,),
            )
        else:
            cur.execute(
                """
                SELECT token, SUM(count) as total_count
                FROM member_word_stats
                WHERE group_id = ?
                GROUP BY token
                HAVING total_count > 0
                ORDER BY total_count DESC, token ASC
                LIMIT ?
                """,
                (group_id, limit),
            )
        rows = cur.fetchall()
    finally:
        conn.close()

    out: list[dict] = []
    for row in rows:
        out.append({"token": str(row["token"]), "count": int(row["total_count"] or 0)})
    return out


def get_top_phrases(group_id: str | None = None, limit: int = 20) -> list[dict]:
    scope = SCOPE_GLOBAL_TERMS if group_id is None else SCOPE_GROUP_TERMS
    key = _normalize_group_id(group_id) if group_id is not None else GLOBAL_SCOPE
    limit = max(1, min(300, int(limit)))

    _ensure_db()
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT token, count
            FROM global_terms
            WHERE scope = ? AND group_id = ? AND token_type = 'phrase'
            ORDER BY count DESC, token ASC
            LIMIT ?
            """,
            (scope, key, limit),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    out: list[dict] = []
    for row in rows:
        out.append({"token": str(row["token"]), "count": int(row["count"] or 0)})
    return out


def get_top_fillers(group_id: str | None = None, limit: int = 20) -> list[dict]:
    scope = SCOPE_GLOBAL_TERMS if group_id is None else SCOPE_GROUP_TERMS
    key = _normalize_group_id(group_id) if group_id is not None else GLOBAL_SCOPE
    limit = max(1, min(300, int(limit)))

    _ensure_db()
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT token, count
            FROM global_terms
            WHERE scope = ? AND group_id = ? AND token_type = 'filler'
            ORDER BY count DESC, token ASC
            LIMIT ?
            """,
            (scope, key, limit),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    out: list[dict] = []
    for row in rows:
        out.append({"token": str(row["token"]), "count": int(row["count"] or 0)})
    return out


def get_user_words(group_id: str, user_id: str, limit: int = 20) -> list[dict]:
    group_id = _normalize_group_id(group_id)
    user_id = _normalize_user_id(user_id)
    if not group_id or not user_id:
        return []

    limit = max(1, min(300, int(limit)))
    _ensure_db()
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT token, count
            FROM member_word_stats
            WHERE group_id = ? AND user_id = ?
            ORDER BY count DESC, token ASC
            LIMIT ?
            """,
            (group_id, user_id, limit),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    out: list[dict] = []
    for row in rows:
        out.append({"token": str(row["token"]), "count": int(row["count"] or 0)})
    return out


def get_top_member_profiles(group_id: str, limit: int = 20) -> list[dict]:
    return _build_profile_rows(group_id, limit=limit, order_by="message_count")


def get_top_profiles_by(group_id: str, *, limit: int = 20, order_by: str = "message_count") -> list[dict]:
    return _build_profile_rows(group_id, limit=limit, order_by=order_by)


def get_group_member_profiles(group_id: str, limit: int = 200) -> list[dict]:
    return _build_profile_rows(group_id, limit=limit, order_by="message_count")


