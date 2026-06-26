"""Meme bank for lightweight no-token offline replies."""

import os
import random
import re
import sqlite3
import threading
import time

from services.utils import clean_text


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEME_DB = os.path.join(BASE_DIR, "data", "memes.db")

MEME_POOL_LIMIT = 500
MIN_MEME_QUALITY = 2
MIN_MEME_LENGTH = 2
MAX_MEME_LENGTH = 15
MAX_MEME_KEEP_CANDIDATES = 20
MEME_HEAT_DECAY = 0.80

GLOBAL_SCOPE = "global"

_DB_LOCK = threading.Lock()
_WORD_RE = re.compile(r"[A-Za-z0-9\u4e00-\u9fff]{2,}")
_AT_TOKEN_RE = re.compile(r"\\[CQ:at,qq=\\d+\\]")
_MENTION_PREFIX_RE = re.compile(r"^\\s*[@\\w]+\\s*[,，:：\\s]*")


def _normalize_group_id(group_id: object | None) -> str:
    raw = str(group_id).strip() if group_id is not None else ""
    if not raw:
        return GLOBAL_SCOPE
    return raw


def _normalize_user_id(user_id: object) -> str:
    raw = str(user_id).strip() if user_id is not None else ""
    return raw


def _normalize_text(text: object) -> str:
    return clean_text(str(text)) if text is not None else ""


def _normalize_meme_text(text: str, user_name: str | None) -> str:
    cleaned = _AT_TOKEN_RE.sub("", text or "")
    cleaned = _MENTION_PREFIX_RE.sub("", cleaned)
    if user_name:
        name = _normalize_text(user_name)
        if name:
            cleaned = re.sub(
                rf"^\\s*{re.escape(name)}\\s*[:：,，\\s]*",
                "",
                cleaned,
            )
    cleaned = re.sub(r"\\s+", " ", cleaned).strip()
    return cleaned


def _quality_score(text: str) -> int:
    if not text:
        return 0
    if len(text) < MIN_MEME_LENGTH or len(text) > MAX_MEME_LENGTH:
        return 0

    tokens = _WORD_RE.findall(text)
    unique_tokens = set(tokens)
    score = 1 + min(6, len(unique_tokens))

    punctuation_hits = len(re.findall(r"[!?！？~…。]", text))
    if punctuation_hits:
        score += min(3, punctuation_hits)

    if any(ch.isdigit() for ch in text):
        score += 1

    if len(unique_tokens) >= 2 and not text.endswith("..."):
        score += 1

    return max(0, min(12, score))


def _ensure_db_columns(cur: sqlite3.Cursor) -> None:
    cur.execute("PRAGMA table_info(memes)")
    columns = {row[1] for row in cur.fetchall()}

    if "occurrence_count" not in columns:
        cur.execute("ALTER TABLE memes ADD COLUMN occurrence_count INTEGER NOT NULL DEFAULT 1")
    if "last_used_at" not in columns:
        cur.execute("ALTER TABLE memes ADD COLUMN last_used_at INTEGER")
    if "updated_at" not in columns:
        # fallback for old schema that only had created_at
        cur.execute("ALTER TABLE memes ADD COLUMN updated_at INTEGER NOT NULL DEFAULT 0")


