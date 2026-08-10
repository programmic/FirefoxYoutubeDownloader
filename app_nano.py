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
from pathlib import Path

app = FastAPI()
db = Database()

class CmdRequest(BaseModel):
    cmd: str


@app.on_event("startup")
async def startup():
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

    print("[SYSTEM] Listening on Port 8765")


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
        existing_status = db.get_status(url)
        if existing_status in ["queued", "downloading", "completed"]:
            return {"status": "already_exists", "url": url}
        db.add_video(url, "queued")
        return {"status": "queued", "url": url}

    # Bulk enqueue for immediate queueing (no selection)
    urls = data.get("urls")
    signal = data.get("signal")

    if urls and not signal:
        queued = 0
        for u in urls:
            if not isinstance(u, str):
                continue
            existing_status = db.get_status(u)
            if existing_status in ["queued", "downloading", "completed"]:
                continue
            db.add_video(u, "queued")
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
        existing_status = db.get_status(u)
        if existing_status in ["queued", "downloading", "completed"]:
            continue
        db.add_video(u, "queued")
        # also push onto the in-memory queue so downloads start immediately
        try:
            if getattr(state, 'queue', None) is not None:
                await state.queue.put(u)
            queued += 1
        except Exception as e:
            print(f"Failed to put {u} on queue: {e}")
            # still count as queued in DB
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
    print("Execute the following command from the project root:")
    print("uvicorn app_nano:app --host 127.0.0.1 --port 8765")
    print("NOTICE: IF FFMPEG IS NOT ON PATH, PROJECT IN CURRENT STATE WILL FAIL, PROPPER FFMPEG / FFMPROBE SETTINGS ARE IN WORK")