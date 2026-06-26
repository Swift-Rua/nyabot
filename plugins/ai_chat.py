"""QQ group chat AI plugin."""

import random
import time
import re

from nonebot import get_bot, on_message
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageSegment
from nonebot.adapters.onebot.v11.exception import ActionFailed

from plugins.summon import (
    SUMMON_COMMANDS,
    check_auto,
    group_last_active,
    group_state,
    group_voice_mode,
)
from services.ai_gate import AIGate
from services.context_compressor import record_message
from services.data_store import get_group_name, get_user_sync
from services.deepseek_client import _fallback_reply as _deep_fallback_reply
from services.deepseek_client import ask_ai, was_last_ai_call_no_tokens
from services.group_reply_policy import get_group_default_reply_prob
from services.offline_responder import build_no_token_reply
from services.mention_resolver import parse_at_from_event, resolve_mentions
from services.group_quotes import add_quote, get_random_quote
from services.mood import get as get_mood, update as update_mood
from services.nya_personality import learn_catchphrase, record_chat as nya_record
from services.persona_guard import check as persona_check
from services.profile_updater import ProfileUpdater
from services.proactive import record_member
from plugins.setu import SETU_COMMANDS
from services.sticker import (
    collect_from_event,
    detect as sticker_detect,
    face_to_text,
    reply_to_sticker,
    reply_with_sticker,
)


chat = on_message(priority=10)

DEFAULT_REPLY_PROB = 0.2
SILENT_DURATION_SECONDS = 12 * 60 * 60
MUTE_CMD = "牛牛喵闭嘴！"
UNMUTE_CMD = "牛牛喵归来！"
CALL_KEYWORDS = ("牛牛喵", "猫猫", "喵", "meow", "@牛牛喵")

SYSTEM_HINT = (
    "You are a member in a QQ group, not an AI assistant.",
    "Keep replies casual, playful and human-like.",
)
PERSONA_RETRY_HINT = "You are not AI. Reply again as a normal group friend, do not mention AI."

gate = AIGate(cooldown=5.0, reply_prob=DEFAULT_REPLY_PROB)
updater = ProfileUpdater()

_GROUP_MUTE_UNTIL: dict[str, float] = {}
_FOLLOWUP_REPLY_LEFT: dict[str, int] = {}


def _fallback_reply() -> str:
    return _deep_fallback_reply()


def _normalize_text(value: object) -> str:
    return "" if value is None else str(value).strip()


def _get_name(user_id: str) -> str:
    user = get_user_sync(user_id)
    if user:
        return _normalize_text(user.get("name")) or f"User{user_id}"
    return f"User{user_id}"


def _is_group_muted(group_id: str) -> bool:
    until = _GROUP_MUTE_UNTIL.get(group_id)
    if not until:
        return False
    if time.time() >= until:
        _GROUP_MUTE_UNTIL.pop(group_id, None)
        return False
    return True


def _has_call_keyword(text: str) -> bool:
    if not text:
        return False
    compact = re.sub(r"\s+", "", str(text)).lower()
    for w in CALL_KEYWORDS:
        kw = str(w).lower()
        if kw and kw in compact:
            return True
    return False

async def _safe_ask(text, user_id, group_id, user_name, mentioned_ids, mood, max_try=2):
    try:
        reply = await ask_ai(
            message=text,
            user_id=user_id,
            group_id=group_id,
            sender_name=user_name,
            system_hint=SYSTEM_HINT,
            mentioned_ids=mentioned_ids,
            mood_state=mood,
        )
        if was_last_ai_call_no_tokens():
            offline = build_no_token_reply(
                group_id=group_id,
                user_id=user_id,
                mentioned_ids=mentioned_ids,
                seed=text,
                mood_state=mood,
            )
            if not offline:
                offline = build_no_token_reply(
                    group_id=None,
                    user_id=user_id,
                    mentioned_ids=mentioned_ids,
                    seed=text,
                    mood_state=mood,
                )
            if offline:
                return offline

            quote = await get_random_quote()
            if quote:
                return quote
            return _fallback_reply()

        reply = _normalize_text(reply)

        for _ in range(max_try):
            try:
                retry_needed = persona_check(reply)
            except Exception as e:
                print(f"[persona] check error: {type(e).__name__}: {e}")
                break

            if not retry_needed:
                break

            print(f"[persona] retry: {retry_needed}")
            reply = await ask_ai(
                message=f"{text}\n\n{PERSONA_RETRY_HINT}",
                user_id=user_id,
                group_id=group_id,
                sender_name=user_name,
                system_hint=SYSTEM_HINT,
                mentioned_ids=mentioned_ids,
                mood_state=mood,
            )
            reply = _normalize_text(reply)

        return reply or _fallback_reply()
    except Exception as e:
        print(f"[AI] ask failed: {type(e).__name__}: {e}")
        return _fallback_reply()