def _ensure_db() -> None:
    os.makedirs(os.path.dirname(MEME_DB), exist_ok=True)
    conn = sqlite3.connect(MEME_DB)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS memes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT NOT NULL,
                source_user_id TEXT NOT NULL,
                source_user_name TEXT,
                source_message_id TEXT,
                content TEXT NOT NULL,
                quality INTEGER NOT NULL DEFAULT 0,
                quality_score INTEGER NOT NULL DEFAULT 0,
                seen_count INTEGER NOT NULL DEFAULT 0,
                occurrence_count INTEGER NOT NULL DEFAULT 1,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                last_used_at INTEGER
            )
            """
        )
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_memes_unique ON memes(group_id, source_user_id, content)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_memes_quality ON memes(quality DESC, occurrence_count DESC, updated_at DESC)")
        _ensure_db_columns(cur)
        conn.commit()
    finally:
        conn.close()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(MEME_DB, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _trim_pool(conn: sqlite3.Connection, adding: bool = False) -> None:
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM memes")
    total = int(cur.fetchone()[0] or 0)
    overflow = total - MEME_POOL_LIMIT
    if adding and total >= MEME_POOL_LIMIT:
        overflow += 1
    if overflow <= 0:
        return

    cur.execute(
        """
        DELETE FROM memes
        WHERE id IN (
            SELECT id FROM memes
            ORDER BY RANDOM()
            LIMIT ?
        )
        """,
        (overflow,),
    )


def _days_since(timestamp: int, now: float) -> int:
    if timestamp <= 0:
        return 0
    return max(0, int((now - float(timestamp)) / 86400))


def _decay_weight(raw_weight: int, last_used_ts: int, now: float) -> int:
    age_days = _days_since(last_used_ts, now)
    if age_days <= 0:
        return max(1, raw_weight)
    value = raw_weight * (MEME_HEAT_DECAY ** age_days)
    return max(1, int(value))


def _collect_candidates(
    cur: sqlite3.Cursor,
    group_id: str | None,
    seed: str | None,
    min_quality: int,
    limit: int,
) -> list[sqlite3.Row]:
    filters = _normalize_text(seed)
    params: list[object] = [min_quality]
    where = "WHERE quality >= ?"

    if group_id:
        where += " AND group_id = ?"
        params.append(group_id)

    if filters:
        tokens = list(_WORD_RE.findall(filters))
        if not tokens:
            tokens = [filters]
        like_filters = []
        for token in tokens[:MAX_MEME_KEEP_CANDIDATES]:
            like_filters.append("content LIKE ?")
            params.append(f"%{token}%")
        if like_filters:
            where += " AND (" + " OR ".join(like_filters) + ")"
        else:
            where += " AND 1=0"

    cur.execute(
        f"""
        SELECT id, content, quality, occurrence_count, seen_count, updated_at, last_used_at
        FROM memes
        {where}
        ORDER BY updated_at DESC, quality DESC
        LIMIT {max(1, int(limit))}
        """,
        tuple(params),
    )
    return cur.fetchall()


def _sample_content(rows: list[sqlite3.Row], now: float) -> tuple[str | None, int | None]:
    if not rows:
        return None, None

    weighted: list[tuple[int, int, str]] = []
    for row in rows:
        quality = int(row["quality"] or 0)
        seen = int(row["seen_count"] or 0)
        occ = int(row["occurrence_count"] or 1)
        updated = int(row["updated_at"] or 0)
        last_used = int(row["last_used_at"] or updated)

        base = 1 + quality * 2 + occ + seen
        weight = _decay_weight(base, last_used, now)
        if weight <= 0:
            weight = 1
        weighted.append((weight, int(row["id"]), str(row["content"])))

    if not weighted:
        return None, None

    total_weights = [w for w, _, _ in weighted]
    pick = random.choices(range(len(weighted)), weights=total_weights, k=1)[0]
    _, meme_id, content = weighted[pick]
    return content, meme_id


def record_meme(
    group_id: str | None,
    user_id: str | None,
    user_name: str | None,
    text: str,
    *,
    message_id: str | int | None = None,
) -> str | None:
    """Store a normalized meme candidate. Returns inserted text on success."""
    group = _normalize_group_id(group_id)
    user = _normalize_user_id(user_id)
    if not group or not user:
        return None

    normalized = _normalize_meme_text(text, user_name)
    content = normalized.strip()
    if not content:
        return None

    quality = _quality_score(content)
    if quality < MIN_MEME_QUALITY:
        return None

    now = int(time.time())
    _ensure_db()

    with _DB_LOCK:
        conn = _connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT 1
                FROM memes
                WHERE group_id = ? AND source_user_id = ? AND content = ?
                LIMIT 1
                """,
                (group, user, content),
            )
            is_new = cur.fetchone() is None
            _trim_pool(conn, adding=is_new)

            cur.execute(
                """
                INSERT INTO memes (
                    group_id, source_user_id, source_user_name, source_message_id,
                    content, quality, quality_score, seen_count, occurrence_count,
                    created_at, updated_at, last_used_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 1, ?, ?, ?)
                ON CONFLICT(group_id, source_user_id, content) DO UPDATE SET
                    quality = CASE WHEN excluded.quality > quality THEN excluded.quality ELSE quality END,
                    quality_score = CASE WHEN excluded.quality_score > quality_score THEN excluded.quality_score ELSE quality_score END,
                    source_user_name = excluded.source_user_name,
                    source_message_id = excluded.source_message_id,
                    occurrence_count = occurrence_count + 1,
                    updated_at = excluded.updated_at
                """,
                (
                    group,
                    user,
                    _normalize_text(user_name),
                    str(message_id) if message_id is not None else None,
                    content,
                    quality,
                    quality,
                    now,
                    now,
                    now,
                ),
            )
            conn.commit()
            return content
        finally:
            conn.close()


def touch_meme(content: str | None = None, meme_id: int | None = None) -> None:
    if meme_id is None and not content:
        return
    content = _normalize_text(content)
    if not content and meme_id is None:
        return

    now = int(time.time())
    _ensure_db()
    with _DB_LOCK:
        conn = _connect()
        try:
            cur = conn.cursor()
            if meme_id is not None:
                cur.execute(
                    """
                    UPDATE memes
                    SET
                        seen_count = seen_count + 1,
                        last_used_at = ?
                    WHERE id = ?
                    """,
                    (now, meme_id),
                )
            else:
                cur.execute(
                    """
                    UPDATE memes
                    SET
                        seen_count = seen_count + 1,
                        last_used_at = ?
                    WHERE content = ? AND group_id = ?
                    """,
                    (now, content, GLOBAL_SCOPE),
                )
            conn.commit()
        finally:
            conn.close()


def get_random_meme(
    group_id: str | None = None,
    *,
    seed: str | None = None,
    min_quality: int | None = None,
) -> str | None:
    """Return a random meme text for no-token replies."""
    _ensure_db()
    min_quality = MIN_MEME_QUALITY if min_quality is None else int(min_quality)
    min_quality = max(0, min_quality)

    normalized_seed = _normalize_text(seed)
    now = float(time.time())
    target_group = _normalize_group_id(group_id) if group_id is not None else None

    conn = _connect()
    try:
        cur = conn.cursor()

        scopes = [target_group]
        if target_group is not None:
            scopes.append(None)

        for scope in scopes:
            candidates = _collect_candidates(cur, scope, normalized_seed, min_quality, MAX_MEME_KEEP_CANDIDATES)
            text, meme_id = _sample_content(candidates, now)
            if text and meme_id is not None:
                touch_meme(meme_id=meme_id)
                return text

        # full fallback across all groups
        fallback = _collect_candidates(cur, None, None, min_quality, max(1, int(MAX_MEME_KEEP_CANDIDATES / 2)))
        text, meme_id = _sample_content(fallback, now)
        if text and meme_id is not None:
            touch_meme(meme_id=meme_id)
            return text

        return None
    finally:
        conn.close()
