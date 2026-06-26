"""nyabot entrypoint."""
import asyncio
import nonebot
import os
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

_BACKGROUND_TASKS: set[asyncio.Task] = set()


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

    async def _run_task(coro):
        task = asyncio.create_task(coro)
        _BACKGROUND_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_TASKS.discard)
        return task

    await _run_task(proactive_loop())
    await _run_task(impression_loop())
    await _run_task(event_loop())


@driver.on_shutdown
async def stop_background_tasks():
    from services.sticker import close_session

    if not _BACKGROUND_TASKS:
        close_session()
        return

    tasks = list(_BACKGROUND_TASKS)
    for task in tasks:
        task.cancel()

    done, pending = await asyncio.wait(
        tasks,
        timeout=10.0,
        return_when=asyncio.ALL_COMPLETED,
    )

    for task in done:
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            continue
        except Exception as e:
            print(f"[bot] background task stop error: {e!r}")
        else:
            if exc is not None:
                print(f"[bot] background task stopped with: {exc!r}")

    if pending:
        for task in pending:
            task.cancel()
        await asyncio.sleep(0.1)

        for task in pending:
            if not task.done():
                _BACKGROUND_TASKS.discard(task)
                try:
                    tname = task.get_name()
                except Exception:
                    tname = str(task)
                print(f"[bot] background task still alive on shutdown: {tname}")
    close_session()


if __name__ == "__main__":
    nonebot.run()
