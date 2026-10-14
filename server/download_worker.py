# server/download_worker.py

import asyncio
import json
import os
import re
import subprocess
from pathlib import Path
from dotenv import load_dotenv

from . import database as db
from . import config as config_module

PROJECT_ROOT = Path(__file__).resolve().parents[1]

env = load_dotenv()

_FFMPEG_POSTPROCESS_RE = re.compile(
    r'^\[(?P<postprocessor>[^\]]+)\] '
    r'(?P<action>Converting|Remuxing) video from '
    r'(?P<source_ext>[^ ]+) to (?P<target_ext>[^;]+); Destination: (?P<destination>.+)$'
)

_EPISODE_PREFIX_PATTERNS = (
    re.compile(r'(?i)\bS(?P<season>\d{1,2})\s*E(?P<episode>\d{1,3})\b'),
    re.compile(r'(?i)\bEP(?:ISODE)?\.?\s*(?P<episode>\d{1,3})\b'),
    re.compile(r'(?i)(?<!\w)#\s*(?P<episode>\d{1,3})\b'),
)

_RUNTIME_TITLE_CACHE = {}

def _build_ytdlp_auth_args(config, silence="quiet"):
    """
    Build the JavaScript runtime and authentication arguments used by
    the yt-dlp CLI. These arguments are shared by title fetching and
    actual downloads so both use the exact same configuration.
    
    # Parameters:
    - config: A dictionary containing the configuration settings.
    - silence: A string indicating the level of silence for yt-dlp output.
      - "all" (default): Suppresses all output.
      - "quiet": Suppresses most output but allows warnings and errors.
      - "not-js": supresses everything but the JavaScript runtime output (useful for debugging JS runtime issues).
      - "browser": suppresses everything but the browser-related output (useful for debugging browser issues).
    """
    
    def print_debug(msg, level="all"):
        # determines wether the message should be printed based on the silence level
        # if level is greater or equal to the silence level, print the message
        levels = {"all": 5, "quiet": 3, "not-js": 1, "browser": 1}
        if levels.get(level, 0) >= levels.get(silence, 0):
            print(msg)
    
    args = []

    # JavaScript runtime
    js_runtime = config.get("js_runtime_loc")
    if isinstance(js_runtime, str) and js_runtime.startswith(".env/"):
        env_name = js_runtime.removeprefix(".env/")
        js_runtime = os.environ.get(env_name)
        print_debug(f"[YT-DLP] JS runtime environment variable {env_name}: {js_runtime!r}", level="not-js")
    else:
        print_debug(f"[YT-DLP] JS runtime config: {js_runtime!r}", level="not-js")

    if js_runtime and str(js_runtime).lower() != "none":
        js_runtime_path = _resolve_project_path(js_runtime)

        if js_runtime_path.is_dir():
            js_runtime_path /= "node.exe"

        print_debug(f"[YT-DLP] Resolved JS runtime: {js_runtime_path}", level="not-js")
        print_debug(f"[YT-DLP] JS runtime exists: {js_runtime_path.is_file()}", level="not-js")

        if js_runtime_path.is_file():
            args.extend([
                "--js-runtimes",
                f"node:{js_runtime_path}",
            ])
        else:
            print_debug(
                f"[YT-DLP] WARNING: JavaScript runtime not found: "
                f"{js_runtime_path}",
                level="quiet"
            )

    # Browser cookies
    if config.get("cookies_from_browser"):
        browser_name = config.get("browser_name", "firefox")

        print_debug(
            f"[YT-DLP] Using browser cookies from: "
            f"{browser_name}",
            level="browser"
        )

        args.extend([
            "--cookies-from-browser",
            browser_name,
        ])

    # Cookie file
    cookies_file = config.get("cookies_file")

    if cookies_file:
        cookies_path = _resolve_project_path(cookies_file)
        print_debug(
            f"[YT-DLP] Resolved cookies file: {cookies_path}",
            level="browser"
        )
        print_debug(
            f"[YT-DLP] Cookies file exists: {cookies_path.is_file()}",
            level="browser"
        )

        if cookies_path.is_file():
            print_debug(
                f"[YT-DLP] Using cookies file: "
                f"{cookies_path}",
                level="browser"
            )

            args.extend([
                "--cookies",
                str(cookies_path),
            ])
        else:
            print_debug(
                f"[YT-DLP] WARNING: Cookies file not found: "
                f"{cookies_path}",
                level="quiet"
            )

    return args

