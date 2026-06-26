"""Build world context for prompt generation."""

import time

from services.data_store import (
    get_core_members,
    get_group_members,
    get_group_name,
    get_users_sync,
)
from services.message_logger import get_member_profile
from services.relation import get_incoming_relations, get_outgoing_relations
from services.context_compressor import compress

WORLD_GROUP_MEMBER_CONTEXT_LIMIT = 60


def _format_member_profile(profile: dict | None) -> str:
    if not isinstance(profile, dict):
        return ""
    msg_count = int(profile.get("message_count", 0))
    total_len = int(profile.get("total_length", 0))
    image_count = int(profile.get("image_count", 0))
    face_count = int(profile.get("face_count", 0))
    mention_count = int(profile.get("mention_count", 0))
    first_seen = float(profile.get("first_seen", 0.0))
    last_seen = float(profile.get("last_seen", 0.0))

    lines: list[str] = []
    lines.append(f"Messages: {msg_count}")
    lines.append(f"Total length: {total_len}")
    lines.append(f"Images: {image_count}")
    lines.append(f"Faces: {face_count}")
    lines.append(f"Mentioned count: {mention_count}")
    if first_seen:
        lines.append(f"First seen: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(first_seen))}")
    if last_seen:
        lines.append(f"Last seen: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(last_seen))}")
    return " / ".join(lines)


def _format_relation_lines(user_id: str, group_id: str | None) -> str:
    if not group_id:
        return ""

    uid = str(user_id)
    users = get_users_sync()
    out: list[str] = []

    outgoing = get_outgoing_relations(uid, group_id=group_id, limit=5)
    for rel in outgoing:
        target_id = str(rel.get("target_id", ""))
        if not target_id:
            continue
        score = f"{rel.get('interaction', 0)}/{rel.get('affinity', 50)}"
        target_name = users.get(target_id, {}).get("name", target_id) if isinstance(users, dict) else target_id
        out.append(f"- 与 {target_name}({target_id}) 的互动更频繁，交互{score}")

    incoming = get_incoming_relations(uid, group_id=group_id, limit=5)
    for rel in incoming:
        src_id = str(rel.get("speaker_id", ""))
        if not src_id:
            continue
        score = f"{rel.get('interaction', 0)}/{rel.get('affinity', 50)}"
        src_name = users.get(src_id, {}).get("name", src_id) if isinstance(users, dict) else src_id
        out.append(f"- {src_name}({src_id}) 最近也比较常指向我，交互{score}")

    if not out:
        return ""
    return "\n## Relation hints\n" + "\n".join(out[:10])


def _fmt_tags(tags: dict) -> str:
    from services.deepseek_client import _fmt_tags as fmt

    return fmt(tags)


def _select_member_ids_for_context(
    group_member_ids: list[str],
    user_id: str,
    mentioned_ids: list[str] | None,
    limit: int,
    core_member_ids: list[str] | None = None,
) -> list[str]:
    if limit <= 0:
        return []

    seen: set[str] = set()
    ordered: list[str] = []

    base = [str(user_id)]
    if mentioned_ids:
        for mid in mentioned_ids:
            m = str(mid)
            if m and m not in base:
                base.append(m)

    core_member_ids = [str(uid) for uid in (core_member_ids or []) if str(uid)]
    for uid in base + core_member_ids + list(group_member_ids):
        uid = str(uid)
        if not uid or uid in seen:
            continue
        ordered.append(uid)
        seen.add(uid)
        if len(ordered) >= limit:
            break

    return ordered


