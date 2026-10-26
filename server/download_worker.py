# server/download_worker.py

import asyncio
import json
import os
import re
import subprocess
import tempfile
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

def _expected_output_path(title, config):
    """Render the configured output template for the final video filename."""
    template = config.get("output_template")
    if not isinstance(template, str):
        return None

    extension = None
    if config.get("recode_video"):
        extension = "mp4"
    elif config.get("remux_video") or config.get("merge_final"):
        containers = config.get("preferred_video_container")
        if isinstance(containers, list) and containers:
            extension = str(containers[0])

    if extension is None:
        return None

    try:
        output_name = template.replace("%(title)s", str(title)).replace(
            "%(ext)s", extension
        )
    except Exception:
        return None
    return Path(output_name)

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

# ---------------------------------------------------------------------------
# Precise SponsorBlock cutting
#
# yt-dlp's own --sponsorblock-remove cuts with `-c copy` + the concat demuxer.
# Video can only be cut on keyframes, audio on any packet, and every segment
# boundary adds a small audio/video length mismatch (plus AAC priming delay),
# so the desync grows after each removed segment. Instead we let yt-dlp only
# *mark* sponsor segments as chapters, then cut them ourselves in one ffmpeg
# pass that re-encodes and trims video and audio with the same timestamps.
# ---------------------------------------------------------------------------

_SB_CHAPTER_PREFIX = "[SponsorBlock]"

# extension -> (video args, audio args, extra args)
_CUT_CODECS = {
    ".mp4":  (["-c:v", "libx264", "-preset", "slow", "-crf", "24"], ["-c:a", "aac", "-b:a", "160k"], ["-movflags", "+faststart"]),
    ".m4v":  (["-c:v", "libx264", "-preset", "slow", "-crf", "24"], ["-c:a", "aac", "-b:a", "160k"], ["-movflags", "+faststart"]),
    ".mkv":  (["-c:v", "libx264", "-preset", "slow", "-crf", "24"], ["-c:a", "aac", "-b:a", "160k"], []),
    ".webm": (["-c:v", "libvpx-vp9", "-crf", "30", "-b:v", "0", "-row-mt", "1"], ["-c:a", "libopus", "-b:a", "128k"], []),
}


def _resolve_ffmpeg_tools(ffmpeg_location):
    """Return (ffmpeg, ffprobe) executables; falls back to PATH."""
    ffmpeg, ffprobe = "ffmpeg", "ffprobe"
    if ffmpeg_location:
        loc = Path(ffmpeg_location)
        exe = ".exe" if os.name == "nt" else ""
        if loc.is_dir():
            ffmpeg = str(loc / f"ffmpeg{exe}")
            ffprobe = str(loc / f"ffprobe{exe}")
        elif loc.is_file():
            ffmpeg = str(loc)
            ffprobe = str(loc.with_name(f"ffprobe{loc.suffix}"))
    return ffmpeg, ffprobe


