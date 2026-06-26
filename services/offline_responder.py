"""Offline no-token response helpers."""

from __future__ import annotations

import json
import os
import random
import re
from difflib import SequenceMatcher

from services.message_logger import (
    fetch_recent_messages,
    get_member_profile,
    get_messages_by_keyword,
    get_message_pool,
    get_top_fillers,
    get_top_phrases,
    get_top_words,
    get_user_words,
)
from services.meme_bank import get_random_meme
from services.group_quotes import get_quotes as get_group_quotes
from services.reply_chain import get_reply_chain
from services.relation import get_top_friends, get_top_rivals
from services.data_store import get_users_sync
from services.face_learning import get_random_face_token
from services.mood_profile import get_context as get_mood_context
from services.time_profile import get_context as get_time_context
from services.utils import clean_text


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES_FILE = os.path.join(BASE_DIR, "data", "templates.json")
_DEFAULT_TEMPLATES = [
    "{speaker} 这段时间发言了 {active_days} 天，最近比较稳，发了 {message_count} 条消息，状态挺带感。",
    "{speaker} 刚刚提到了 {topic}，我猜你会觉得 {top_word} 这个词很有共鸣。",
    "{speaker} 这位朋友 {best_friend} 最近总能在话题上有很准的回应。",
    "{speaker} 按活跃时间看，{active_hour} 点大家都比较热闹，平均发言长度约 {average_length} 字。",
    "{speaker} 这个时间点常见主题是 {topic}，也许你会想接着说这句。",
    "{speaker} 最近常出现 {top_words}，你可以接个梗玩起来。",
    "{speaker} 这句里有点像 {top_word} 的感觉，能不能接一句更有趣的。"
]

_DEFAULT_STOP_TOPICS = {"这个", "那个", "然后", "就是", "好像", "到底", "你们", "而且", "不过"}

_MAX_MARKOV_STEPS = 3
_MARKOV_POOL = 500
_MARKOV_MAX_CHARS = 30
_MARKOV_MIN_CHARS = 5
_MARKOV_ORDER = 2
_MARKOV_ATTEMPTS = 8
_MAX_MARKOV_WORDS = 360

_HISTORY_QUOTE_PROBABILITY = 0.15
_HISTORY_POOL_SIZE = 500
_HISTORY_QUOTE_SAMPLE_SIZE = 30
_NO_TOKEN_FACE_PROBABILITY = 0.25
_NO_TOKEN_QUOTE_PARTS_MIN = 2
_NO_TOKEN_QUOTE_PARTS_MAX = 3
_NO_TOKEN_MAX_REPLY_CHARS = 90

_TOP_WORDS_FOR_CONTEXT = 3
_TOP_PHRASES_FOR_CONTEXT = 3
_TOP_FILLERS_FOR_CONTEXT = 3
_RICH_TEXT_PROB = 0.15

_MOOD_PREPEND = {
    "low": "有点困了，先来一句：",
    "medium": "顺势接话：",
    "high": "现在氛围很热闹，直接来句：",
}

_MOOD_APPEND = {
    "low": "先别急着回，慢慢想。",
    "medium": "你看这反应还行。",
    "high": "今天就靠这一句带节奏。",
}

def _load_templates() -> list[dict[str, object]]:
    if not os.path.exists(TEMPLATES_FILE):
        return [{"text": t} for t in _DEFAULT_TEMPLATES]

    try:
        with open(TEMPLATES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data = data.get("templates", [])
        if not isinstance(data, list):
            return [{"text": t} for t in _DEFAULT_TEMPLATES]

        out: list[dict[str, object]] = []
        for item in data:
            if isinstance(item, str):
                text = item.strip()
                if text:
                    out.append({"text": text})
                continue

            if not isinstance(item, dict):
                continue

            text = str(item.get("text") or item.get("template") or item.get("reply") or "").strip()
            if not text:
                continue
            out.append(
                {
                    "text": text,
                    "probability": item.get("probability", item.get("weight", 1)),
                    "conditions": item.get("conditions", item.get("condition", {})),
                }
            )
        if not out:
            return [{"text": t} for t in _DEFAULT_TEMPLATES]
        return out
    except Exception:
        return [{"text": t} for t in _DEFAULT_TEMPLATES]


def _safe_value(value: object, fallback: str = "") -> str:
    if value is None:
        return fallback
    s = str(value).strip()
    return s if s else fallback


def _normalize_template_probability(raw: object) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 1.0
    if value <= 0:
        return 0.0
    if value >= 1:
        return min(1.0, value)
    return value


def _normalize_keyword_list(raw: object) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple, set)):
        return []
    out: list[str] = []
    for item in raw:
        text = str(item).strip().lower()
        if text:
            out.append(text)
    return out


