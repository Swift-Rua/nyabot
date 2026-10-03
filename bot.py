"""nyabot entrypoint."""
import asyncio
import os
import sys
import time

# The default Windows console code page is often GBK.  NoneBot logs raw group
# messages, so an emoji can otherwise make Loguru fail while writing the log.
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except (OSError, ValueError):
            pass

import services.system_tls
import nonebot
from nonebot import get_bot
from nonebot.plugin import on_metaevent
from nonebot.adapters.onebot.v11 import Adapter, Event, HeartbeatMetaEvent, MessageSegment
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
_HEARTBEAT_LAST_SEEN: dict[str, float | None] = {}
_HEARTBEAT_INTERVAL_MS: dict[str, int] = {}
_WATCHDOG_TASKS: dict[str, asyncio.Task] = {}
_DEFAULT_HEARTBEAT_INTERVAL_MS = 30_000
_HEARTBEAT_STALE_MULTIPLIER = 3.0
_HEARTBEAT_MIN_TIMEOUT_SECONDS = 90.0
_HEARTBEAT_CHECK_INTERVAL_SECONDS = 10.0


def _connection_log(message: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [onebot-watchdog] {message}"
    try:
        print(line)
    except Exception:
        pass

    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "connection_watchdog.log")
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as log_file:
            log_file.write(line + "\n")
    except OSError as e:
        try:
            print(f"[onebot-watchdog] could not write diagnostic log: {e}")
        except Exception:
            pass


async def _watch_onebot_connection(bot) -> None:
    """Close a stale reverse WS so NapCat can reconnect on its configured timer."""
    adapter = bot.adapter
    bot_id = str(bot.self_id)
    websocket = None

    while websocket is None:
        await asyncio.sleep(1)
        if bot_id not in adapter.bots:
            return
        websocket = adapter.connections.get(bot_id)

    connected_at = time.monotonic()
    while True:
        await asyncio.sleep(_HEARTBEAT_CHECK_INTERVAL_SECONDS)
        if adapter.connections.get(bot_id) is not websocket or websocket.closed:
            return

        last_seen = _HEARTBEAT_LAST_SEEN.get(bot_id)
        reference_time = last_seen if last_seen is not None else connected_at
        interval_ms = _HEARTBEAT_INTERVAL_MS.get(bot_id, _DEFAULT_HEARTBEAT_INTERVAL_MS)
        stale_after = max(
            _HEARTBEAT_MIN_TIMEOUT_SECONDS,
            interval_ms / 1000 * _HEARTBEAT_STALE_MULTIPLIER,
        )
        stale_for = time.monotonic() - reference_time
        if stale_for < stale_after:
            continue

        _connection_log(
            f"bot={bot_id} heartbeat missing for {int(stale_for)}s; "
            f"closing WS to trigger NapCat reconnect"
        )
        try:
            await asyncio.wait_for(
                websocket.close(code=1012, reason="OneBot heartbeat timeout"),
                timeout=5,
            )
        except Exception as e:
            _connection_log(f"bot={bot_id} WS close failed: {type(e).__name__}: {e}")
        return


def _watchdog_finished(bot_id: str, task: asyncio.Task) -> None:
    _BACKGROUND_TASKS.discard(task)
    if _WATCHDOG_TASKS.get(bot_id) is task:
        _WATCHDOG_TASKS.pop(bot_id, None)
    if task.cancelled():
        return
    try:
        error = task.exception()
    except Exception as e:
        _connection_log(f"bot={bot_id} watchdog result error: {type(e).__name__}: {e}")
        return
    if error is not None:
        _connection_log(f"bot={bot_id} watchdog crashed: {type(error).__name__}: {error}")


heartbeat_events = on_metaevent(priority=1, block=False)


@heartbeat_events.handle()
async def record_onebot_heartbeat(event: Event):
    if not isinstance(event, HeartbeatMetaEvent):
        return
    bot_id = str(event.self_id)
    first_heartbeat = _HEARTBEAT_LAST_SEEN.get(bot_id) is None
    _HEARTBEAT_LAST_SEEN[bot_id] = time.monotonic()
    if event.interval > 0:
        _HEARTBEAT_INTERVAL_MS[bot_id] = event.interval
    if first_heartbeat:
        _connection_log(
            f"bot={bot_id} heartbeat detected, interval={event.interval}ms"
        )


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
    bot_id = str(bot.self_id)
    previous_task = _WATCHDOG_TASKS.get(bot_id)
    if previous_task is not None and not previous_task.done():
        previous_task.cancel()

    _HEARTBEAT_LAST_SEEN[bot_id] = None
    _HEARTBEAT_INTERVAL_MS[bot_id] = _DEFAULT_HEARTBEAT_INTERVAL_MS
    _connection_log(f"bot={bot_id} reverse WS connected; heartbeat watchdog started")

    task = asyncio.create_task(_watch_onebot_connection(bot))
    _WATCHDOG_TASKS[bot_id] = task
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(lambda done, current_bot_id=bot_id: _watchdog_finished(current_bot_id, done))


@driver.on_bot_disconnect
async def on_bot_disconnect(bot):
    bot_id = str(bot.self_id)
    task = _WATCHDOG_TASKS.pop(bot_id, None)
    if task is not None and not task.done():
        task.cancel()
    _HEARTBEAT_LAST_SEEN.pop(bot_id, None)
    _HEARTBEAT_INTERVAL_MS.pop(bot_id, None)
    _connection_log(f"bot={bot_id} reverse WS disconnected")


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
    from services.sticker import close_session as close_sticker_session
    from services.chatgpt_api import close_session as close_chatgpt_session

    if not _BACKGROUND_TASKS:
        close_sticker_session()
        await close_chatgpt_session()
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
    close_sticker_session()
    await close_chatgpt_session()


if __name__ == "__main__":
    nonebot.run()