async def _probe_json(ffprobe, path, *flags):
    proc = await asyncio.create_subprocess_exec(
        ffprobe, "-v", "error", *flags, "-of", "json", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    return json.loads(stdout.decode("utf-8", errors="replace") or "{}")


def _merge_ranges(ranges):
    merged = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 0.001:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


async def _cut_sponsors_precisely(path, ffmpeg_location=None):
    """Remove SponsorBlock-marked chapters from `path` with a frame-accurate re-encode."""
    path = Path(path)
    ffmpeg, ffprobe = _resolve_ffmpeg_tools(ffmpeg_location)

    chapters = (await _probe_json(ffprobe, path, "-show_chapters")).get("chapters", [])
    cuts = _merge_ranges(
        (float(c["start_time"]), float(c["end_time"]))
        for c in chapters
        if (c.get("tags") or {}).get("title", "").startswith(_SB_CHAPTER_PREFIX)
    )
    if not cuts:
        print(f"[SPONSORBLOCK] No sponsor segments in {path.name}; leaving file untouched.")
        return

    duration = float((await _probe_json(ffprobe, path, "-show_format"))["format"]["duration"])

    # Locate the main video stream and the embedded thumbnail (mp4: attached_pic video stream)
    streams = (await _probe_json(ffprobe, path, "-show_streams")).get("streams", [])
    thumb_idx, video_idx = None, None
    
    for st in streams:
        if st.get("codec_type") != "video":
            continue
        if (st.get("disposition") or {}).get("attached_pic"):
            thumb_idx = thumb_idx if thumb_idx is not None else st["index"]
        elif video_idx is None:
            video_idx = st["index"]
    if video_idx is None:
        print(f"[SPONSORBLOCK] No video stream found in {path.name}; skipping cut.")
        return

    # Invert the cut list into the ranges we keep
    keep, cursor = [], 0.0
    for start, end in cuts:
        if start - cursor > 0.05:
            keep.append((cursor, start))
        cursor = max(cursor, end)
    if duration - cursor > 0.05:
        keep.append((cursor, duration))
    if not keep:
        print(f"[SPONSORBLOCK] Whole video is marked as removable; skipping cut for {path.name}.")
        return

    parts, labels = [], []
    for i, (a, b) in enumerate(keep):
        parts.append(f"[0:{video_idx}]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[v{i}]")
        parts.append(f"[0:a:0]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS[a{i}]")
        labels.append(f"[v{i}][a{i}]")
    parts.append(f"{''.join(labels)}concat=n={len(keep)}:v=1:a=1[v][a]")

    vargs, aargs, extra = _CUT_CODECS.get(path.suffix.lower(), _CUT_CODECS[".mp4"])
    out_path = path.with_name(f"{path.stem}.sbcut{path.suffix}")

    # Carry the thumbnail over untouched
    thumb_args = []
    if thumb_idx is not None:
        thumb_args = ["-map", f"0:{thumb_idx}", "-c:v:1", "copy", "-disposition:v:1", "attached_pic"]
    elif path.suffix.lower() == ".mkv":
        thumb_args = ["-map", "0:t?", "-c:t", "copy"]  # mkv stores covers as attachments

    # The filter script goes through a file so long segment lists can't hit the command-line limit
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as fh:
        fh.write(";\n".join(parts))
        script_path = fh.name

    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(path),
        "-filter_complex_script", script_path,
        "-map", "[v]", "-map", "[a]",
        *vargs, *aargs, *extra,
        *thumb_args,
        "-map_chapters", "-1",
        str(out_path),
    ]
    print(f"[SPONSORBLOCK] Cutting {len(cuts)} segment(s) out of {path.name} (re-encoding)...")
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        stdout, _ = await proc.communicate()
    finally:
        os.unlink(script_path)

    if proc.returncode != 0:
        out_path.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg sponsor cut failed: {stdout.decode(errors='ignore')[-500:]}")

    os.replace(out_path, path)

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

    output_path = _expected_output_path(title, getattr(config_module, "CONFIG", {}))
    if output_path is not None and output_path.is_file():
        print(f"[DOWNLOAD] Skipping existing file: {output_path}")
        database.update_status(url, "downloaded")
        return {
            'status': 'skipped',
            'reason': 'file_exists',
            'saved_bytes': 0,
        }

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
    ffmpeg_resolved = None
    if ffmpeg_location:
        if str(ffmpeg_location).startswith(".env/"):
            ffmpeg_location = os.environ.get(str(ffmpeg_location).removeprefix(".env/"))
        if ffmpeg_location:
            ffmpeg_path = _resolve_project_path(ffmpeg_location)
            if ffmpeg_path.exists():
                ffmpeg_resolved = ffmpeg_path
                args.extend(["--ffmpeg-location", str(ffmpeg_path)])

    sb_precise_cut = False
    output_paths_file = None
    if config.get("sponsorblock_enabled"):
        sb_categories = config.get("sponsorblock_categories", [])
        if isinstance(sb_categories, list) and sb_categories:
            sb_arg = ",".join(sb_categories)
        else:
            print("[DOWNLOAD] SponsorBlock is enabled but no categories are specified. Setting to remove all (Default).")
            sb_arg = "all"

        if config.get("sb_precise_cut", True):
            # Mark only; we cut afterwards with a re-encode so A/V stays in sync
            sb_precise_cut = True
            args.extend(["--sponsorblock-mark", sb_arg, "--embed-chapters"])
            fd, output_paths_file = tempfile.mkstemp(suffix=".txt")
            os.close(fd)
            args.extend(["--no-simulate", "--print-to-file", "after_move:filepath", output_paths_file])
        else:
            args.extend(["--sponsorblock-remove", sb_arg])
            # force keyframe option to stop timestamp shifting to next I-frame when removing segments
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

    if sb_precise_cut:
        try:
            with open(output_paths_file, encoding="utf-8") as fh:
                downloaded_files = [ln.strip() for ln in fh if ln.strip()]
        finally:
            os.unlink(output_paths_file)
        for downloaded in downloaded_files:
            await _cut_sponsors_precisely(downloaded, ffmpeg_resolved)

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