import subprocess
import re
import asyncio
from . import database as db

async def download_video(url):
    print(f"[DOWNLOAD] Starting download for: {url}")
    return await _download_async(url)

async def _download_async(url):
    database = db.Database()
    proc = await asyncio.create_subprocess_exec(
        "yt-dlp", "--newline", "--progress", url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT
    )
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        print(line.decode(errors="ignore").rstrip())
    rc = await proc.wait()
    if rc != 0:
        print(f"\n[ERROR] yt-dlp failed for {url}")
        database.update_status(url, "failed")
        return False

    print(f"\n[DOWNLOAD] Finished downloading: {url}")
    database.update_status(url, "downloaded")
    return True