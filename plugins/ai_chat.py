"""QQ group chat AI plugin."""

import random
import asyncio
import time
import re
from difflib import SequenceMatcher

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
from services.group_quotes import add_quote
from services.mood import get as get_mood, update as update_mood
from services.nya_personality import learn_catchphrase, record_chat as nya_record
from services.persona_guard import check as persona_check
from services.profile_updater import ProfileUpdater
from services.proactive import record_member
from services.event_system import on_group_rename, on_user_rename
from plugins.setu import SETU_COMMANDS
from services.sticker import (
    collect_from_event,
    detect as sticker_detect,
    face_to_text,
    reply_to_sticker,
    reply_with_sticker,
)
from services.utils import clean_text


chat = on_message(priority=10)

DEFAULT_REPLY_PROB = 0.2
SILENT_DURATION_SECONDS = 12 * 60 * 60
MUTE_CMD = "牛牛喵闭嘴！"
UNMUTE_CMD = "牛牛喵归来！"
_BOT_API_TIMEOUT = 8.0
CALL_KEYWORDS = ("牛牛喵", "猫猫", "喵", "meow", "@牛牛喵")
TYPING_HABIT_PROBABILITY = 0.45
TYPING_GAP_TOKENS = ("...", "???", "……")

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


def _compact_for_repeat_check(text: str) -> str:
    normalized = clean_text(_normalize_text(text)).lower()
    if not normalized:
        return ""
    return re.sub(r"[^\w\u4e00-\u9fff]", "", normalized)


