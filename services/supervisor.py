"""Independent process watchdog. Uses only the Python standard library."""

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"


def _log(message: str) -> None:
    with (DATA_DIR / "supervisor.log").open("a", encoding="utf-8") as file:
        file.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")


def _stop_worker(worker: subprocess.Popen) -> None:
    if worker.poll() is not None:
        return
    if os.name == "nt":
        # Kill only our own child tree, including Windows' venv launcher.
        subprocess.run(["taskkill", "/PID", str(worker.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
    else:
        worker.kill()
    worker.wait(timeout=15)


def run(command=None, *, stall_seconds=75, startup_seconds=120, restart_delay=5, max_restarts=None) -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    stop_file = DATA_DIR / "supervisor.stop"
    if command is None and "--stop" in sys.argv:
        stop_file.touch()
        print("[supervisor] Stop requested.")
        return 0
    # One supervisor per project, released by the OS even after a crash.
    lock_file = (DATA_DIR / "supervisor.lock").open("a+b")
    if lock_file.tell() == 0:
        lock_file.write(b"1")
        lock_file.flush()
    lock_file.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_file.close()
        print("[supervisor] Already running; no duplicate worker started.")
        return 1
    stop_file.unlink(missing_ok=True)
    command = command or [sys.executable, "-u", str(BASE_DIR / "bot.py"), "--worker"]
    stop_requested = False
    worker = None

    def request_stop(signum, frame):
        nonlocal stop_requested
        stop_requested = True

    previous = signal.signal(signal.SIGINT, request_stop)
    previous_term = signal.signal(signal.SIGTERM, request_stop)
    # Prevent CMD mouse selection from suspending this process's console IO.
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
        kernel32.GetStdHandle.restype = wintypes.HANDLE
        kernel32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        handle = kernel32.GetStdHandle(-10 & 0xffffffff)
        mode = wintypes.DWORD()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, (mode.value | 0x80) & ~0x40)
    print("[supervisor] Auto recovery enabled. Logs: data/bot_runtime.log", flush=True)
    restarts = 0
    try:
        while not stop_requested:
            run_id = uuid.uuid4().hex[:12]
            heartbeat = DATA_DIR / f"health-{run_id}.json"
            env = os.environ.copy()
            env["NYABOT_HEARTBEAT_FILE"] = str(heartbeat)
            env["NYABOT_THREAD_DUMP_FILE"] = str(DATA_DIR / f"hang-{run_id}.log")
            started = last_alive = time.monotonic()
            seen_heartbeat = False
            restart_reason = ""
            with (DATA_DIR / "bot_runtime.log").open("a", encoding="utf-8") as output:
                flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                worker = subprocess.Popen(command, cwd=BASE_DIR, env=env, stdin=subprocess.DEVNULL,
                                          stdout=output, stderr=subprocess.STDOUT, creationflags=flags)
                _log(f"worker started pid={worker.pid}, run={run_id}")
                while not stop_requested:
                    if stop_file.exists():
                        stop_requested = True
                        break
                    code = worker.poll()
                    if code is not None:
                        if code == 0:
                            _log("worker exited normally; supervisor stopping")
                            return 0
                        restart_reason = f"worker exited code={code}"
                        break
                    try:
                        health = json.loads(heartbeat.read_text(encoding="utf-8"))
                        last_alive = float(health["monotonic"])
                        seen_heartbeat = True
                    except (OSError, ValueError, KeyError, TypeError):
                        pass
                    now = time.monotonic()
                    if (seen_heartbeat and now - last_alive > stall_seconds) or (
                        not seen_heartbeat and now - started > startup_seconds
                    ):
                        restart_reason = f"event loop heartbeat stalled; run={run_id}"
                        break
                    time.sleep(0.5)
                _stop_worker(worker)
                worker = None
            heartbeat.unlink(missing_ok=True)
            heartbeat.with_suffix(".tmp").unlink(missing_ok=True)
            if stop_requested:
                break
            _log(f"restarting: {restart_reason}")
            restarts += 1
            if max_restarts is not None and restarts > max_restarts:
                return 1
            # Back off on persistent startup/crash failures, capped at 60s.
            delay_end = time.monotonic() + min(60, restart_delay * max(1, restarts if time.monotonic() - started < 300 else 1))
            while not stop_requested and time.monotonic() < delay_end:
                if stop_file.exists():
                    stop_requested = True
                    break
                time.sleep(0.5)
        _log("supervisor stopped by user")
        return 0
    finally:
        if worker is not None:
            _stop_worker(worker)
        signal.signal(signal.SIGINT, previous)
        signal.signal(signal.SIGTERM, previous_term)
        lock_file.close()
        stop_file.unlink(missing_ok=True)