def _fetch_video_title(url, conf_args=None): #used in download_worker.py and select.py
    """Fetches the video title using yt-dlp.
    Reroutes to `_fetch_video_title_async` to avoid blocking the FastAPI event loop."""
    return asyncio.run(_fetch_video_title_async(url, conf_args=conf_args))

async def _fetch_video_title_async(url, conf_args=None):
    """Asynchronous title fetching to keep the FastAPI event loop unblocked."""
    if url in _RUNTIME_TITLE_CACHE:
        return _RUNTIME_TITLE_CACHE[url]
    
    # check for playlist url
    if "&list=" in url or "?list=" in url:
        return await _fetch_playlist_title_async(url, conf_args=conf_args)
        
    cached_title = _load_title_from_databank(url)
    if cached_title:
        _RUNTIME_TITLE_CACHE[url] = cached_title
        return cached_title

    config = getattr(config_module, "CONFIG", {})
    try:
        args = ["yt-dlp", url, "--get-title", "--no-download", "--quiet"]
        if conf_args is None:
            conf_args = _build_ytdlp_auth_args(config)
        args.extend(conf_args)

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        
        if proc.returncode == 0:
            title = stdout.decode("utf-8", errors="replace").strip()
            if title:
                _RUNTIME_TITLE_CACHE[url] = title
                return title
        print(f"[TITLE] yt-dlp failed with exit code {proc.returncode}")
        error_output = stderr.decode("utf-8", errors="replace").strip()
        if error_output:
            print(f"[TITLE] stderr:\n{error_output}")
    except Exception as e:
        print(f"[TITLE] Async fetch failed for {url}: {e}")
    
    return url

async def _fetch_playlist_title_async(url, conf_args=None):
    """Fetches the title of a playlist using yt-dlp efficiently."""
    if conf_args is None:
        conf_args = _build_ytdlp_auth_args(getattr(config_module, "CONFIG", {}))

    args = [
        "yt-dlp",
        url,
        "--playlist-items", "0",
        "--print", "playlist_title",
        "--quiet"
    ]
    args.extend(conf_args)

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode == 0:
        title = stdout.decode("utf-8", errors="replace").strip()
        return title if title else f"Playlist: ({url})"
    else:
        print(f"[TITLE] yt-dlp failed to fetch playlist title with exit code {proc.returncode}")
        error_output = stderr.decode("utf-8", errors="replace").strip()
        if error_output:
            print(f"[TITLE] stderr:\n{error_output}")
        return url

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

def _load_title_from_databank(url):
    databank_path = config_module.NAME_DATABANK_PATH
    if not databank_path.is_file():
        return None
    # accept full links or youtube video IDs
    if not url.startswith("https://www.youtube.com/watch?v="):
        url = f"https://www.youtube.com/watch?v={url}"
    try:
        with open(databank_path, 'r', encoding='utf-8') as f:
            databank = json.load(f)
        return databank.get(url)
    except Exception:
        return None

def _detect_episode_prefix(title):
    if not title:
        return None
    stripped_title = title.strip()
    if re.match(r'(?i)^(?:S\d{1,2}\s*E\d{1,3}|EP(?:ISODE)?\.?\s*\d{1,3}|#\s*\d{1,3})\b', stripped_title):
        return None

    for pattern in _EPISODE_PREFIX_PATTERNS:
        match = pattern.search(stripped_title)
        if not match:
            continue
        episode = int(match.group('episode'))
        season = match.groupdict().get('season')
        if season is not None:
            return f"S{int(season):02d}E{episode:03d} - "
        return f"EP. {episode:03d} - "
    return None

