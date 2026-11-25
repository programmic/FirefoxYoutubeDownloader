import time
import asyncio
import traceback

from .download_worker import download_video
from .database import Database
from . import config as config_module
from . import state

# Use a Database instance here (avoid importing app to prevent circular imports)
db = Database()

HEARTBEAT_INTERVAL = 5
PRINT_HEARTBEAT = False

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
    print("[WORKER]  Starting worker")
    async def worker_loop():
        while True:
            try:
                print(f"[WORKER]  Waiting for item (qsize={queue.qsize()})")
            except Exception:
                print("[WORKER]  Waiting for item (qsize=? )")

            url = await queue.get()
            print(f"[WORKER]  Dequeued: {url}")
            print(f"[WORKER]  Starting download: (\033[37m{queue.qsize()} remaining)\033[0m {url} - {time.strftime('%Y-%m-%d %H:%M:%S')}")
            state.active_downloads += 1
            try:
                # 0 / unset = no limit: re-encodes legitimately take a long time (H1)
                timeout = config_module.CONFIG.get("download_timeout") or None
                # status handling (downloading/completed/failed) lives in download_video
                await asyncio.wait_for(download_video(url), timeout=timeout)
            except asyncio.TimeoutError:
                print(f"[WORKER] Timeout downloading: {url}")
                db.update_status(url, "failed")
            except Exception as e:
                print(f"\033[31m[WORKER] Error downloading {url}:\033[91m {e}\033[0m")
                traceback.print_exc()
                try:
                    db.update_status(url, "failed")
                except Exception:
                    # ensure worker loop doesn't crash when DB update fails
                    print(f"\033[31m[WORKER] Failed to write error status to DB\033[0m")
            except BaseException as e:
                # BaseException includes shutdown cancellations. Record and requeue
                # interrupted work, but do not hide the cancellation from the task.
                print(f"[WORKER] Download interrupted: {url}: {type(e).__name__}: {e}")
                try:
                    db.update_status(url, "queued")
                except Exception:
                    traceback.print_exc()
                raise
            finally:
                state.active_downloads -= 1
                queue.task_done()

    heartbeat_task = asyncio.create_task(heartbeat(queue))
    configured_workers = config_module.CONFIG.get("max_concurrent_downloads", 1)
    try:
        max_workers = max(1, int(configured_workers))
    except (TypeError, ValueError):
        print(
            f"[WORKER] Invalid max_concurrent_downloads value: "
            f"{configured_workers!r}; using 1"
        )
        max_workers = 1

    await asyncio.gather(*(worker_loop() for _ in range(max_workers)))
    await heartbeat_task