async def _resolve_group_name(group_id: str) -> str:
    try:
        cached = get_group_name(group_id)
        if cached:
            return cached
    except Exception:
        pass

    try:
        bot = get_bot()
        info = await bot.call_api("get_group_info", group_id=int(group_id))
        if isinstance(info, dict):
            name = str(info.get("group_name") or info.get("groupName") or "").strip()
            if name:
                from services.data_store import ensure_group_meta

                await ensure_group_meta(group_id, name)
                return name
    except Exception:
        pass

    return ""


def _extract_reply_to(event: GroupMessageEvent) -> str | None:
    reply_obj = getattr(event, "reply", None)
    if isinstance(reply_obj, dict):
        return _normalize_text(reply_obj.get("message_id"))
    if reply_obj is None:
        return None
    try:
        if _normalize_text(getattr(reply_obj, "message_id", "")):
            return _normalize_text(reply_obj.message_id)
    except Exception:
        pass
    return None


def _extract_reply_user(event: GroupMessageEvent) -> str | None:
    reply_obj = getattr(event, "reply", None)
    if isinstance(reply_obj, dict):
        uid = str(reply_obj.get("user_id") or "").strip()
        if uid:
            return uid
    if reply_obj is None:
        return None
    try:
        uid = str(getattr(reply_obj, "user_id", "")).strip()
        if uid:
            return uid
    except Exception:
        pass
    return None


async def _send_text_or_voice(group_id: str, text: str):
    from services.tts import send_voice_reply

    if group_voice_mode.get(group_id, False):
        try:
            await send_voice_reply(int(group_id), text)
            return
        except Exception as e:
            print(f"[chat] tts send failed, fallback text, group={group_id}, error={type(e).__name__}: {e}")

    try:
        await get_bot().send_group_msg(group_id=int(group_id), message=text)
    except ActionFailed as e:
        print(f"[chat] send text failed (ActionFailed), group={group_id}, message={e}")
    except Exception as e:
        print(f"[chat] send text failed, group={group_id}, error={type(e).__name__}: {e}")


async def _send_reply(group_id: str, text: str, event):
    st_info = sticker_detect(event)
    if st_info.get("has_sticker") and not event.get_plaintext().strip():
        sticker_seg = reply_to_sticker(event)
        if sticker_seg:
            try:
                await get_bot().send_group_msg(group_id=int(group_id), message=str(sticker_seg))
            except ActionFailed as e:
                print(f"[chat] send sticker failed (ActionFailed), group={group_id}, message={e}")
                await _send_text_or_voice(group_id, text)
            except Exception as e:
                print(f"[chat] send sticker failed, group={group_id}, error={type(e).__name__}: {e}")
                await _send_text_or_voice(group_id, text)
            return

    if random.random() < 0.5:
        sticker_segment, _ = reply_with_sticker()
        if sticker_segment and not group_voice_mode.get(group_id, False):
            full_msg = MessageSegment.text(text) + sticker_segment
            try:
                await get_bot().send_group_msg(group_id=int(group_id), message=full_msg)
            except ActionFailed as e:
                print(f"[chat] send sticker+text failed (ActionFailed), group={group_id}, message={e}")
                await _send_text_or_voice(group_id, text)
            except Exception as e:
                print(f"[chat] send sticker+text failed, group={group_id}, error={type(e).__name__}: {e}")
                await _send_text_or_voice(group_id, text)
            return

    await _send_text_or_voice(group_id, text)


def _is_control_command(text: str) -> bool:
    if not text:
        return False
    t = text.strip()
    tl = t.lower()
    if tl in {
        "bot start",
        "bot stop",
        "bot voice on",
        "bot voice off",
        "image",
        "photo",
        "sticker",
    }:
        return True
    if t in {MUTE_CMD, UNMUTE_CMD}:
        return True
    return t in SUMMON_COMMANDS or t in SETU_COMMANDS


def _is_bot_mentioned(event, at_ids: list[str], text: str) -> bool:
    self_id = str(event.self_id)
    if self_id in at_ids:
        return True

    raw_message = _normalize_text(getattr(event, "raw_message", "")).replace("＠", "@")
    if f"[CQ:at,qq={self_id}]" in raw_message:
        return True

    compact = re.sub(r"\s+", "", text).replace("＠", "@")
    return (f"@{self_id}" in compact) or ("@牛牛喵" in compact)