def _build_format_selector(config):
    raw = config.get("max_resolution", 1080)

    def _parse_resolution(value):
        if value is None:
            return 1080
        if isinstance(value, (int, float)):
            val = int(value)
        else:
            s = str(value).strip().lower()
            m = re.match(r'^(\d+)\s*[x×]\s*(\d+)$', s)
            if m:
                try: 
                    return int(m.group(2))
                except Exception: 
                    return 1080
            m = re.match(r'^(\d+)p$', s)
            if m:
                try: 
                    return int(m.group(1))
                except Exception: 
                    return 1080
            if s.isdigit():
                val = int(s)
            else:
                return 1080

        common_widths = {3840, 2560, 1920, 1280}
        if val in common_widths:
            return int(round(val * 9 / 16))
        return val

    max_height = _parse_resolution(raw)
    if not isinstance(max_height, int) or max_height <= 0:
        max_height = 1080

    pvc = config.get("p_vcodec", "avc1")
    pac = config.get("p_acodec", "mp4a")

    return (
        f"bv*[vcodec^={pvc}][height<={max_height}]"
        f"+ba[acodec^={pac}]/"
        f"bv*[height<={max_height}]+ba/"
        f"b[height<={max_height}]"
    )

async def download_video(url, conf_args=None):
    database = db.Database()
    existing_status = database.get_status(url)
    if existing_status in {'downloaded', 'completed'}:
        print(f"[DOWNLOAD] Skipping already downloaded video: {url}")
        return {
            'status': 'skipped',
            'reason': 'already_downloaded',
            'saved_bytes': 0,
        }

    try:
        if conf_args is None:
            conf_args = _build_ytdlp_auth_args(
                getattr(config_module, "CONFIG", {})
            )
        title = await _fetch_video_title_async(url=url, conf_args=conf_args)
    except Exception as e:
        title = url
        print(f"Error fetching video title for url {url}: \n{e}")
        
    print(f"[DOWNLOAD] Starting: {url:<50} - {title[:75]:<75}")

    # FIXED: Added the mandatory title argument to prevent structural runtime crashes
    result = await _download_async(url, database, title, auth_args=conf_args)
    return result

