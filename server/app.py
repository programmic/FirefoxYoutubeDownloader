import asyncio

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from config import CONFIG
from database import Database
import download_worker
import state as state

app = FastAPI()

db = Database()


class DownloadRequest(BaseModel):
    url: str

worker_task = None

@app.on_event("startup")
async def startup():
    global worker_task
    # ensure the queue is created on the running event loop
    if state.queue is None:
        state.queue = asyncio.Queue()
    print("[SYSTEM] Listening on Port 8765")


@app.post("/enqueue")
async def enqueue(request: Request):
    # Log raw request for debugging 422 validation errors
    body_bytes = await request.body()
    try:
        body_text = body_bytes.decode(errors="replace")
    except Exception:
        body_text = str(body_bytes)

    print("ENQUEUE RAW HEADERS:", dict(request.headers))
    print("ENQUEUE RAW BODY:", body_text)

    try:
        data = await request.json()
    except Exception as e:
        print("ENQUEUE: failed to parse JSON:", e)
        return JSONResponse(status_code=422, content={"detail": "Invalid JSON"})

    url = data.get("url")
    if not url:
        print("ENQUEUE: missing 'url' in JSON body:", data)
        return JSONResponse(status_code=422, content={"detail": "Missing 'url' field"})

    print(f"Enqueue request: {url}")
    print("QUEUE OBJECT ID:", id(state.queue))

    inserted = db.add_video(url, "queued")

    if not inserted:
        return {"status": "already_exists"}

    print("QUEUE SIZE BEFORE:", state.queue.qsize())
    await state.queue.put(url)
    print("QUEUE SIZE AFTER:", state.queue.qsize())

    print(f"Queued: {url}")

    return {"status": "ok"}

@app.post("/log")
async def log(msg: dict):
    print("[EXTENSION]", msg["text"])
    return {"ok": True}


@app.get("/debug/queue")
async def debug_queue():
    q = state.queue
    if q is None:
        return {"qsize": None, "items": None}
    try:
        # access the internal deque for debugging purposes
        items = list(q._queue)
    except Exception as e:
        items = f"error: {e}"
    return {"qsize": q.qsize(), "items": items}

if __name__ == "__main__":
    print("Execute the following command from the project root:\nuvicorn server.app:app --host 127.0.0.1 --port 8765")