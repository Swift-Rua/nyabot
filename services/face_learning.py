"""Learn and reuse QQ face ids from recorded message stats."""

import os
import random
import sqlite3

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HISTORY_DB = os.path.join(BASE_DIR, "data", "history.db")


def _normalize_group_id(group_id: object) -> str | None:
    if group_id is None:
        return None
    text = str(group_id).strip()
    return text if text else None


def _normalize_limit(value: object | None, default: int, max_value: int) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        limit = default
    if limit < 1:
        return 1
    return min(max_value, limit)


def get_face_statistics(group_id: str | None = None, limit: int = 120) -> list[tuple[int, int]]:
    if not os.path.exists(HISTORY_DB):
        return []

    gid = _normalize_group_id(group_id)
    limit = _normalize_limit(limit, 120, 300)

    conn = sqlite3.connect(HISTORY_DB)
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        if gid:
            cur.execute(
                """
                SELECT face_id, SUM(count) as total_count
                FROM member_face_stats
                WHERE group_id = ?
                GROUP BY face_id
                ORDER BY total_count DESC, face_id ASC
                LIMIT ?
                """,
                (gid, limit),
            )
        else:
            cur.execute(
                """
                SELECT face_id, SUM(count) as total_count
                FROM member_face_stats
                GROUP BY face_id
                ORDER BY total_count DESC, face_id ASC
                LIMIT ?
                """,
                (limit,),
            )

        rows = cur.fetchall()
    finally:
        conn.close()

    out: list[tuple[int, int]] = []
    for row in rows:
        try:
            fid = int(row["face_id"])
            count = int(row["total_count"])
        except (TypeError, ValueError, KeyError):
            continue
        if fid <= 0 or count <= 0:
            continue
        out.append((fid, count))
    return out


def get_random_face_token(group_id: str | None = None, *, limit: int = 120) -> str | None:
    entries = get_face_statistics(group_id=group_id, limit=limit)
    if not entries:
        return None

    ids = [int(fid) for fid, _ in entries]
    weights = [int(cnt) for _, cnt in entries]
    face_id = random.choices(ids, weights=weights, k=1)[0]
    return f"[CQ:face,id={face_id}]"

