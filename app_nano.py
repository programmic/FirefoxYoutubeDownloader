import asyncio
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from server.config import CONFIG
from server.database import Database
import server.download_worker
import server.state as state
import server.queue_manager as queue_manager
import sys
import subprocess
import tempfile
import json
import argparse
import ctypes
import time
from pathlib import Path

app = FastAPI()
db = Database()
shutdown_on_idle_minutes = None
shutdown_monitor_task = None

class CmdRequest(BaseModel):
    cmd: str


def _queue_has_url(queue, url: str) -> bool:
    try:
        queued_items = getattr(queue, "_queue", None)
        if queued_items is None:
            return False
        return any(item == url for item in queued_items)
    except Exception:
        return False


async def _enqueue_url(url: str):
    existing_status = db.get_status(url)
    if existing_status in ["queued", "downloading", "completed"]:
        return {"status": "already_exists", "url": url}

    if existing_status in ["failed", "error"]:
        db.update_status(url, "queued")
    else:
        db.add_video(url, "queued")

    queue = getattr(state, "queue", None)
    if queue is None:
        state.queue = asyncio.Queue()
        queue = state.queue

    if not _queue_has_url(queue, url):
        try:
            queue.put_nowait(url)
        except Exception as e:
            print(f"Failed to put {url} on queue: {e}")

    return {"status": "queued", "url": url}


@app.on_event("startup")
async def startup():
    global shutdown_monitor_task
    # initialize queue and bind it to the running event loop so other threads
    # (e.g. the tkinter selection GUI) can schedule work onto it safely.
    if state.queue is None:
        state.queue = asyncio.Queue()
    # store running loop for thread-safe scheduling
    state.loop = asyncio.get_running_loop()

    # start the background queue worker (will run indefinitely)
    try:
        asyncio.create_task(queue_manager.worker(state.queue))
    except Exception as e:
        print("Failed to start queue worker:", e)

    if shutdown_on_idle_minutes is not None:
        shutdown_monitor_task = asyncio.create_task(_shutdown_when_idle())

    print("[SYSTEM] Listening on Port 8765")


def _system_idle_seconds():
    if sys.platform != "win32":
        return 0.0

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        raise OSError("GetLastInputInfo failed")
    tick_count = ctypes.windll.kernel32.GetTickCount()
    return max(0.0, (tick_count - info.dwTime) / 1000.0)


async def _shutdown_when_idle():
    idle_seconds = shutdown_on_idle_minutes * 60
    empty_since = None
    while True:
        await asyncio.sleep(5)
        if state.queue is None or state.queue.qsize() != 0 or state.active_downloads:
            empty_since = None
            continue

        now = time.monotonic()
        if empty_since is None:
            empty_since = now
        if now - empty_since < idle_seconds:
            continue
        try:
            device_idle = _system_idle_seconds()
        except OSError as exc:
            print(f"[SYSTEM] Unable to determine device idle time: {exc}")
            return
        if device_idle < idle_seconds:
            continue

        print("[SYSTEM] Queue and device idle; shutting down the computer.")
        subprocess.run(["shutdown", "/s", "/t", "0"], check=False)
        return


def _parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--shutdown_on_idle",
        type=float,
        metavar="MINUTES",
        help="ask for confirmation, then shut down when the device and download queue are idle",
    )
    args = parser.parse_args(argv)
    if args.shutdown_on_idle is not None and args.shutdown_on_idle <= 0:
        parser.error("--shutdown_on_idle must be greater than zero")
    return args


def _configure_shutdown_on_idle(argv=None):
    global shutdown_on_idle_minutes
    args = _parse_args(argv)
    if args.shutdown_on_idle is None:
        return
    answer = input(
        f"Shut down this PC after {args.shutdown_on_idle:g} minutes of device and queue idle time? [y/N] "
    ).strip().lower()
    if answer in {"y", "yes"}:
        shutdown_on_idle_minutes = args.shutdown_on_idle
        print("[SYSTEM] Shutdown-on-idle enabled.")
    else:
        print("[SYSTEM] Shutdown-on-idle disabled.")


@app.post("/enqueue")
async def enqueue(request: Request):
    # Log raw request for debugging 422 validation errors
    body_bytes = await request.body()
    try:
        body_text = body_bytes.decode(errors="replace")
    except Exception:
        body_text = str(body_bytes)

    try:
        data = await request.json()
    except Exception as e:
        print("ENQUEUE: failed to parse JSON:", e)
        return JSONResponse(status_code=422, content={"detail": "Invalid JSON"})

    # Single-url enqueue (backwards-compatible)
    url = data.get("url")
    if url:
        print(f"Received URL: {url}")
        return await _enqueue_url(url)

    # Bulk enqueue for immediate queueing (no selection)
    urls = data.get("urls")
    signal = data.get("signal")

    if urls and not signal:
        queued = 0
        for u in urls:
            if not isinstance(u, str):
                continue
            result = await _enqueue_url(u)
            if result.get("status") == "queued":
                queued += 1
        return {"status": "bulk_queued", "count": queued}

    # Bulk enqueue with signal -> store pending selection
    if urls and signal:
        if not isinstance(urls, list):
            return JSONResponse(status_code=422, content={"detail": "'urls' must be a list"})
        # store pending selection in memory
        state.pending_selections[signal] = urls
        print(f"Stored selection signal {signal} with {len(urls)} urls")
        return {"status": "selection_received", "signal": signal, "count": len(urls), "select_url": f"/select/{signal}"}

    return JSONResponse(
        status_code=422,
        content={"detail": "Missing 'url' field or 'urls' payload"}
    )


