"""
关系图数据库：
- 记录群内提及关系
- 维持 interaction / affinity
- 提供关系查询 API（群内Top关系/双向关系）
- 与 members.json 的 relations 字段兼容更新
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time

from services.data_store import get_users_sync, record_interaction, update_affinity

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RELATION_DB = os.path.join(BASE_DIR, "data", "relation.db")
GLOBAL_GROUP = "global"

_DB_LOCK = threading.Lock()


def _normalize_user_id(user_id: object) -> str:
    return str(user_id).strip() if str(user_id).strip() else ""


def _normalize_group_id(group_id: object | None) -> str:
    g = str(group_id).strip() if group_id is not None and str(group_id).strip() else GLOBAL_GROUP
    return g or GLOBAL_GROUP


def _ensure_db() -> None:
    os.makedirs(os.path.dirname(RELATION_DB), exist_ok=True)
    conn = sqlite3.connect(RELATION_DB)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS relations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT NOT NULL,
                speaker_id TEXT NOT NULL,
                target_id TEXT NOT NULL,
                interaction INTEGER NOT NULL DEFAULT 0,
                affinity INTEGER NOT NULL DEFAULT 50,
                last_interaction REAL NOT NULL DEFAULT 0,
                UNIQUE(group_id, speaker_id, target_id)
            )
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_rel_group_speaker
            ON relations(group_id, speaker_id, interaction DESC)
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_rel_group_target
            ON relations(group_id, target_id, interaction DESC)
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_rel_group_last
            ON relations(group_id, last_interaction DESC)
            """
        )
        conn.commit()
    finally:
        conn.close()


