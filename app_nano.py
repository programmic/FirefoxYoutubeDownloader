import asyncio
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from server.config import CONFIG
from server.database import Database
import server.download_worker

app = FastAPI()
db = Database()

class CmdRequest(BaseModel):
    cmd: str

@app.on_event("startup")
async def startup():
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

    url = data.get("url")

    if not url:
        return JSONResponse(
            status_code=422,
            content={"detail": "Missing 'url' field"}
        )

    print(f"Received URL: {url}")

    db.add_video(url, "queued")

    return {"status": "queued", "url": url}

    

@app.post("/log")
async def log(msg: dict):
    print("[EXTENSION]", msg.get("text"))
    return {"ok": True}

@app.post("/command")
async def command(request: Request):

    data = await request.json()

    cmd = data.get("cmd")

    if cmd == "!RUNALL":

        runall = getattr(server.download_worker, "runall", None)

        if asyncio.iscoroutinefunction(runall):
            asyncio.create_task(runall())
        else:
            loop = asyncio.get_running_loop()
            loop.run_in_executor(None, runall)

        return {"status": "runall_started"}

    return {"status": "unknown_command"}

@app.get("/debug/db")
async def debug_db():
    try:
        queued = db.get_queued()
    except Exception as e:
        return {"error": str(e)}
    return {"queued_count": len(queued), "items": queued}

if __name__ == "__main__":
    print("Execute the following command from the project root:\nuvicorn app_nano:app --host 127.0.0.1 --port 8765")
