"""Listen to group notice events and feed the event system."""

from nonebot import on_notice
from nonebot.adapters.onebot.v11 import (
    GroupDecreaseNoticeEvent,
    GroupIncreaseNoticeEvent,
    NoticeEvent,
)

from services.event_system import (
    on_group_join,
    on_group_leave,
    on_group_rename,
    on_user_rename,
)


group_events_listener = on_notice()


def _to_str(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


@group_events_listener.handle()
async def _(event: NoticeEvent):
    notice_type = _to_str(getattr(event, "notice_type", ""))
    if not notice_type:
        return

    group_id = _to_str(getattr(event, "group_id", ""))
    if not group_id:
        return

    if isinstance(event, GroupIncreaseNoticeEvent):
        on_group_join(group_id=group_id, user_id=_to_str(getattr(event, "user_id", "")))
        return

    if isinstance(event, GroupDecreaseNoticeEvent):
        on_group_leave(
            group_id=group_id,
            user_id=_to_str(getattr(event, "user_id", "")),
            operator_id=_to_str(getattr(event, "operator_id", "")),
        )
        return

    if notice_type == "group_card":
        on_user_rename(
            group_id=group_id,
            user_id=_to_str(getattr(event, "user_id", "")),
            old_name=_to_str(getattr(event, "card_old", "")),
            new_name=_to_str(getattr(event, "card_new", "")),
        )
        return

    if notice_type in {"group_name", "group_name_update"}:
        on_group_rename(
            group_id=group_id,
            old_name=_to_str(getattr(event, "name_old", "")),
            new_name=_to_str(getattr(event, "name", "")),
        )
        return

