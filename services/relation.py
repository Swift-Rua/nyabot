"""
关系系统 2.0 — 互动追踪 + 亲密度管理 + 自动衰减。
每次群成员互动时调用 record()，后台定期衰减。
"""
from services.data_store import get_users_sync, record_interaction, update_affinity


# ═══════════════════════════════════════════
# 互动记录
# ═══════════════════════════════════════════

async def record(speaker_id: str, mentioned_ids: list[str]):
    """
    记录一次互动：speaker 对每个被提及的人 interaction+1，affinity+1。
    同时更新 last_chat 时间。
    """
    if not mentioned_ids:
        return

    # interaction 计数 + 时间戳
    await record_interaction(speaker_id, mentioned_ids)

    # 亲密度小幅增长（每次互动 +1）
    for tid in mentioned_ids:
        if str(tid) != str(speaker_id):
            await update_affinity(speaker_id, tid, 1)


# ═══════════════════════════════════════════
# 亲密度衰减（后台定期调用）
# ═══════════════════════════════════════════

async def decay_all():
    """
    所有用户对很久没互动的人亲密度 -1。
    每次后台循环调用（约每 10 分钟一次较合理）。
    """
    users = get_users_sync()
    if not users:
        return

    for uid, profile in users.items():
        relations = profile.get("relations", {})
        for other_id, rel in relations.items():
            interaction = rel.get("interaction", 0)
            affinity = rel.get("affinity", 50)
            # 互动少的慢慢降亲密度（最低保留 20）
            if interaction < 5 and affinity > 20:
                await update_affinity(uid, other_id, -1)
