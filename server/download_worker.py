
import asyncio
import os
import re
from pathlib import Path

from . import database as db
from . import config
CONFIG = config.CONFIG
PROJECT_ROOT = Path(__file__).resolve().parents[1]


_FFMPEG_POSTPROCESS_RE = re.compile(
    r'^\[(?P<postprocessor>[^\]]+)\] '
    r'(?P<action>Converting|Remuxing) video from '
    r'(?P<source_ext>[^ ]+) to (?P<target_ext>[^;]+); Destination: (?P<destination>.+)$'
)


def _replace_suffix(path, new_suffix):
    return str(Path(path).with_suffix(f'.{new_suffix.lstrip(".")}'))


def _resolve_project_path(value):
    path = Path(value)
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


def _format_bytes(value):
    units = ['B', 'KB', 'MB', 'GB', 'TB']
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == 'B':
                return f'{int(size)} {unit}'
            return f'{size:.1f} {unit}'
        size /= 1024


def _calculate_saved_bytes(snapshots):
    saved_bytes = 0

    for snapshot in snapshots:
        source_size = snapshot.get('source_size')
        destination = snapshot.get('destination')
        if source_size is None or not destination:
            continue

        try:
            destination_size = os.path.getsize(destination)
        except OSError:
            continue

        saved_bytes += max(0, source_size - destination_size)

    return saved_bytes

async def download_video(url):
    print(f"[DOWNLOAD] Starting download for: {url}")

    result = await asyncio.create_task(_download_async(url, db.Database()))
    return result

async def _download_async(url, database, config=CONFIG):

    args = [
        "yt-dlp",
        "--newline",
        "--progress",

        url,

        "--format",
        "bestvideo[height<=1080]+bestaudio/best[height<=1080]",

        "--output",
        config["output_template"],
    ]

    if config["embed_thumbnail"]:
        args.append("--embed-thumbnail")

    if config["write_subtitles"]:
        args.extend([
            "--write-subs",
            "--embed-subs",
            "--convert-subs",
            "srt",
        ])

    if config["embed_chapters"]:
        args.append("--embed-chapters")

    if config["embed_metadata"]:
        args.append("--embed-metadata")

    if config["remux_video"]:
        args.extend([
            "--remux-video",
            config["preferred_video_codecs"][0]
        ])

    if config["recode_video"]:
        args.extend([
            "--recode-video",
            "mp4"
        ])

    if config["browser_name"]:
        args.extend([
            "--impersonate",
            config["browser_name"]
        ])
        
    if config["cookies_from_browser"]:
        args.extend([
            "--cookies-from-browser",
            config["browser_name"]
        ])

    if config["cookies_file"]:
        cookies_path = _resolve_project_path(config["cookies_file"])
        if not cookies_path.exists():
            print(f"[WARNING] Cookies file not found: {cookies_path}")
        else:
            print(f"[DOWNLOAD] Using cookies file: {cookies_path}")
        args.extend([
            "--cookies",
            str(cookies_path)
        ])
    
    if config["merge_final"]:
        args.append("--merge-output-format")
        args.append(config["preferred_video_codecs"][0])

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT
    )

    ffmpeg_snapshots = []
    
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        decoded_line = line.decode(errors="ignore").rstrip()
        match = _FFMPEG_POSTPROCESS_RE.match(decoded_line)
        if match:
            destination = match.group('destination')
            source_ext = match.group('source_ext').lstrip('.')
            source_path = _replace_suffix(destination, source_ext)
            try:
                source_size = os.path.getsize(source_path)
            except OSError:
                source_size = None

            ffmpeg_snapshots.append({
                'destination': destination,
                'source_size': source_size,
            })

        print(decoded_line, end="\r")
    rc = await proc.wait() # this will wait until the process finishes and return the exit code
    if rc != 0:
        raise RuntimeError(f"yt-dlp exit {rc}")
    else:
        print(f"\n[DOWNLOAD] Finished downloading: {url}")
        database.update_status(url, "downloaded")
        return {
            'status': 'downloaded',
            'saved_bytes': _calculate_saved_bytes(ffmpeg_snapshots),
        }

async def runall():

    database = db.Database()

    queued_urls = database.get_queued()

    max_workers = CONFIG["max_concurrent_downloads"]

    semaphore = asyncio.Semaphore(max_workers)

    summary = {
        'succeeded': 0,
        'failed': 0,
        'saved_bytes': 0,
    }

    async def worker(url):

        async with semaphore:

            try:
                result = await download_video(url)
                if result and result.get('status') == 'downloaded':
                    summary['succeeded'] += 1
                    summary['saved_bytes'] += result.get('saved_bytes', 0)

            except Exception as e:

                print(f"\n[ERROR] Download failed for {url}")
                print(f"[ERROR] {e}")

                database.update_status(url, "failed")
                summary['failed'] += 1

    tasks = [worker(url) for url in queued_urls]

    await asyncio.gather(*tasks)

    print("\n[SYSTEM] All downloads complete")
    print(
        f"[SYSTEM] Summary: {summary['succeeded']} succeeded, "
        f"{summary['failed']} failed, "
        f"{_format_bytes(summary['saved_bytes'])} saved by ffmpeg post-processing"
    )

if __name__ == "__main__":
    # fetch url list from downloads.db, get all entries with status queued
    # for each entry, call download_video(url)

    database = db.Database()
    
    database.get_queued()

    for url in database.get_queued():
        asyncio.run(download_video(url))