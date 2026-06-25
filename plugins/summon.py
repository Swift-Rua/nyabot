"""
召唤/遣返模块 — 管理 bot 的陪聊模式开关。
"""
import time
from nonebot import on_message
from nonebot.adapters.onebot.v11 import GroupMessageEvent

summon = on_message(priority=1, block=False)

# { group_id: bool }
group_state: dict[str, bool] = {}

# { group_id: float }
group_start_time: dict[str, float] = {}

# { group_id: float }
group_last_active: dict[str, float] = {}

# { group_id: bool }  默认 False = 文字模式
group_voice_mode: dict[str, bool] = {}

SUMMON_COMMANDS: tuple[str, ...] = (
    "牛牛喵召唤术！",
    "牛牛喵归来！",
    "牛牛喵遣返术！",
    "牛牛喵 语音模式",
    "牛牛喵 文字模式",
)

ACTIVE_DURATION = 20 * 60   # 召唤最长持续 20 分钟
AUTO_SLEEP = 3 * 60        # 3 分钟没人说话自动休眠


def activate(group_id: str):
    now = time.time()
    group_state[group_id] = True
    group_start_time[group_id] = now
    group_last_active[group_id] = now


def deactivate(group_id: str):
    """关闭召唤模式并清理状态"""
    group_state.pop(group_id, None)
    group_start_time.pop(group_id, None)
    group_last_active.pop(group_id, None)


def check_auto(group_id: str):
    """检查是否应该自动休眠（每次收到消息 + 后台循环都会调）"""
    if not group_state.get(group_id, False):
        return

    now = time.time()
    start = group_start_time.get(group_id, now)
    last = group_last_active.get(group_id, now)

    if now - start > ACTIVE_DURATION:
        deactivate(group_id)
        return

    if now - last > AUTO_SLEEP:
        deactivate(group_id)


@summon.handle()
async def _(event: GroupMessageEvent):
    group_id = str(event.group_id)
    text = event.get_plaintext().strip()

    if text == "牛牛喵召唤术！":
        activate(group_id)
        await summon.finish("🐱上线啦！陪聊20分钟喵～")

    if text == "牛牛喵遣返术！":
        deactivate(group_id)
        await summon.finish("💤回纸箱睡觉了～")

    if text == "牛牛喵 语音模式":
        group_voice_mode[group_id] = True
        await summon.finish("🔊 切换为语音模式啦~")

    if text == "牛牛喵 文字模式":
        group_voice_mode[group_id] = False
        await summon.finish("📝 切换为文字模式啦~")