async def _download_async(url, database, title, config=None, auth_args=None):
    if config is None:
        config = getattr(config_module, 'CONFIG', {})

    verbose = bool(config.get("ytdlp_verbal"))
    show_progress = bool(config.get("progress"))
    
    prefix_args = []
    prefix = _detect_episode_prefix(title)
    if prefix:
        prefix_args = ["--replace-in-metadata", "title", "^", prefix]

    args = [
        "yt-dlp",
        url,
        *prefix_args,
        "--format",
        _build_format_selector(config),
        "--output",
        config["output_template"],
    ]

    if verbose:
        args.append("-v")
    else:
        args.extend(["--quiet"])
    
    if show_progress:
        args.extend(["--progress", "--newline"])

    if config.get("embed_thumbnail"): 
        args.append("--embed-thumbnail")
    if config.get("write_subtitles"): 
        args.extend(["--write-subs", "--embed-subs", "--convert-subs", "srt"])
    if config.get("embed_chapters"): 
        args.append("--embed-chapters")
    if config.get("embed_metadata"): 
        args.append("--embed-metadata")
    if config.get("remux_video"): 
        args.extend(["--remux-video", config["preferred_video_container"][0]])
    if config.get("recode_video"): 
        args.extend(["--recode-video", "mp4"])

    if auth_args is None:
        auth_args = _build_ytdlp_auth_args(config)
    args.extend(auth_args)

    ffmpeg_location = config.get("ffmpeg_loc")
    if ffmpeg_location:
        if str(ffmpeg_location).startswith(".env/"):
            ffmpeg_location = os.environ.get(str(ffmpeg_location).removeprefix(".env/"))
        if ffmpeg_location:
            ffmpeg_path = _resolve_project_path(ffmpeg_location)
            if ffmpeg_path.exists():
                args.extend(["--ffmpeg-location", str(ffmpeg_path)])

    if config.get("sponsorblock_enabled"):
        sb_categories = config.get("sponsorblock_categories", [])
        if isinstance(sb_categories, list) and sb_categories:
            args.extend(["--sponsorblock-remove", ",".join(sb_categories)])
        else:
            print("[DOWNLOAD] SponsorBlock is enabled but no categories are specified. Setting to remove all (Default).")
            args.extend(["--sponsorblock-remove", "all"])
        
        # sponsor block force keyframe option to stop timestamp shifting to next I-frame when removing segments
        if config.get("sb_force_keyframe_at_cut"):
            args.append("--force-keyframes-at-cuts")
            

    if config.get("merge_final"):
        args.extend(["--merge-output-format", config["preferred_video_container"][0]])

    print("\n========== YT-DLP ==========")
    print(*args)
    print("============================")
    print(f"[DOWNLOAD] Downloading: {url} - {title[:75]}")
    print(f"\033[37m[SYSTEM] Queue length: {len(database.get_queued())}\033[0m")
    print("============================\n")

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
        if verbose:
            print(decoded_line)
            continue

        if show_progress:
            if decoded_line.startswith("[download]"):
                print("\r\033[K" + decoded_line, end="", flush=True)
            else:
                print(decoded_line)

    rc = await proc.wait()
    if rc != 0:
        raise RuntimeError(f"yt-dlp exit {rc}")

    print(f"\n[DOWNLOAD] Finished downloading: {url}")
    database.update_status(url, "downloaded")
    return {
        'status': 'downloaded',
        'saved_bytes': _calculate_saved_bytes(ffmpeg_snapshots),
    }

async def runall():
    database = db.Database()
    queued_urls = database.get_queued()
    max_workers = config_module.CONFIG.get("max_concurrent_downloads", 1)
    semaphore = asyncio.Semaphore(max_workers)

    summary = {
        'succeeded': 0, 'failed': 0, 'skipped': 0, 'saved_bytes': 0,
        'failed_downloads': [],
    }

    async def worker(url):
        async with semaphore:
            try:
                result = await download_video(url, conf_args=_build_ytdlp_auth_args(config_module.CONFIG))
                if result and result.get('status') == 'downloaded':
                    summary['succeeded'] += 1
                    summary['saved_bytes'] += result.get('saved_bytes', 0)
                elif result and result.get('status') == 'skipped':
                    summary['skipped'] += 1
            except Exception as e:
                print(f"\n[ERROR] Download failed for {url}: {e}")
                failed_title = _RUNTIME_TITLE_CACHE.get(url, f"Unknown-URL-ID-{id(url)}")
                database.update_status(url, "failed")
                summary['failed'] += 1
                summary['failed_downloads'].append({
                    "url": url,
                    "title": failed_title,
                    "reason": str(e),
			})

    tasks = [worker(url) for url in queued_urls]
    await asyncio.gather(*tasks)

    print("\n[SYSTEM] All downloads complete")
    print(
        f"[SYSTEM] Summary: {summary['succeeded']} videos succeeded, "
        f"{summary['failed']} videos failed, "
        f"{summary['skipped']} videos skipped, "
        f"{_format_bytes(summary['saved_bytes'])} storage space saved"
    )
    if len(summary["failed_downloads"]) >= 1:
        print(f"\n{'='*25} FAILED DOWNLOADS {'='*25}")
        for i in summary["failed_downloads"]:
            print(f"- {i.get('title')}\n   {i.get('reason')}")

if __name__ == "__main__":
    database = db.Database()
    for url in database.get_queued():
        asyncio.run(download_video(url, conf_args=_build_ytdlp_auth_args(config_module.CONFIG)))
