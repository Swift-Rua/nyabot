"""nyabot entrypoint."""
import os
import asyncio
import nonebot
from nonebot import get_bot
from nonebot.adapters.onebot.v11 import Adapter, MessageSegment
from dotenv import load_dotenv

load_dotenv()
try:
    GROUP_ID = int(os.getenv("GROUP_ID", "0"))
except (ValueError, TypeError):
    print("[bot] GROUP_ID invalid, skip startup/shutdown notification")
    GROUP_ID = 0

nonebot.init()

driver = nonebot.get_driver()
driver.register_adapter(Adapter)

nonebot.load_plugins("plugins")


async def _send_group_msg(text: str, with_sticker: bool = True):
    """Send a group message if GROUP_ID configured."""
    if not GROUP_ID:
        return
    try:
        bot = get_bot()
        from services.sticker import reply_with_sticker
        if with_sticker:
            seg, _ = reply_with_sticker()
            await bot.send_group_msg(
                group_id=GROUP_ID,
                message=MessageSegment.text(text) + seg,
            )
        else:
            await bot.send_group_msg(group_id=GROUP_ID, message=text)
    except Exception as e:
        print(f"[bot] send notify error: {e}")


@driver.on_bot_connect
async def on_bot_connect(bot):
    """No startup text."""
    await asyncio.sleep(1)


@driver.on_startup
async def on_start():
    pass


@driver.on_shutdown
async def on_stop():
    # keep minimal shutdown delay only
    await asyncio.sleep(1)


@driver.on_startup
async def start_background_tasks():
    from services.proactive import proactive_loop
    from services.impression import impression_loop
    from services.group_events import event_loop
    from services.profile_updater import ProfileUpdater

    # startup data cleanup
    await ProfileUpdater().rebuild_aliases()

    asyncio.create_task(proactive_loop())
    asyncio.create_task(impression_loop())
    asyncio.create_task(event_loop())


if __name__ == "__main__":
    nonebot.run()
