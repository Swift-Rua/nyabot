"""Learn and sample reply-chain patterns for no-token situations."""

import os
import random
import re
import sqlite3
import threading
import time

from services.utils import clean_text


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHAIN_DB = os.path.join(BASE_DIR, "data", "reply_chain.db")

REPLY_CHAIN_POOL_LIMIT = 5000
MIN_REPLY_QUALITY = 1
MAX_TEXT_LENGTH = 220
_WORD_RE = re.compile(r"[A-Za-z0-9\u4e00-\u9fff]{2,}")
_GLOBAL_SCOPE = "global"
_DB_LOCK = threading.Lock()


def _normalize_group_id(group_id: object | None) -> str:
    raw = str(group_id).strip() if group_id is not None and str(group_id).strip() else ""
    return raw or _GLOBAL_SCOPE


def _normalize_text(text: object) -> str:
    return clean_text(str(text)) if text is not None else ""


def _quality(seed: str, reply: str) -> int:
    if not seed or not reply:
        return 0
    score = 1
    score += min(6, len(seed))
    score += min(8, len(reply))
    s_tokens = set(_WORD_RE.findall(seed.lower()))
    r_tokens = set(_WORD_RE.findall(reply.lower()))
    score += min(6, len(s_tokens.intersection(r_tokens)))
    if seed in reply:
        score += 2
    return max(0, score // 3)


def _ensure_db() -> None:
    os.makedirs(os.path.dirname(CHAIN_DB), exist_ok=True)
    conn = sqlite3.connect(CHAIN_DB)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS reply_chains (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT NOT NULL,
                seed_text TEXT NOT NULL,
                reply_text TEXT NOT NULL,
                seed_user_id TEXT,
                reply_user_id TEXT,
                quality INTEGER NOT NULL DEFAULT 1,
                use_count INTEGER NOT NULL DEFAULT 1,
                created_at INTEGER NOT NULL,
                last_used_at INTEGER NOT NULL
            )
            """
        )
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_chain_pair ON reply_chains(group_id, seed_text, reply_text)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_chain_seed ON reply_chains(group_id, seed_text, quality DESC)")
        conn.commit()
    finally:
        conn.close()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(CHAIN_DB, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _trim_pool(conn: sqlite3.Connection, adding: bool = False) -> None:
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM reply_chains")
    total = int(cur.fetchone()[0] or 0)
    overflow = total - REPLY_CHAIN_POOL_LIMIT
    if adding and total >= REPLY_CHAIN_POOL_LIMIT:
        overflow += 1
    if overflow <= 0:
        return

    cur.execute(
        """
        DELETE FROM reply_chains
        WHERE id IN (
            SELECT id FROM reply_chains
            ORDER BY RANDOM()
            LIMIT ?
        )
        """,
        (overflow + 1,),
    )


def record_reply_chain(
    group_id: str | None,
    seed_text: str,
    reply_text: str,
    *,
    seed_user_id: str | None = None,
    reply_user_id: str | None = None,
) -> None:
    group = _normalize_group_id(group_id)
    seed = _normalize_text(seed_text)[:MAX_TEXT_LENGTH]
    reply = _normalize_text(reply_text)[:MAX_TEXT_LENGTH]
    if not group or not seed or not reply:
        return
    if seed == reply:
        return

    q = _quality(seed, reply)
    if q < MIN_REPLY_QUALITY:
        return

    now = int(time.time())
    _ensure_db()
    with _DB_LOCK:
        conn = _connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT 1
                FROM reply_chains
                WHERE group_id = ? AND seed_text = ? AND reply_text = ?
                LIMIT 1
                """,
                (group, seed, reply),
            )
            is_new = cur.fetchone() is None

            _trim_pool(conn, adding=is_new)

            cur.execute(
                """
                INSERT INTO reply_chains (
                    group_id, seed_text, reply_text, seed_user_id, reply_user_id,
                    quality, use_count, created_at, last_used_at
                ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(group_id, seed_text, reply_text) DO UPDATE SET
                    quality = MAX(reply_chains.quality, excluded.quality),
                    use_count = reply_chains.use_count + 1,
                    reply_user_id = excluded.reply_user_id,
                    last_used_at = excluded.last_used_at
                """,
                (
                    group,
                    seed,
                    reply,
                    _normalize_text(seed_user_id) or None,
                    _normalize_text(reply_user_id) or None,
                    q,
                    now,
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()


def _build_seed_patterns(seed: str) -> list[str]:
    tokens = [t for t in _WORD_RE.findall(seed) if t]
    if not tokens:
        tokens = [seed]
    if len(seed) <= 16:
        tokens.append(seed)
    return [f"%{_normalize_text(t)}%" for t in tokens[:12]]


def _query_candidates(cur: sqlite3.Cursor, group_id: str | None, seed: str, min_quality: int, limit: int = 20) -> list[sqlite3.Row]:
    norm_seed = _normalize_text(seed)
    if not norm_seed:
        return []
    patterns = _build_seed_patterns(norm_seed)
    if not patterns:
        return []

    params: list[object] = [min_quality]
    where_group = []
    if group_id is not None:
        where_group.append("group_id = ?")
        params.append(_normalize_group_id(group_id))
    where_patterns = []
    for item in patterns:
        where_patterns.append("seed_text LIKE ?")
        params.append(item)

    where = "WHERE quality >= ?"
    if where_group:
        where += " AND (" + " OR ".join(where_group) + ")"
    where += " AND (" + " OR ".join(where_patterns) + ")"

    cur.execute(
        f"""
        SELECT id, group_id, seed_text, reply_text, quality, use_count
        FROM reply_chains
        {where}
        ORDER BY (use_count * 2 + quality) DESC, quality DESC, RANDOM()
        LIMIT {max(1, int(limit))}
        """,
        tuple(params),
    )
    return cur.fetchall()


def _sample_reply(candidates: list[sqlite3.Row]) -> tuple[str | None, int | None]:
    if not candidates:
        return None, None
    weighted: list[tuple[int, str]] = []
    for row in candidates:
        use_count = int(row["use_count"] or 0)
        quality = int(row["quality"] or 0)
        weight = max(1, (use_count + 1) * max(1, quality))
        weighted.append((weight, str(row["reply_text"]), int(row["id"]) if row["id"] is not None else 0))
    options = [r[1] for r in weighted]
    weights = [r[0] for r in weighted]
    choice = random.choices(list(range(len(weighted))), weights=weights, k=1)[0]
    selected = weighted[choice]
    return selected[1], selected[2]


def get_reply_chain(group_id: str | None, seed: str | None, *, min_quality: int = MIN_REPLY_QUALITY) -> str | None:
    if not seed:
        return None
    norm_seed = _normalize_text(seed)
    if not norm_seed or len(norm_seed) < 2:
        return None
    min_quality = max(0, int(min_quality))

    _ensure_db()
    conn = _connect()
    try:
        cur = conn.cursor()

        group_scope = _normalize_group_id(group_id) if group_id else None
        for target_group in (group_scope, None):
            candidates = _query_candidates(cur, target_group, norm_seed, min_quality, limit=24)
            if candidates:
                chosen, chosen_id = _sample_reply(candidates)
                if chosen and chosen_id is not None:
                    cur.execute(
                        """
                        UPDATE reply_chains
                        SET use_count = use_count + 1, last_used_at = ?
                        WHERE id = (
                            SELECT id FROM reply_chains WHERE id = ?
                        )
                        """,
                        (int(time.time()), int(chosen_id)),
                    )
                    conn.commit()
                    return chosen

        # fallback: any group, any seed as broad match
        cur.execute(
            """
            SELECT id, reply_text, quality, use_count
            FROM reply_chains
            WHERE quality >= ?
            ORDER BY RANDOM()
            LIMIT 20
            """,
            (min_quality,),
        )
        fallback = cur.fetchall()
        chosen, chosen_id = _sample_reply(fallback)
        if chosen and chosen_id is not None:
            cur.execute(
                """
                UPDATE reply_chains
                SET use_count = use_count + 1, last_used_at = ?
                WHERE id = ?
                """,
                (int(time.time()), int(chosen_id)),
            )
            conn.commit()
            return chosen
    finally:
        conn.close()
    return None
