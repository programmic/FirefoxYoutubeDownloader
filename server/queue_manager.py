import time
import asyncio

from .download_worker import download_video
from .database import Database
from . import state

# Use a Database instance here (avoid importing app to prevent circular imports)
db = Database()

HEARTBEAT_INTERVAL = 5
PRINT_HEARTBEAT = False
DOWNLOAD_TIMEOUT = 900
MAX_WORKERS = 2


async def heartbeat(queue: asyncio.Queue):
    while True:
        if state.heartbeat:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            try:
                if PRINT_HEARTBEAT:
                    print(f"[HEARTBEAT] queue qsize={queue.qsize()}")
            except Exception as e:
                print("[HEARTBEAT] failed to read queue size:", e)
        else:
            await asyncio.sleep(HEARTBEAT_INTERVAL)


async def worker(queue: asyncio.Queue):
    print("[WORKER] Starting worker")
    async def worker_loop():
        while True:
            try:
                print(f"[WORKER] Waiting for item (qsize={queue.qsize()})")
            except Exception:
                print("[WORKER] Waiting for item (qsize=? )")

            url = await queue.get()
            print(f"[WORKER] Dequeued: {url}")
            print(f"[WORKER] Starting download: (\033[37m{queue.qsize()} remaining)\033[0m {url} - {time.strftime('%Y-%m-%d %H:%M:%S')}")
            try:
                result = await asyncio.wait_for(download_video(url), timeout=DOWNLOAD_TIMEOUT)
                if result and result.get('status') == 'downloaded':
                    db.update_status(url, "completed")
            except asyncio.TimeoutError:
                print(f"[WORKER] Timeout downloading: {url}")
                db.update_status(url, "timeout")
            except Exception as e:
                print(f"\033[31m[WORKER] Error downloading {url}:\033[91m", e, "\033[0m")
                try:
                    db.update_status(url, "error")
                except Exception:
                    # ensure worker loop doesn't crash when DB update fails
                    print(f"\033[31m[WORKER] Failed to write error status to DB\033[0m")
            finally:
                queue.task_done()

    heartbeat_task = asyncio.create_task(heartbeat(queue))
    await asyncio.gather(*(worker_loop() for _ in range(MAX_WORKERS)))
    await heartbeat_task