"""Shared in-memory group runtime flags."""

import time


_GROUP_MUTE_UNTIL: dict[str, float] = {}


def mute_group(group_id: str, seconds: float) -> None:
    _GROUP_MUTE_UNTIL[str(group_id)] = time.time() + max(0.0, float(seconds))


def unmute_group(group_id: str) -> None:
    _GROUP_MUTE_UNTIL.pop(str(group_id), None)


def is_group_muted(group_id: str) -> bool:
    group_id = str(group_id)
    until = _GROUP_MUTE_UNTIL.get(group_id)
    if not until:
        return False
    if time.time() >= until:
        _GROUP_MUTE_UNTIL.pop(group_id, None)
        return False
    return True
