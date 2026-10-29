"""Event-loop heartbeat and native thread dumps for the external supervisor."""

import asyncio
import faulthandler
import json
import os
import time
from pathlib import Path

_dump_file = None


async def heartbeat_loop() -> None:
    global _dump_file
    target = os.getenv("NYABOT_HEARTBEAT_FILE")
    if not target:
        return
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    _dump_file = open(os.environ["NYABOT_THREAD_DUMP_FILE"], "a", encoding="utf-8")
    faulthandler.enable(file=_dump_file)
    try:
        while True:
            # This timer runs outside the event loop and can dump a blocked lock.
            faulthandler.dump_traceback_later(60, file=_dump_file)
            payload = json.dumps({"pid": os.getpid(), "monotonic": time.monotonic()})
            temporary = path.with_suffix(".tmp")
            try:
                temporary.write_text(payload, encoding="utf-8")
                try:
                    os.replace(temporary, path)
                except PermissionError:
                    # Windows can briefly hold the target while the supervisor
                    # reads it. Keep the heartbeat alive and retry next tick.
                    try:
                        path.write_text(payload, encoding="utf-8")
                    finally:
                        temporary.unlink(missing_ok=True)
            except OSError as exc:
                print(f"[health] heartbeat write failed: {type(exc).__name__}: {exc}", flush=True)
                temporary.unlink(missing_ok=True)
            await asyncio.sleep(2)
    finally:
        faulthandler.cancel_dump_traceback_later()
        faulthandler.disable()
        _dump_file.close()
        _dump_file = None