@chat.handle()
async def _(event: GroupMessageEvent):
    group_id = str(event.group_id)
    user_id = str(event.user_id)

    check_auto(group_id)

    from services.data_store import get_user_sync as _gsu, update_user as _dsu

    user_profile = _gsu(user_id)
    if user_profile and user_profile.get("name", "").startswith("User"):
        real_name = _normalize_text(event.sender.card) or _normalize_text(event.sender.nickname)
        if real_name and real_name != user_profile["name"]:
            await _dsu(
                user_id,
                {
                    "name": real_name,
                    "aliases": updater.generate_aliases(real_name),
                },
            )

    at_ids, display_text = parse_at_from_event(event)
    text = _normalize_text(display_text or event.get_plaintext())
    raw_text = text
    user_name = _get_name(user_id)

    sticker_info = sticker_detect(event)
    await collect_from_event(event, user_name)
    if sticker_info.get("faces"):
        face_desc = face_to_text(sticker_info["faces"])
        text = f"{text} [faces: {face_desc}]" if text else f"[faces: {face_desc}]"

    await _dsu(user_id, {"meta": {"last_seen": time.strftime("%m-%d %H:%M")}})
    from services.utils import is_noise

    if not is_noise(text):
        group_name = await _resolve_group_name(group_id)
        await record_member(group_id, user_id, group_name or getattr(event, "group_name", None))
        await updater.ensure_user(user_id)
        await updater.auto_profile(user_id, text)
    if not is_noise(raw_text) and not _is_control_command(raw_text):
        await add_quote(group_id, user_id, user_name, raw_text)

    has_image = bool(sticker_info.get("images"))
    has_face = bool(sticker_info.get("faces"))
    record_message(
        group_id,
        user_name,
        text,
        user_id=user_id,
        message_id=getattr(event, "message_id", None),
        reply_to=_extract_reply_to(event),
        at_list=at_ids,
        message_type=getattr(event, "message_type", "group"),
        has_image=has_image,
        has_face=has_face,
        face_ids=sticker_info.get("faces"),
    )
    gate.set_group_default_prob(group_id, get_group_default_reply_prob(group_id, DEFAULT_REPLY_PROB))

    if _is_control_command(text):
        return

    if text == "bot mute 12h":
        _GROUP_MUTE_UNTIL[group_id] = time.time() + SILENT_DURATION_SECONDS
        _FOLLOWUP_REPLY_LEFT.pop(group_id, None)
        await chat.finish("Got it. I will stay quiet for 12h.")

    if text == MUTE_CMD:
        _GROUP_MUTE_UNTIL[group_id] = time.time() + SILENT_DURATION_SECONDS
        _FOLLOWUP_REPLY_LEFT.pop(group_id, None)
        await chat.finish("好，我会安静12小时喵，不会主动说话。")

    if text == UNMUTE_CMD:
        _GROUP_MUTE_UNTIL.pop(group_id, None)
        _FOLLOWUP_REPLY_LEFT.pop(group_id, None)
        await chat.finish("喵喵回来了，解除12小时静音。")

    if text == "bot reply 10%":
        gate.set_group_reply_prob(group_id, 0.10)
        await chat.finish("Reply probability set to 10%.")

    if text == "bot reply default":
        gate.reset_group_reply_prob(group_id)
        _GROUP_MUTE_UNTIL.pop(group_id, None)
        await chat.finish("Reply probability restored to default.")

    mentioned_ids = list(set(at_ids + resolve_mentions(text)))
    reply_user = _extract_reply_user(event)
    relation_targets = set(mentioned_ids)
    if reply_user:
        relation_targets.add(reply_user)
    if relation_targets:
        from services.relation import record as record_relation
        await record_relation(user_id, list(relation_targets), group_id, replied_user_id=reply_user)

    if group_state.get(group_id, False):
        group_last_active[group_id] = time.time()
        update_mood(text)
        mood = get_mood()
        reply = await _safe_ask(text, user_id, group_id, user_name, mentioned_ids, mood)
        nya_record(text, mentioned_ids)
        learn_catchphrase(reply)
        await _send_reply(group_id, reply, event)
        await chat.finish()

    bot_at_mentioned = _is_bot_mentioned(event, at_ids, text)
    force_reply = _has_call_keyword(text) or ("meow" in text.lower()) or bot_at_mentioned

    if _is_group_muted(group_id) and not force_reply:
        return

    followup_left = _FOLLOWUP_REPLY_LEFT.get(group_id, 0)
    should_reply = force_reply or followup_left > 0
    if not should_reply:
        should_reply = gate.should_reply(group_id, user_id, text, force_reply=False)

    if should_reply:
        if force_reply:
            _FOLLOWUP_REPLY_LEFT[group_id] = 1
        elif followup_left > 0:
            if followup_left <= 1:
                _FOLLOWUP_REPLY_LEFT.pop(group_id, None)
            else:
                _FOLLOWUP_REPLY_LEFT[group_id] = followup_left - 1

        update_mood(text)
        mood = get_mood()
        nya_record(text, mentioned_ids)
        reply = await _safe_ask(text, user_id, group_id, user_name, mentioned_ids, mood)
        learn_catchphrase(reply)
        await _send_reply(group_id, reply, event)
        await chat.finish()
    else:
        print(f"[chat] skip reply: group={group_id}, user={user_id}, force={force_reply}, at={bot_at_mentioned}, followup={followup_left}, text={text[:60]!r}")