def _is_repetitive_reply(seed: str, reply: str, *, ratio_threshold: float = 0.9) -> bool:
    source = _compact_for_repeat_check(seed)
    target = _compact_for_repeat_check(reply)
    if not source or not target:
        return False
    if source == target:
        return True
    if len(source) < 6 or len(target) < 6:
        return False
    if abs(len(source) - len(target)) > max(2, len(source) // 3):
        return False
    return SequenceMatcher(None, source, target).ratio() >= ratio_threshold


async def _build_no_token_reply(
    *,
    group_id: str | None,
    user_id: str,
    seed: str,
    mentioned_ids: list[str] | None,
    mood_state: dict | None,
    disable_rich_text: bool = False,
    quote_only: bool = False,
) -> str | None:
    for _ in range(3):
        for target_group in (group_id, None):
            for seed_hint in (seed, None):
                reply = build_no_token_reply(
                    group_id=target_group,
                    user_id=user_id,
                    mentioned_ids=mentioned_ids,
                    seed=(seed_hint or None),
                    mood_state=mood_state,
                    use_rich_text=not disable_rich_text,
                    only_quotes=quote_only,
                )
                if reply and not _is_repetitive_reply(seed, reply):
                    return reply
            if target_group is None:
                break
            reply = build_no_token_reply(
                group_id=None,
                user_id=user_id,
                mentioned_ids=mentioned_ids,
                seed=seed,
                mood_state=mood_state,
                use_rich_text=not disable_rich_text,
                only_quotes=quote_only,
            )
            if reply and not _is_repetitive_reply(seed, reply):
                return reply

    return None


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

async def _safe_ask(
    text,
    user_id,
    group_id,
    user_name,
    mentioned_ids,
    mood,
    max_try: int = 2,
) -> tuple[str, bool]:
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
            offline = await _build_no_token_reply(
                group_id=group_id,
                user_id=user_id,
                seed=text,
                mentioned_ids=mentioned_ids,
                mood_state=mood,
                disable_rich_text=True,
                quote_only=True,
            )
            if offline:
                return offline, True

            fallback = _fallback_reply()
            if _is_repetitive_reply(text, fallback):
                return f"{fallback} 😹", True
            return fallback, True

        reply = _normalize_text(reply)

        for _ in range(max_try):
            try:
                retry_needed = persona_check(reply)
                repetitive = _is_repetitive_reply(text, reply)
            except Exception as e:
                print(f"[persona] check error: {type(e).__name__}: {e}")
                break

            if not retry_needed and not repetitive:
                break

            if repetitive:
                print(f"[reply] retry: repetitive content for seed={text[:30]!r}")
                hint = "请用不同的话回应，不要重复用户原文。"
            else:
                print(f"[persona] retry: {retry_needed}")
                hint = PERSONA_RETRY_HINT

            reply = await ask_ai(
                message=f"{text}\n\n{hint}",
                user_id=user_id,
                group_id=group_id,
                sender_name=user_name,
                system_hint=SYSTEM_HINT,
                mentioned_ids=mentioned_ids,
                mood_state=mood,
            )
            reply = _normalize_text(reply)
            if not _is_repetitive_reply(text, reply):
                break

        if _is_repetitive_reply(text, reply or ""):
            fallback = _fallback_reply()
            if _is_repetitive_reply(text, fallback):
                return f"{fallback} 😹", False
            return fallback, False

        return (reply or _fallback_reply()), False
    except Exception as e:
        print(f"[AI] ask failed: {type(e).__name__}: {e}")
        return _fallback_reply(), False


async def _resolve_group_name(group_id: str) -> str:
    cached = ""
    try:
        cached = get_group_name(group_id)
        if cached:
            pass
    except Exception:
        pass

    try:
        bot = get_bot()
        info = await asyncio.wait_for(
            bot.call_api("get_group_info", group_id=int(group_id)),
            timeout=_BOT_API_TIMEOUT,
        )
        if isinstance(info, dict):
            name = str(info.get("group_name") or info.get("groupName") or "").strip()
            if name:
                from services.data_store import ensure_group_meta
                if cached and cached != name:
                    on_group_rename(group_id, cached, name)
                await ensure_group_meta(group_id, name)
                return name
            if cached:
                return cached
    except Exception:
        pass

    if cached:
        return cached

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
            await asyncio.wait_for(
                send_voice_reply(int(group_id), text),
                timeout=_BOT_API_TIMEOUT,
            )
            return
        except Exception as e:
            print(f"[chat] tts send failed, fallback text, group={group_id}, error={type(e).__name__}: {e}")

    try:
        await asyncio.wait_for(
            get_bot().send_group_msg(group_id=int(group_id), message=text),
            timeout=_BOT_API_TIMEOUT,
        )
    except ActionFailed as e:
        print(f"[chat] send text failed (ActionFailed), group={group_id}, message={e}")
    except Exception as e:
        print(f"[chat] send text failed, group={group_id}, error={type(e).__name__}: {e}")


def _build_typing_chunks(text: str) -> list[str]:
    text = _normalize_text(text)
    if not text:
        return []

    if len(text) <= 12:
        return [text]

    if random.random() > TYPING_HABIT_PROBABILITY:
        return [text]

    stop_chars = "。！？!?;；:：、，\n"
    chunks: list[str] = []
    current: list[str] = []
    for ch in text:
        current.append(ch)
        if ch in stop_chars and len(current) >= 4 and random.random() < 0.9:
            chunk = "".join(current).strip()
            if chunk:
                chunks.append(chunk)
            current = []
            if random.random() < 0.4:
                chunks.append(random.choice(TYPING_GAP_TOKENS))

    tail = "".join(current).strip()
    if tail:
        chunks.append(tail)

    if len(chunks) <= 1 and len(text) > 24:
        cut = max(4, len(text) // 2)
        fragments = [
            f"{text[:cut]} {random.choice(TYPING_GAP_TOKENS)}",
            text[cut:],
        ]
        chunks = [frag.strip() for frag in fragments if frag.strip()]

    chunks = [frag for frag in chunks if _normalize_text(frag)]
    return chunks[:4] or [text]


async def _send_reply(group_id: str, text: str, event, *, typing_habit: bool = False, auto_sticker: bool = True):
    st_info = sticker_detect(event)
    if st_info.get("has_sticker") and not event.get_plaintext().strip():
        sticker_seg = reply_to_sticker(event)
        if sticker_seg:
            try:
                await asyncio.wait_for(
                    get_bot().send_group_msg(group_id=int(group_id), message=str(sticker_seg)),
                    timeout=_BOT_API_TIMEOUT,
                )
            except ActionFailed as e:
                print(f"[chat] send sticker failed (ActionFailed), group={group_id}, message={e}")
                await _send_text_or_voice(group_id, text)
            except Exception as e:
                print(f"[chat] send sticker failed, group={group_id}, error={type(e).__name__}: {e}")
                await _send_text_or_voice(group_id, text)
            return

    if typing_habit and not group_voice_mode.get(group_id, False) and random.random() < TYPING_HABIT_PROBABILITY:
        chunks = _build_typing_chunks(text)
        if len(chunks) > 1:
            for i, chunk in enumerate(chunks):
                try:
                    await asyncio.wait_for(
                        get_bot().send_group_msg(group_id=int(group_id), message=chunk),
                        timeout=_BOT_API_TIMEOUT,
                    )
                except ActionFailed as e:
                    print(f"[chat] send split reply failed (ActionFailed), group={group_id}, message={e}")
                    break
                except Exception as e:
                    print(f"[chat] send split reply failed, group={group_id}, error={type(e).__name__}: {e}")
                    break

                if i < len(chunks) - 1:
                    await asyncio.sleep(random.uniform(0.4, 1.2))
            else:
                return

            # fallback to last attempt for full text
            await _send_text_or_voice(group_id, text)
            return

    if auto_sticker and random.random() < 0.5:
        sticker_segment, _ = reply_with_sticker()
        if sticker_segment and not group_voice_mode.get(group_id, False):
            full_msg = MessageSegment.text(text) + sticker_segment
            try:
                await asyncio.wait_for(
                    get_bot().send_group_msg(group_id=int(group_id), message=full_msg),
                    timeout=_BOT_API_TIMEOUT,
                )
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
    if user_profile:
        real_name = _normalize_text(event.sender.card) or _normalize_text(event.sender.nickname)
        if real_name and real_name != user_profile.get("name"):
            on_user_rename(group_id, user_id, user_profile.get("name", ""), real_name)
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
        reply, used_no_token = await _safe_ask(text, user_id, group_id, user_name, mentioned_ids, mood)
        nya_record(text, mentioned_ids)
        learn_catchphrase(reply)
        await _send_reply(
            group_id,
            reply,
            event,
            typing_habit=False if used_no_token else True,
            auto_sticker=not used_no_token,
        )
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
        reply, used_no_token = await _safe_ask(text, user_id, group_id, user_name, mentioned_ids, mood)
        learn_catchphrase(reply)
        await _send_reply(
            group_id,
            reply,
            event,
            typing_habit=False if used_no_token else True,
            auto_sticker=not used_no_token,
        )
        await chat.finish()
    else:
        print(f"[chat] skip reply: group={group_id}, user={user_id}, force={force_reply}, at={bot_at_mentioned}, followup={followup_left}, text={text[:60]!r}")