def build(
    group_id: str | None,
    user_id: str,
    mentioned_ids: list[str] | None = None,
    mood_state: dict | None = None,
) -> str:
    members = get_users_sync()
    user_id = str(user_id)
    profile = members.get(user_id) if isinstance(members, dict) else None

    group_member_ids = get_group_members(
        group_id,
        max_members=WORLD_GROUP_MEMBER_CONTEXT_LIMIT,
    ) if group_id else []
    core_member_ids = get_core_members(group_id)
    member_ids_for_context = _select_member_ids_for_context(
        group_member_ids,
        user_id=user_id,
        mentioned_ids=mentioned_ids,
        limit=WORLD_GROUP_MEMBER_CONTEXT_LIMIT,
        core_member_ids=core_member_ids,
    )
    group_members: dict[str, dict] = {}
    for uid in member_ids_for_context:
        uid = str(uid)
        member_profile = members.get(uid)
        if isinstance(member_profile, dict):
            group_members[uid] = member_profile

    if group_member_ids:
        members_in_context = group_members
    else:
        members_in_context = {}
        if isinstance(profile, dict):
            members_in_context[user_id] = profile

    blocks: list[str] = []

    if isinstance(profile, dict):
        name = str(profile.get("name", "Unknown"))
        style = str(profile.get("style", ""))
        tags = _fmt_tags(profile.get("tags", {}))
        profile_stats = _format_member_profile(
            get_member_profile(group_id, user_id) if group_id else None
        )
        impression = str(profile.get("meta", {}).get("impression", "")).strip()
        block = (
            "## Current speaker\n"
            f"Name: {name} (ID:{user_id})\n"
            f"Tags: {tags}\n"
            f"Style: {style}"
        )
        if profile_stats:
            block += f"\nProfile: {profile_stats}"
        if impression:
            block += f"\nImpression: {impression}"
        blocks.append(block)
    else:
        blocks.append(f"Current speaker unknown (ID:{user_id})")

    if group_id:
        group_id = str(group_id)
        group_name = get_group_name(group_id)
        if group_name:
            blocks.append(f"Current group: {group_name} (ID:{group_id})")
        else:
            blocks.append(f"Current group ID: {group_id}")

        relation_block = _format_relation_lines(user_id=user_id, group_id=group_id)
        if relation_block:
            blocks.append(relation_block)

    if members_in_context:
        lines = ["## Group members for context"]
        for uid, p in members_in_context.items():
            if not isinstance(p, dict):
                continue
            name = str(p.get("name", uid))
            style = str(p.get("style", ""))
            tags = _fmt_tags(p.get("tags", {}))
            aliases = p.get("aliases", [])
            alias_part = ""
            if isinstance(aliases, list) and aliases:
                alias_part = " aliases=" + "/".join(str(a) for a in aliases if isinstance(a, str))
            lines.append(f"- {name} (ID:{uid}){alias_part} | Tags:{tags} | Style:{style}")
        blocks.append("\n".join(lines))

    if group_id:
        ctx = compress(group_id, max_items=20)
        if ctx:
            blocks.append(f"## Group recent context\n{ctx}")

    from services.nya_personality import build_self_block

    blocks.append(build_self_block())

    if isinstance(mood_state, dict):
        mood_lines = ["## Mood"]
        for key, label in [
            ("happy", "happy"),
            ("tired", "tired"),
            ("social", "social"),
            ("roast", "roast"),
            ("energy", "energy"),
        ]:
            val = int(mood_state.get(key, 50))
            emoji = _mood_emoji(key, val)
            mood_lines.append(f"- {label}: {emoji} {val}/100")
        mood_lines.append("- Mood summary: favor calm, clear, funny, and concise responses.")
        blocks.append("\n".join(mood_lines))

    from services.group_events import build_context as build_events

    event_block = build_events(days=3, group_id=group_id)
    if event_block:
        blocks.append(event_block)

    from services.memory import build_context as build_memory

    memory_block = build_memory(user_id=user_id, mentioned_ids=mentioned_ids)
    if memory_block:
        blocks.append(memory_block)

    if mentioned_ids:
        lines = ["## Mentioned users"]
        for mid in set(map(str, mentioned_ids)):
            mp = members.get(mid)
            if isinstance(mp, dict):
                rel = {}
                if isinstance(profile, dict):
                    rel = profile.get("relations", {}).get(mid, {}) if isinstance(profile.get("relations", {}), dict) else {}
                affinity = int(rel.get("affinity", 50)) if isinstance(rel, dict) else 50
                interaction = int(rel.get("interaction", 0)) if isinstance(rel, dict) else 0
                lines.append(
                    f"- {mp.get('name', mid)} (ID:{mid}) "
                    f"Tags:{_fmt_tags(mp.get('tags', {}))} "
                    f"Affinity:{affinity} Interaction:{interaction}"
                )
                impression = str(mp.get("meta", {}).get("impression", "")).strip()
                if impression:
                    lines.append(f"  Impression: {impression}")
            else:
                lines.append(f"- Unknown user (ID:{mid})")
        blocks.append("\n".join(lines))

    return "\n\n".join(blocks)


def _mood_emoji(key: str, val: int) -> str:
    if key == "happy":
        return ":-)" if val > 60 else ":-|" if val > 30 else ":-("
    if key == "tired":
        return "zZ" if val > 60 else "..." if val > 30 else "sleepy"
    if key == "social":
        return ":::)" if val > 60 else ":|:" if val > 30 else ":("
    if key == "roast":
        return "😏" if val > 60 else "😐" if val > 30 else "😠"
    if key == "energy":
        return "⚡" if val > 60 else "🙂" if val > 30 else "😴"
    return "?"