def _connect():
    conn = sqlite3.connect(RELATION_DB, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _normalize_mentions(mentioned_ids: list[str] | None) -> list[str]:
    if not mentioned_ids:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for uid in mentioned_ids:
        u = _normalize_user_id(uid)
        if not u or u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


async def record(
    speaker_id: str,
    mentioned_ids: list[str],
    group_id: str | None = None,
    replied_user_id: str | None = None,
):
    """
    记录一次互动关系：
    1) 更新 members.json 的 relations（兼容旧逻辑）
    2) 更新 relation.db 的关系图谱（带群维度）
    """
    speaker = _normalize_user_id(speaker_id)
    if not speaker:
        return

    targets = _normalize_mentions(mentioned_ids)
    if replied_user_id:
        rid = _normalize_user_id(replied_user_id)
        if rid and rid != speaker and rid not in targets:
            targets.append(rid)
    if not targets:
        return

    now = time.time()
    await record_interaction(speaker, targets)

    # 兼容逻辑：更新旧成员关系亲密度
    for tid in targets:
        if tid != speaker:
            await update_affinity(speaker, tid, 1)

    _ensure_db()
    group = _normalize_group_id(group_id)
    with _DB_LOCK:
        conn = _connect()
        try:
            cur = conn.cursor()
            for target in targets:
                if target == speaker:
                    continue
                cur.execute(
                    """
                    INSERT INTO relations (
                        group_id, speaker_id, target_id, interaction, affinity, last_interaction
                    ) VALUES (?, ?, ?, 1, 1, ?)
                    ON CONFLICT(group_id, speaker_id, target_id) DO UPDATE SET
                        interaction = interaction + 1,
                        affinity = CASE
                            WHEN affinity >= 100 THEN 100
                            ELSE affinity + 1
                        END,
                        last_interaction = excluded.last_interaction
                    """,
                    (group, speaker, target, now),
                )
            conn.commit()
        finally:
            conn.close()


def get_outgoing_relations(
    speaker_id: str,
    group_id: str | None = None,
    limit: int = 20,
) -> list[dict]:
    speaker = _normalize_user_id(speaker_id)
    if not speaker:
        return []
    group = _normalize_group_id(group_id)
    limit = max(1, min(200, int(limit)))

    _ensure_db()
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT target_id, interaction, affinity, last_interaction
            FROM relations
            WHERE group_id = ? AND speaker_id = ?
            ORDER BY interaction DESC, affinity DESC, last_interaction DESC
            LIMIT ?
            """,
            (group, speaker, limit),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    out: list[dict] = []
    for row in rows:
        out.append(
            {
                "group_id": group,
                "target_id": str(row["target_id"]),
                "interaction": int(row["interaction"] or 0),
                "affinity": int(row["affinity"] or 50),
                "last_interaction": float(row["last_interaction"] or 0),
            }
        )
    return out


def get_incoming_relations(
    target_id: str,
    group_id: str | None = None,
    limit: int = 20,
) -> list[dict]:
    target = _normalize_user_id(target_id)
    if not target:
        return []
    group = _normalize_group_id(group_id)
    limit = max(1, min(200, int(limit)))

    _ensure_db()
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT speaker_id, interaction, affinity, last_interaction
            FROM relations
            WHERE group_id = ? AND target_id = ?
            ORDER BY interaction DESC, affinity DESC, last_interaction DESC
            LIMIT ?
            """,
            (group, target, limit),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    out: list[dict] = []
    for row in rows:
        out.append(
            {
                "group_id": group,
                "speaker_id": str(row["speaker_id"]),
                "interaction": int(row["interaction"] or 0),
                "affinity": int(row["affinity"] or 50),
                "last_interaction": float(row["last_interaction"] or 0),
            }
        )
    return out


def get_top_relations(group_id: str | None = None, limit: int = 20) -> list[dict]:
    """Top users by total outgoing interaction in a group."""
    group = _normalize_group_id(group_id)
    limit = max(1, min(200, int(limit)))

    _ensure_db()
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT speaker_id AS user_id,
                   SUM(interaction) AS total_interaction,
                   ROUND(AVG(affinity), 2) AS avg_affinity,
                   COUNT(target_id) AS connected_users,
                   MAX(last_interaction) AS last_interaction
            FROM relations
            WHERE group_id = ?
            GROUP BY speaker_id
            ORDER BY total_interaction DESC, avg_affinity DESC
            LIMIT ?
            """,
            (group, limit),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    out: list[dict] = []
    for row in rows:
        out.append(
            {
                "group_id": group,
                "user_id": str(row["user_id"]),
                "total_interaction": int(row["total_interaction"] or 0),
                "avg_affinity": float(row["avg_affinity"] or 50.0),
                "connected_users": int(row["connected_users"] or 0),
                "last_interaction": float(row["last_interaction"] or 0),
            }
        )
    return out


def get_top_friends(user_id: str, group_id: str | None = None, limit: int = 20) -> list[dict]:
    """Outgoing edges ranked by affinity first (best friends)."""
    rows = get_outgoing_relations(user_id, group_id=group_id, limit=limit)
    rows.sort(
        key=lambda r: (
            int(r.get("affinity", 0)),
            int(r.get("interaction", 0)),
            float(r.get("last_interaction", 0)),
            str(r.get("target_id", "")),
        ),
        reverse=True,
    )
    return rows


def get_top_rivals(user_id: str, group_id: str | None = None, limit: int = 20) -> list[dict]:
    """Outgoing edges with low affinity but high interaction (potential rivals)."""
    rows = get_outgoing_relations(user_id, group_id=group_id, limit=limit * 2)
    rows.sort(
        key=lambda r: (
            100 - int(r.get("affinity", 0)),
            int(r.get("interaction", 0)),
            str(r.get("target_id", "")),
        )
    )
    return rows[:limit]


async def decay_all():
    """
    衰减低互动边的 affinity，保持长期关系不过度膨胀。
    同时兼容 members.json 老数据衰减。
    """
    users = get_users_sync()
    if users:
        for uid, profile in users.items():
            relations = profile.get("relations", {})
            for other_id, rel in relations.items():
                interaction = rel.get("interaction", 0)
                affinity = rel.get("affinity", 50)
                if interaction < 5 and affinity > 20:
                    await update_affinity(uid, other_id, -1)

    _ensure_db()
    with _DB_LOCK:
        conn = _connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE relations
                SET affinity = CASE
                    WHEN affinity > 20 THEN affinity - 1
                    ELSE affinity
                END
                WHERE interaction < 5 AND affinity > 20
                """
            )
            conn.commit()
        finally:
            conn.close()
