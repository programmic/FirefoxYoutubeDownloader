import time
import asyncio

from downloader import download_video
from .database import Database

# Use a Database instance here (avoid importing app to prevent circular imports)
db = Database()

HEARTBEAT_INTERVAL = 5
DOWNLOAD_TIMEOUT = 900
MAX_WORKERS = 2


async def heartbeat(queue: asyncio.Queue):
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL)
        try:
            print(f"[HEARTBEAT] queue qsize={queue.qsize()}")
        except Exception as e:
            print("[HEARTBEAT] failed to read queue size:", e)


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
            try:
                await asyncio.wait_for(download_video(url), timeout=DOWNLOAD_TIMEOUT)
                db.update_status(url, "completed")
            except asyncio.TimeoutError:
                print(f"[WORKER] Timeout downloading: {url}")
                db.update_status(url, "timeout")
            except Exception as e:
                print(f"[WORKER] Error downloading {url}:", e)
                try:
                    db.update_status(url, "error")
                except Exception:
                    # ensure worker loop doesn't crash when DB update fails
                    print("[WORKER] Failed to write error status to DB")
            finally:
                queue.task_done()

    heartbeat_task = asyncio.create_task(heartbeat(queue))
    await asyncio.gather(*(worker_loop() for _ in range(MAX_WORKERS)))
    await heartbeat_task