def _compact_for_repeat_check(text: str) -> str:
    raw = clean_text(text).lower()
    return re.sub(r"[^\w\u4e00-\u9fff]", "", raw)


def _is_redundant_reply(seed: str, reply: str, *, ratio_threshold: float = 0.9) -> bool:
    src = _compact_for_repeat_check(seed)
    dst = _compact_for_repeat_check(reply)
    if not src or not dst:
        return False
    if src == dst:
        return True
    if len(src) < 6 or len(dst) < 6:
        return False
    if abs(len(src) - len(dst)) > max(2, len(src) // 3):
        return False
    return SequenceMatcher(None, src, dst).ratio() >= ratio_threshold


def _contains_any(text: str, patterns: list[str]) -> bool:
    return any(p and p in text for p in patterns)


def _contains_all(text: str, patterns: list[str]) -> bool:
    return all(p and p in text for p in patterns)


def _template_matches(raw_template: dict[str, object], context: dict[str, str]) -> bool:
    conditions = raw_template.get("conditions")
    if not isinstance(conditions, dict):
        return True

    topic = _safe_value(context.get("topic")).lower()
    seed = _safe_value(context.get("seed")).lower()
    ctx_text = f"{topic} {seed} {_safe_value(context.get('top_word')).lower()} {_safe_value(context.get('top_phrase')).lower()} {_safe_value(context.get('top_filler')).lower()}".strip()

    contains = _normalize_keyword_list(conditions.get("contains"))
    if contains and not _contains_all(ctx_text, contains):
        return False

    contains_any = _normalize_keyword_list(conditions.get("contains_any"))
    if contains_any and not _contains_any(ctx_text, contains_any):
        return False

    not_contains = _normalize_keyword_list(conditions.get("not_contains"))
    if not_contains and _contains_any(ctx_text, not_contains):
        return False

    topic_contains = _normalize_keyword_list(conditions.get("topic_contains"))
    if topic_contains and not _contains_any(topic, topic_contains):
        return False

    seed_contains = _normalize_keyword_list(conditions.get("seed_contains"))
    if seed_contains and not _contains_all(seed, seed_contains):
        return False

    group_id = _safe_value(context.get("group_id")).strip()
    target_groups = _normalize_keyword_list(conditions.get("group_id"))
    if target_groups and group_id not in target_groups and str(group_id) not in target_groups:
        return False

    return True


def _select_template(context: dict[str, str]) -> str:
    templates = _load_templates()
    if not templates:
        return ""

    candidates: list[tuple[str, float]] = []
    fallback: list[str] = []
    for tpl in templates:
        text = _safe_value(tpl.get("text"))
        if not text:
            continue
        fallback.append(text)

        probability = _normalize_template_probability(tpl.get("probability"))
        if probability <= 0:
            continue
        if random.random() > probability:
            continue
        if not _template_matches(tpl, context):
            continue
        candidates.append((text, probability))

    if not candidates:
        candidates = [(text, 1.0) for text in fallback]

    if not candidates:
        return ""

    selected_text = random.choices([c[0] for c in candidates], weights=[c[1] for c in candidates], k=1)[0]

    class _SafeFormatDict(dict):
        def __missing__(self, key):
            return ""

    return str(selected_text).format_map(_SafeFormatDict(context))


def _extract_topic(profile: dict | None, recent_msgs: list[str]) -> str:
    if profile:
        topic = _safe_value(profile.get("most_common_word"), "")
        if topic and topic not in _DEFAULT_STOP_TOPICS:
            return topic

    for text in recent_msgs:
        if text:
            tokens = clean_text(text).split()
            for token in tokens:
                if len(token) >= 2 and token not in _DEFAULT_STOP_TOPICS:
                    return token
    return "缇よ亰"


def _build_top_words(group_id: str | None) -> tuple[str, str, str]:
    top_words = get_top_words(group_id=group_id, limit=_TOP_WORDS_FOR_CONTEXT)
    if not top_words:
        return "", "", ""

    token = _safe_value(top_words[0].get("token"))
    count = _safe_value(top_words[0].get("count"), "0")
    all_words = ", ".join([_safe_value(item.get("token")) for item in top_words[:_TOP_WORDS_FOR_CONTEXT] if _safe_value(item.get("token"))])
    return token, count, all_words


def _build_top_phrases(group_id: str | None) -> tuple[str, str, str]:
    top_phrases = get_top_phrases(group_id=group_id, limit=_TOP_PHRASES_FOR_CONTEXT)
    if not top_phrases:
        return "", "", ""

    phrase = _safe_value(top_phrases[0].get("token"))
    count = _safe_value(top_phrases[0].get("count"), "0")
    all_phrases = ", ".join([_safe_value(item.get("token")) for item in top_phrases if _safe_value(item.get("token"))])
    return phrase, count, all_phrases


def _build_top_fillers(group_id: str | None) -> tuple[str, str, str]:
    top_fillers = get_top_fillers(group_id=group_id, limit=_TOP_FILLERS_FOR_CONTEXT)
    if not top_fillers:
        return "", "", ""

    filler = _safe_value(top_fillers[0].get("token"))
    count = _safe_value(top_fillers[0].get("count"), "0")
    all_fillers = ", ".join([_safe_value(item.get("token")) for item in top_fillers if _safe_value(item.get("token"))])
    return filler, count, all_fillers


def _build_context(group_id: str | None, user_id: str, seed: str | None = None, mentioned_ids: list[str] | None = None) -> dict[str, str]:
    users = get_users_sync()
    profile = get_member_profile(group_id, user_id) if group_id else None
    friends = get_top_friends(user_id, group_id=group_id, limit=1)
    rivals = get_top_rivals(user_id, group_id=group_id, limit=1)

    top_word, top_word_count, top_words = _build_top_words(group_id)
    top_phrase, top_phrase_count, top_phrases = _build_top_phrases(group_id)
    top_filler, top_filler_count, top_fillers = _build_top_fillers(group_id)

    top_user_word = ""
    top_user_word_count = "0"
    top_user_words = ""
    if group_id:
        user_words = get_user_words(group_id, user_id, limit=_TOP_WORDS_FOR_CONTEXT)
        if user_words:
            top_user_word = _safe_value(user_words[0].get("token"))
            top_user_word_count = _safe_value(user_words[0].get("count"), "0")
            top_user_words = ", ".join(
                [_safe_value(item.get("token")) for item in user_words if _safe_value(item.get("token"))]
            )

    top_friend_id = friends[0].get("target_id") if friends else ""
    top_rival_id = rivals[0].get("target_id") if rivals else ""

    context = {
        "speaker": str(users.get(user_id, {}).get("name", user_id)),
        "sender_id": str(user_id),
        "group_id": _safe_value(group_id, "global"),
        "seed": _safe_value(seed),
        "active_days": _safe_value((profile or {}).get("active_days"), "0"),
        "streak_days": _safe_value((profile or {}).get("streak_days"), "0"),
        "message_count": _safe_value((profile or {}).get("message_count"), "0"),
        "average_length": _safe_value((profile or {}).get("average_length"), "0"),
        "active_hour": _safe_value((profile or {}).get("most_active_hour"), "12"),
        "best_friend": _safe_value(users.get(top_friend_id, {}).get("name") if isinstance(users, dict) else "", top_friend_id),
        "worst_friend": _safe_value(users.get(top_rival_id, {}).get("name") if isinstance(users, dict) else "", top_rival_id),
        "mentioned_count": str(len(mentioned_ids or [])),
        "mentioned": ", ".join(_safe_value(m) for m in (mentioned_ids or [])),
        "top_word": top_word,
        "top_word_count": top_word_count,
        "top_words": top_words,
        "top_phrase": top_phrase,
        "top_phrase_count": top_phrase_count,
        "top_phrases": top_phrases,
        "top_filler": top_filler,
        "top_filler_count": top_filler_count,
        "top_fillers": top_fillers,
        "user_top_word": top_user_word,
        "user_top_word_count": top_user_word_count,
        "user_top_words": top_user_words,
    }

    try:
        mood_ctx = get_mood_context(group_id=group_id, user_id=user_id)
        if isinstance(mood_ctx, dict):
            context.update({str(k): _safe_value(v) for k, v in mood_ctx.items()})
    except Exception:
        pass

    try:
        time_ctx = get_time_context(group_id=group_id, window_days=7)
        if isinstance(time_ctx, dict):
            context.update({str(k): _safe_value(v) for k, v in time_ctx.items()})
    except Exception:
        pass

    return context


def _collect_recent_messages(group_id: str | None) -> list[str]:
    if group_id:
        rows = get_message_pool(group_id=group_id, pool_size=_MARKOV_POOL)
    else:
        rows = fetch_recent_messages(group_id=None, limit=_MAX_MARKOV_WORDS)
    texts: list[str] = []
    for row in rows:
        text = _safe_value(row.get("message"), "").strip()
        if not text:
            continue
        texts.append(clean_text(text))
    return texts


def _build_markov_chain(messages: list[str], order: int) -> tuple[dict[str, list[str]], list[str]]:
    chain: dict[str, list[str]] = {}
    start_states: list[str] = []
    if order <= 0:
        order = 1

    for msg in messages:
        text = clean_text(msg).strip()
        if len(text) <= order:
            continue
        start_states.append(text[:order])
        for idx in range(len(text) - order):
            key = text[idx : idx + order]
            nxt = text[idx + order]
            chain.setdefault(key, []).append(nxt)
    return chain, start_states


def _pick_seed_state(seed: str | None, messages: list[str], chain: dict[str, list[str]], order: int) -> tuple[str | None, str]:
    if not chain:
        return None, ""
    seed_text = clean_text(seed or "").strip()
    if not seed_text:
        return random.choice(list(chain.keys())), ""

    if len(seed_text) >= order:
        suffix = seed_text[-order:]
        if suffix in chain:
            return suffix, seed_text

    if len(seed_text) > 1:
        first = seed_text[-1]
        candidates = [k for k in chain.keys() if first in k]
        if candidates:
            return random.choice(candidates), seed_text

    # exact match from historical messages for stronger seed grounding
    for msg in messages:
        if seed_text and seed_text in msg:
            start = msg.find(seed_text)
            if start == -1:
                continue
            next_pos = start + len(seed_text)
            if next_pos >= len(msg):
                continue
            state = msg[next_pos - order : next_pos]
            if len(state) == order and state in chain:
                return state, seed_text

    return random.choice(list(chain.keys())), seed_text


def _generate_markov_reply(
    chain: dict[str, list[str]],
    start_state: str,
    seed_text: str,
    *,
    order: int,
) -> str:
    if not chain or not start_state:
        return ""

    max_len = _MARKOV_MAX_CHARS
    seed_text = clean_text(seed_text).strip()
    if not seed_text:
        seed_text = start_state

    if len(seed_text) > max_len:
        return seed_text[:max_len]

    state = start_state[-order:] if len(start_state) >= order else start_state
    out = seed_text
    for _ in range(max_len - len(out)):
        options = chain.get(state)
        if not options:
            break
        nxt = random.choice(options)
        out += nxt
        state = (state + nxt)[-order:]
    return out


def build_markov_reply(group_id: str | None = None, seed: str | None = None) -> str | None:
    messages = _collect_recent_messages(group_id)
    if len(messages) < 2:
        return None

    chain, _ = _build_markov_chain(messages, _MARKOV_ORDER)
    if not chain:
        return None

    source_set = {clean_text(m).strip().lower() for m in messages if clean_text(m).strip()}

    for _ in range(_MARKOV_ATTEMPTS):
        start_state, out_seed = _pick_seed_state(seed, messages, chain, _MARKOV_ORDER)
        if not start_state:
            continue

        if out_seed:
            generated = _generate_markov_reply(chain, start_state, out_seed, order=_MARKOV_ORDER)
        else:
            generated = _generate_markov_reply(chain, start_state, "", order=_MARKOV_ORDER)

        text = clean_text(generated).strip()
        if len(text) < _MARKOV_MIN_CHARS or len(text) > _MARKOV_MAX_CHARS:
            continue
        if text.lower() in source_set:
            continue
        return text

    return None


def _build_topic_text(group_id: str | None, user_id: str, seed: str | None = None) -> str:
    recent = _collect_recent_messages(group_id)
    profile = get_member_profile(group_id, user_id) if group_id else None
    topic = _extract_topic(profile, recent)
    if seed:
        topic_seed = clean_text(seed).strip()
        if topic_seed and topic_seed not in topic:
            return f"{topic_seed} {topic}"
    return topic


def _decorate_emotion_text(reply: str, context: dict[str, str]) -> str:
    if not reply or not isinstance(reply, str):
        return reply

    if random.random() > _RICH_TEXT_PROB:
        return reply

    mood_level = _safe_value(context.get("group_activity_level"))
    if mood_level not in _MOOD_PREPEND:
        mood_level = "low" if int(_safe_value(context.get("group_activity", "40")) or 0) < 40 else "medium"

    prepend = _MOOD_PREPEND.get(mood_level, _MOOD_PREPEND["medium"])
    append = _MOOD_APPEND.get(mood_level, _MOOD_APPEND["medium"])

    out = f"{prepend} {reply} {append}"

    time_remark = _safe_value(context.get("time_remark"))
    if time_remark:
        out = f"{time_remark} {out}"

    return out.strip()


def _maybe_append_learned_face(reply: str, group_id: str | None) -> str:
    if not reply:
        return reply

    if random.random() > _NO_TOKEN_FACE_PROBABILITY:
        return reply

    face = get_random_face_token(group_id=group_id, limit=180)
    if not face:
        return reply

    if "[CQ:face" in reply:
        return reply

    if random.random() < 0.7:
        return f"{reply} {face}".strip()
    return f"{face} {reply}".strip()


def _finalize_reply(
    reply: str | None,
    *,
    group_id: str | None,
    context: dict[str, str],
    seed: str | None = None,
    enable_rich_text: bool = True,
) -> str | None:
    if not reply:
        return None
    if seed and _is_redundant_reply(seed, reply):
        return None
    text = _decorate_emotion_text(reply, context) if enable_rich_text else reply
    return _maybe_append_learned_face(text, group_id=group_id)


def _pick_reply_chain(group_id: str | None, seed: str | None) -> str | None:
    if not seed:
        return None
    norm_seed = clean_text(seed).strip()
    if not norm_seed:
        return None
    reply = get_reply_chain(group_id=group_id, seed=norm_seed)
    if reply:
        return reply
    if group_id is not None:
        return get_reply_chain(group_id=None, seed=norm_seed)
    return None


def _pick_meme(group_id: str | None, seed: str | None) -> str | None:
    if not seed:
        return get_random_meme(group_id=group_id)
    return get_random_meme(group_id=group_id, seed=seed)


def _pick_history_quote(group_id: str | None, seed: str | None = None) -> str | None:
    if random.random() > _HISTORY_QUOTE_PROBABILITY:
        return None
    if not group_id:
        return None

    normalized_seed = clean_text(seed or "").strip()
    if normalized_seed:
        keyword_hits = get_messages_by_keyword(
            group_id=group_id,
            keyword=normalized_seed,
            pool_size=_HISTORY_POOL_SIZE,
            limit=_HISTORY_QUOTE_SAMPLE_SIZE,
        )
        if keyword_hits:
            quote = clean_text(_safe_value(random.choice(keyword_hits).get("message"))).strip()
            if quote:
                return quote

    all_recent = get_message_pool(group_id=group_id, pool_size=_HISTORY_POOL_SIZE)
    if not all_recent:
        return None
    quote = clean_text(_safe_value(random.choice(all_recent).get("message"))).strip()
    return quote or None


def _pick_quote_bank(group_id: str | None, seed: str | None = None) -> str | None:
    quotes = get_group_quotes(group_id=group_id, seed=seed, limit=_NO_TOKEN_QUOTE_PARTS_MAX * 2)
    if not quotes and seed:
        quotes = get_group_quotes(group_id=group_id, seed=None, limit=_NO_TOKEN_QUOTE_PARTS_MAX * 2)
    if not quotes:
        return None

    return _join_quote_fragments(quotes, seed=seed)


def _pick_history_quote_bank(group_id: str | None, seed: str | None = None) -> list[str]:
    if random.random() > _HISTORY_QUOTE_PROBABILITY:
        return []
    if not group_id:
        return []

    normalized_seed = clean_text(seed or "").strip()
    rows: list[dict[str, object]] = []
    if normalized_seed:
        rows = get_messages_by_keyword(
            group_id=group_id,
            keyword=normalized_seed,
            pool_size=_HISTORY_POOL_SIZE,
            limit=_HISTORY_QUOTE_SAMPLE_SIZE,
        ) or []

    if not rows:
        rows = get_message_pool(group_id=group_id, pool_size=_HISTORY_POOL_SIZE) or []
    if not rows:
        return []

    raw_quotes = [_safe_value(random.choice(rows).get("message"))]
    for _ in range(_NO_TOKEN_QUOTE_PARTS_MAX - 1):
        item = random.choice(rows)
        quote = _safe_value(item.get("message"))
        if quote:
            raw_quotes.append(quote)
    return raw_quotes


def _join_quote_fragments(quotes: list[str], *, seed: str | None = None) -> str | None:
    if not quotes:
        return None

    uniq: list[str] = []
    for q in quotes:
        text = clean_text(q).strip()
        if not text:
            continue
        if text in uniq:
            continue
        if seed and _is_redundant_reply(clean_text(seed), text):
            continue
        uniq.append(text)

    if not uniq:
        return None

    random.shuffle(uniq)
    max_parts = random.randint(_NO_TOKEN_QUOTE_PARTS_MIN, min(_NO_TOKEN_QUOTE_PARTS_MAX, len(uniq)))
    picked = uniq[:max_parts]

    parts: list[str] = []
    current_len = 0
    seps = ["，", "，", "。", "…", " "]
    for frag in picked:
        frag = frag.strip()
        if not frag:
            continue
        if not parts:
            if len(frag) > _NO_TOKEN_MAX_REPLY_CHARS:
                frag = frag[: _NO_TOKEN_MAX_REPLY_CHARS]
            parts.append(frag)
            current_len = len(frag)
            continue

        sep = random.choice(seps)
        add_len = len(sep) + len(frag)
        if current_len + add_len > _NO_TOKEN_MAX_REPLY_CHARS:
            break
        parts.append(f"{sep}{frag}")
        current_len += add_len

    if not parts:
        return None

    return "".join(parts)


def build_no_token_reply(
    group_id: str | None,
    user_id: str,
    mentioned_ids: list[str] | None = None,
    seed: str | None = None,
    mood_state: dict | None = None,
    use_rich_text: bool = True,
    *,
    only_quotes: bool = False,
) -> str | None:
    if mood_state and isinstance(mood_state, dict):
        try:
            if int(mood_state.get("social", 50)) >= 80:
                pass
        except Exception:
            pass

    context = _build_context(group_id, user_id, seed=seed, mentioned_ids=mentioned_ids)
    recent = _collect_recent_messages(group_id)
    context["topic"] = _build_topic_text(group_id, user_id, seed=seed)

    quote = _pick_quote_bank(group_id=group_id, seed=seed)
    if quote:
        return _finalize_reply(
            quote,
            group_id=group_id,
            context=context,
            seed=seed,
            enable_rich_text=use_rich_text,
        )

    if not quote and group_id is not None:
        quote = _pick_quote_bank(group_id=group_id, seed=None)
        if quote:
            return _finalize_reply(
                quote,
                group_id=group_id,
                context=context,
                seed=seed,
                enable_rich_text=use_rich_text,
            )

    quote = _join_quote_fragments(_pick_history_quote_bank(group_id=group_id, seed=seed), seed=seed)
    if quote:
        return _finalize_reply(
            quote,
            group_id=group_id,
            context=context,
            seed=seed,
            enable_rich_text=use_rich_text,
        )

    if group_id is not None:
        quote = _pick_quote_bank(group_id=None, seed=seed)
        if quote:
            return _finalize_reply(
                quote,
                group_id=group_id,
                context=context,
                seed=seed,
                enable_rich_text=use_rich_text,
            )

    if only_quotes:
        return None

    if seed and _safe_value(context.get("topic")) != "缇よ亰":
        reply = _pick_reply_chain(group_id, context["topic"])
        if reply:
            return _finalize_reply(
                reply,
                group_id=group_id,
                context=context,
                seed=seed,
                enable_rich_text=use_rich_text,
            )

    meme = _pick_meme(group_id, seed)
    if meme:
        return _finalize_reply(
            meme,
            group_id=group_id,
            context=context,
            seed=seed,
            enable_rich_text=use_rich_text,
        )

    template = _select_template(context)
    if template and not template.isspace():
        return _finalize_reply(
            template,
            group_id=group_id,
            context=context,
            seed=seed,
            enable_rich_text=use_rich_text,
        )

    reply = _pick_reply_chain(group_id, seed)
    if reply:
        return _finalize_reply(
            reply,
            group_id=group_id,
            context=context,
            seed=seed,
            enable_rich_text=use_rich_text,
        )

    markov = build_markov_reply(group_id=group_id, seed=seed)
    if markov:
        return _finalize_reply(
            markov,
            group_id=group_id,
            context=context,
            seed=seed,
            enable_rich_text=use_rich_text,
        )

    return None

