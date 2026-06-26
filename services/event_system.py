"""Event system for social milestones and lightweight social records."""

import copy
import json
import os
import threading
import time


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
EVENT_STATE_FILE = os.path.join(DATA_DIR, "group_event_system.json")

_STATE_LOCK = threading.Lock()

_STREAK_WINDOW_SECONDS = 30 * 60
_CONSECUTIVE_THRESHOLD = 3
_MESSAGE_MILESTONES = (100, 1000)
_PENDING_LIMIT_PER_GROUP = 30
_DEFAULT_STATE = {
    "pending_events": {},
    "streaks": {},
    "milestones": {},
}


def _empty_state() -> dict:
    return copy.deepcopy(_DEFAULT_STATE)


def _normalize_group_id(group_id: object) -> str:
    value = str(group_id).strip() if group_id is not None else ""
    return value


def _normalize_user_id(user_id: object) -> str:
    value = str(user_id).strip() if user_id is not None else ""
    return value


def _normalize_text(value: object) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return text


def _safe_int(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clean_state(raw: object) -> dict:
    if not isinstance(raw, dict):
        return _empty_state()

    state = _empty_state()

    pending = raw.get("pending_events", {})
    if isinstance(pending, dict):
        for group_id, events in pending.items():
            gid = _normalize_group_id(group_id)
            if not gid or not isinstance(events, list):
                continue
            out = []
            for item in events:
                if not isinstance(item, dict):
                    continue
                text = _normalize_text(item.get("text"))
                if not text:
                    continue
                out.append(
                    {
                        "time": _safe_int(item.get("time")) or time.time(),
                        "type": _normalize_text(item.get("type")) or "event",
                        "text": text,
                        "meta": item.get("meta", {}),
                    }
                )
            if out:
                state["pending_events"][gid] = out[-_PENDING_LIMIT_PER_GROUP:]

    streaks = raw.get("streaks", {})
    if isinstance(streaks, dict):
        for group_id, value in streaks.items():
            gid = _normalize_group_id(group_id)
            if not gid or not isinstance(value, dict):
                continue

            user_id = _normalize_user_id(value.get("user_id"))
            count = _safe_int(value.get("count"))
            last_seen = _safe_int(value.get("last_seen"))
            if not user_id or count is None or last_seen is None:
                continue
            state["streaks"][gid] = {
                "user_id": user_id,
                "count": max(1, count),
                "last_seen": last_seen,
            }

    milestones = raw.get("milestones", {})
    if isinstance(milestones, dict):
        for group_id, users in milestones.items():
            gid = _normalize_group_id(group_id)
            if not gid or not isinstance(users, dict):
                continue
            fixed: dict[str, list[int]] = {}
            for uid, values in users.items():
                sid = _normalize_user_id(uid)
                if not sid or not isinstance(values, list):
                    continue
                picked = []
                for v in values:
                    vv = _safe_int(v)
                    if vv is None:
                        continue
                    if vv in _MESSAGE_MILESTONES:
                        picked.append(vv)
                if picked:
                    fixed[sid] = sorted(set(picked))
            if fixed:
                state["milestones"][gid] = fixed

    return state


def _load_state() -> dict:
    if not os.path.exists(EVENT_STATE_FILE):
        return _empty_state()

    try:
        with open(EVENT_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return _clean_state(data)
    except Exception:
        return _empty_state()


def _save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(EVENT_STATE_FILE), exist_ok=True)
    tmp_path = f"{EVENT_STATE_FILE}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        try:
            os.replace(tmp_path, EVENT_STATE_FILE)
        except PermissionError:
            with open(EVENT_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


def _append_pending(group_id: str, event_type: str, text: str, meta: dict | None = None) -> None:
    gid = _normalize_group_id(group_id)
    if not gid:
        return

    text = _normalize_text(text)
    if not text:
        return

    with _STATE_LOCK:
        state = _load_state()
        pending = state.setdefault("pending_events", {})
        queue = pending.setdefault(gid, [])

        if queue and queue[-1].get("text") == text:
            return

        queue.append(
            {
                "time": time.time(),
                "type": _normalize_text(event_type) or "event",
                "text": text,
                "meta": meta or {},
            }
        )
        state["pending_events"][gid] = queue[-_PENDING_LIMIT_PER_GROUP:]
        _save_state(state)


def take_events(group_id: str, limit: int = 1) -> list[str]:
    gid = _normalize_group_id(group_id)
    if not gid:
        return []

    limit = max(1, min(5, int(limit)))
    with _STATE_LOCK:
        state = _load_state()
        queue = state.get("pending_events", {}).get(gid, [])
        if not queue:
            return []

        picked = queue[:limit]
        state["pending_events"][gid] = queue[len(picked) :]
        if not state["pending_events"][gid]:
            state["pending_events"].pop(gid, None)
        _save_state(state)

        out: list[str] = []
        for item in picked:
            text = _normalize_text(item.get("text"))
            if text:
                out.append(text)
        return out


def _maybe_record_streak(group_id: str, user_id: str, user_name: str) -> None:
    gid = _normalize_group_id(group_id)
    uid = _normalize_user_id(user_id)
    if not gid or not uid:
        return

    name = _normalize_text(user_name) or uid
    now = int(time.time())

    with _STATE_LOCK:
        state = _load_state()
        streaks = state.setdefault("streaks", {})
        current = streaks.get(gid)

        if not isinstance(current, dict):
            streaks[gid] = {
                "user_id": uid,
                "count": 1,
                "last_seen": now,
            }
            _save_state(state)
            return

        last_user = _normalize_user_id(current.get("user_id"))
        last_seen = _safe_int(current.get("last_seen")) or now
        count = _safe_int(current.get("count")) or 0

        if last_user == uid and (now - int(last_seen)) <= _STREAK_WINDOW_SECONDS:
            count += 1
        else:
            count = 1

        streaks[gid] = {
            "user_id": uid,
            "count": count,
            "last_seen": now,
        }
        _save_state(state)

        if count == _CONSECUTIVE_THRESHOLD:
            _append_pending(
                gid,
                "streak",
                f"{name}({uid}) sent {count} messages in a row.",
            )


def _maybe_record_message_milestone(
    group_id: str,
    user_id: str,
    user_name: str,
    message_count: int | None,
) -> None:
    gid = _normalize_group_id(group_id)
    uid = _normalize_user_id(user_id)
    if not gid or not uid:
        return

    if message_count is None:
        return

    count = int(message_count)
    if count <= 0:
        return

    name = _normalize_text(user_name) or uid
    threshold_set = set(_MESSAGE_MILESTONES)

    with _STATE_LOCK:
        state = _load_state()
        milestones = state.setdefault("milestones", {})
        seen_map = milestones.setdefault(gid, {})
        if not isinstance(seen_map, dict):
            seen_map = {}
            milestones[gid] = seen_map

        raw_seen = seen_map.get(uid, [])
        if not isinstance(raw_seen, list):
            raw_seen = []

        seen = {
            item
            for item in (_safe_int(v) for v in raw_seen)
            if item is not None and item in threshold_set
        }
        changed = False

        for milestone in _MESSAGE_MILESTONES:
            if count >= milestone and milestone not in seen:
                seen.add(milestone)
                _append_pending(
                    gid,
                    "milestone",
                    f"{name}({uid}) reached {milestone} messages.",
                )
                changed = True

        if changed:
            seen_map[uid] = sorted(seen)
            _save_state(state)


def on_group_message(
    group_id: str,
    user_id: str,
    user_name: str,
    message_count: int | None = None,
) -> None:
    if not _normalize_group_id(group_id):
        return
    _maybe_record_streak(group_id, user_id, user_name)
    if message_count is not None:
        _maybe_record_message_milestone(group_id, user_id, user_name, message_count)


def on_group_join(group_id: str, user_id: str) -> None:
    gid = _normalize_group_id(group_id)
    uid = _normalize_user_id(user_id)
    if not gid or not uid:
        return
    _append_pending(gid, "join", f"Member {uid} joined the group.")


def on_group_leave(group_id: str, user_id: str, operator_id: str | None = None) -> None:
    gid = _normalize_group_id(group_id)
    uid = _normalize_user_id(user_id)
    if not gid or not uid:
        return

    operator = _normalize_user_id(operator_id)
    if operator and operator != uid:
        _append_pending(gid, "leave", f"Member {uid} was removed by {operator}.")
    else:
        _append_pending(gid, "leave", f"Member {uid} left the group.")


def on_user_rename(group_id: str, user_id: str, old_name: str, new_name: str) -> None:
    gid = _normalize_group_id(group_id)
    uid = _normalize_user_id(user_id)
    if not gid or not uid:
        return

    old_n = _normalize_text(old_name)
    new_n = _normalize_text(new_name)
    if not old_n or not new_n or old_n == new_n:
        return

    _append_pending(
        gid,
        "rename",
        f"Member {uid} changed name from '{old_n}' to '{new_n}'.",
    )


def on_group_rename(group_id: str, old_name: str, new_name: str) -> None:
    gid = _normalize_group_id(group_id)
    if not gid:
        return

    old_n = _normalize_text(old_name)
    new_n = _normalize_text(new_name)
    if not old_n or not new_n or old_n == new_n:
        return

    _append_pending(gid, "group_rename", f"Group name changed from '{old_n}' to '{new_n}'.")