@app.post("/log")
async def log(msg: dict):
    print("[EXTENSION]", msg.get("text"))
    return {"ok": True}


@app.post("/command")
async def command(request: Request):
    data = await request.json()
    cmd = data.get("cmd")
    if not isinstance(cmd, str):
        return JSONResponse(status_code=422, content={"detail": "Missing 'cmd' string"})

    if cmd == "!RUNALL":
        runall = getattr(server.download_worker, "runall", None)
        if asyncio.iscoroutinefunction(runall):
            asyncio.create_task(runall())
        else:
            loop = asyncio.get_running_loop()
            loop.run_in_executor(None, runall)
        return {"status": "runall_started"}

    if cmd.startswith("!SELECT "):
        # plugin requested selection API for a pending signal
        signal = cmd.split(" ", 1)[1].strip()
        if signal in state.pending_selections:
            select_url = f"/select/{signal}"
            urls = state.pending_selections.get(signal, [])

            # spawn a separate process to run the GUI selector to avoid Tkinter
            # thread issues inside the server process. We write a small JSON file
            # containing the signal and urls and invoke the CLI runner module.
            try:
                tmp = tempfile.NamedTemporaryFile(prefix=f"select_{signal}_", suffix=".json", delete=False)
                tmp.write(json.dumps({"signal": signal, "urls": urls}).encode('utf-8'))
                tmp.flush()
                tmp.close()

                project_root = Path(__file__).resolve().parent
                cmd = [sys.executable, '-m', 'server.select_cli', tmp.name]
                subprocess.Popen(cmd, cwd=str(project_root))
                print(f"Selection runner started for {signal}; temp file: {tmp.name}")
            except Exception as e:
                print(f"Failed to start selection runner for {signal}:", e)

            return {"status": "selection_ready", "signal": signal, "select_url": select_url}
        else:
            return JSONResponse(status_code=404, content={"detail": "Signal not found"})

    return {"status": "unknown_command"}


@app.post("/settings/open")
async def open_settings():
    # spawn settings GUI in separate process
    try:
        project_root = Path(__file__).resolve().parent
        cmd = [sys.executable, '-m', 'server.settings_cli']
        subprocess.Popen(cmd, cwd=str(project_root))
        return {"status": "settings_started"}
    except Exception as e:
        print('Failed to start settings GUI:', e)
        return JSONResponse(status_code=500, content={"detail": str(e)})


@app.get("/settings")
async def get_settings():
    try:
        from server import config as cfg
        return {
            "sponsorblock_enabled": cfg.CONFIG.get("sponsorblock_enabled", False),
            "sponsorblock_categories": cfg.CONFIG.get("sponsorblock_categories", []),
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": str(e)})


@app.post("/settings/update")
async def post_settings(request: Request):
    try:
        data = await request.json()
    except Exception as e:
        return JSONResponse(status_code=422, content={"detail": "Invalid JSON"})

    enabled = data.get("sponsorblock_enabled")
    categories = data.get("sponsorblock_categories")

    try:
        from server import config as cfg
        cfg.update_sponsorblock(bool(enabled), list(categories) if categories is not None else [])
        return {"status": "ok"}
    except Exception as e:
        print('Failed to update settings:', e)
        return JSONResponse(status_code=500, content={"detail": str(e)})


@app.get("/select/{signal}")
async def get_select(signal: str):
    urls = state.pending_selections.get(signal)
    if urls is None:
        return JSONResponse(status_code=404, content={"detail": "Signal not found"})
    return {"signal": signal, "count": len(urls), "urls": urls}


@app.post("/select/{signal}")
async def post_select(signal: str, request: Request):
    urls = state.pending_selections.get(signal)
    if urls is None:
        return JSONResponse(status_code=404, content={"detail": "Signal not found"})

    try:
        data = await request.json()
    except Exception as e:
        print("SELECT POST: failed to parse JSON:", e)
        return JSONResponse(status_code=422, content={"detail": "Invalid JSON"})

    selected = data.get("selected")  # can be list of indices or urls
    to_enqueue = []

    if selected is None:
        # default: enqueue all
        to_enqueue = urls
    else:
        if not isinstance(selected, list):
            return JSONResponse(status_code=422, content={"detail": "'selected' must be a list of indices or urls"})
        # if list of ints -> map indices
        if all(isinstance(x, int) for x in selected):
            for idx in selected:
                if 0 <= idx < len(urls):
                    to_enqueue.append(urls[idx])
        else:
            # assume list of url strings
            for u in selected:
                if isinstance(u, str):
                    to_enqueue.append(u)

    queued = 0
    for u in to_enqueue:
        result = await _enqueue_url(u)
        if result.get("status") == "queued":
            queued += 1

    # remove the pending selection after choosing
    state.pending_selections.pop(signal, None)

    return {"status": "enqueued", "count": queued}


@app.get("/debug/db")
async def debug_db():
    try:
        queued = db.get_queued()
    except Exception as e:
        return {"error": str(e)}
    return {"queued_count": len(queued), "items": queued}


if __name__ == "__main__":
    _configure_shutdown_on_idle()
    print("Starting the downloader server.")
    print("Use: python app_nano.py [--shutdown_on_idle MINUTES]")
    import uvicorn
    uvicorn.run(app, host=CONFIG["host"], port=CONFIG["port"])