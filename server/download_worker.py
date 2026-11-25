# server/download_worker.py
 
import asyncio
import json
import math
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from collections import deque
from fractions import Fraction
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from dotenv import load_dotenv
from tqdm import tqdm
 
from . import database as db
from . import config as config_module
from .logger import Log, Logger
 
PROJECT_ROOT = Path(__file__).resolve().parents[1]
 
env = load_dotenv()
 
_EPISODE_PREFIX_PATTERNS = (
    re.compile(r'(?i)\bS(?P<season>\d{1,2})\s*E(?P<episode>\d{1,3})\b'),
    re.compile(r'(?i)\bEP(?:ISODE)?\.?\s*(?P<episode>\d{1,3})\b'),
    re.compile(r'(?i)(?<!\w)#\s*(?P<episode>\d{1,3})\b'),
)
 
_RUNTIME_TITLE_CACHE = {}
 
_SB_CHAPTER_PREFIX = f"{'[SponsorBlock]':<15}"
MARKER_DOWNLOAD  = f"{'[DOWNLOAD]':<15}"
MARKER_ENCODE    = f"{'[ENCODE]':<15}"
MARKER_NORMALIZE = f"{'[NORMALIZE]':<15}"
MARKER_SEARCH    = f"{'[SEARCH]':<15}"
MARKER_SOURCE    = f"{'[SOURCE]':<15}"
MARKER_INFO      = f"{'[INFO]':<15}"
MARKER_FFPROBE   = f"{'[FFPROBE]':<15}"
MARKER_TITLE     = f"{'[TITLE]':<15}"
MARKER_DATABANK  = f"{'[DATABANK]':<15}"
MARKER_POSTPROCESS = f"{'[POSTPROCESS]':<15}"
YTDLP_PROGRESS_MARKER = "[download]"
_FINAL_CONTAINER = "mkv"            # holds AV1/HEVC + Opus + srt + chapters + cover cleanly
_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
_DATABANK_LOCK = threading.Lock()
_DOWNLOAD_SLOTS = None              # created lazily, see _get_download_slots()
_SRT_TIME_RE = re.compile(r"(\d+):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d+):(\d{2}):(\d{2})[,.](\d{3})")

# Size policy of the final encode (see _encode_to_size_target)
_DEFAULT_CRF = {"av1": 30, "hevc": 25, "h264": 22}
_CRF_HARD_LIMIT = {"av1": 63, "hevc": 51, "h264": 51}        # highest CRF the encoder accepts
# ln(size) lost per +1 CRF. Only a first guess (av1: measured ~0.05 on test clips, x264/x265: size halves per ~6 CRF);
# the search measures the real slope.
_DEFAULT_SLOPE = {"av1": 0.05, "hevc": 0.115, "h264": 0.115}
_ON_SIZE_FAIL_CHOICES = ("keep_source", "fail", "encode_anyway")
_SEARCH_MAX_TRIALS = 4
_SEARCH_SAFETY = 0.95         # the search wants an estimate <= 90 % of the source. Independently encoded short clips run a few % below a real
                              # encode (measured: ~9 % with 5 s clips), and a full encode that ends up larger costs hours, 10 % margin costs ~1 CRF.
_SKIP_MARGIN = 1.15           # estimate at max_crf this far above the limit -> don't even start the full encode
_ABORT_MIN_FRACTION = 0.30    # earliest progress at which a hopeless full encode may be stopped
_ABORT_MIN_DURATION = 600     # ... and only for videos this long (s); the muxer buffers a few seconds of output, which skews the live size of short files
_ABORT_MARGIN = 1.15          # ... when its projected size exceeds the limit by this factor
_SAMPLE_MAX_SHARE = 0.40      # sample clips may cover at most this share of the video
_SAMPLE_MIN_SECONDS = 3.0
_PROGRESS_KEYS = {"frame", "fps", "bitrate", "total_size", "out_time_us", "out_time_ms", "out_time",
                  "dup_frames", "drop_frames", "speed", "progress"}
_WARNED_CONFIG = set()
 


def _build_ytdlp_auth_args(config, silence="quiet"):
    """
    Build the JavaScript runtime and authentication arguments used by
    the yt-dlp CLI. These arguments are shared by title fetching and
    actual downloads so both use the exact same configuration.

    # Parameters:
    - config: A dictionary containing the configuration settings.
    - silence: A string indicating the level of silence for yt-dlp output.
      - "all": Suppresses all output.
      - "quiet": Suppresses most output but allows warnings and errors.
      - "not-js": Suppresses everything but the JavaScript runtime output.
      - "browser": Suppresses everything but browser-related output.
    """

    def print_debug(msg, level="all"):
        # Determines whether the message should be printed based on silence level.
        levels = {"all": 5, "quiet": 3, "not-js": 1, "browser": 1}
        if levels.get(level, 0) >= levels.get(silence, 0):
            print(msg)

    args = []

    # JavaScript runtime
    js_runtime = config.get("js_runtime_loc")

    if isinstance(js_runtime, str) and js_runtime.startswith(".env/"):
        env_name = js_runtime.removeprefix(".env/")
        js_runtime = os.environ.get(env_name)
        print_debug(
            f"[YT-DLP] JS runtime environment variable {env_name}: {js_runtime!r}",
            level="not-js"
        )
    else:
        print_debug(
            f"[YT-DLP] JS runtime config: {js_runtime!r}",
            level="not-js"
        )

    if js_runtime and str(js_runtime).lower() != "none":
        js_runtime_path = _resolve_project_path(js_runtime)

        if js_runtime_path.is_dir():
            js_runtime_path /= "node.exe" if os.name == "nt" else "node"

        print_debug(
            f"[YT-DLP] Resolved JS runtime: {js_runtime_path}",
            level="not-js"
        )
        print_debug(
            f"[YT-DLP] JS runtime exists: {js_runtime_path.is_file()}",
            level="not-js"
        )

        if js_runtime_path.is_file():
            args.extend([
                "--js-runtimes",
                f"node:{js_runtime_path}",
                "--remote-components",
                "ejs:github",
            ])
        else:
            print_debug(
                f"[YT-DLP] WARNING: JavaScript runtime not found: "
                f"{js_runtime_path}",
                level="quiet"
            )

    # YouTube player client configuration
    # Excludes web_embedded, which was returning formats without usable URLs.
    args.extend([
        "--extractor-args",
        "youtube:player_client=default,-web_embedded",
    ])

    print_debug(
        "[YT-DLP] YouTube player client: default (excluding web_embedded)",
        level="not-js"
    )

    # Browser cookies
    if config.get("cookies_from_browser"):
        browser_name = config.get("browser_name", "firefox")

        print_debug(
            f"[YT-DLP] Using browser cookies from: {browser_name}",
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
                f"[YT-DLP] Using cookies file: {cookies_path}",
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


async def _get_video_resolution(video_path, ffprobe_path):
    """Return the first video stream resolution using ffprobe."""
    try:
        proc = await asyncio.create_subprocess_exec(
            str(ffprobe_path),
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "json",
            str(video_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
    except OSError as error:
        print(f"{MARKER_FFPROBE} Could not start ffprobe for {video_path}: {error}")
        return None

    if proc.returncode != 0:
        print(f"{MARKER_FFPROBE} Failed to get resolution for {video_path} with exit code {proc.returncode}")
        error_output = stderr.decode("utf-8", errors="replace").strip()
        if error_output:
            print(f"{MARKER_FFPROBE} stderr:\n{error_output}")
        return None

    try:
        streams = json.loads(stdout.decode("utf-8")).get("streams", [])
        width, height = streams[0]["width"], streams[0]["height"]
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
        print(f"{MARKER_FFPROBE} Invalid resolution output for {video_path}: {error}")
        return None
    return f"{width}x{height}"


# ---------------------------------------------------------------------------
# URL validation and name databank (S2, H8)
# ---------------------------------------------------------------------------
 
def _normalize_url(url):
    """Return a clean https YouTube video URL, or None if `url` isn't one.
 
    Besides de-duplicating (`watch?v=X&list=...` == `watch?v=X`), this is the guard
    against option injection: nothing that doesn't start with http(s)://<youtube host> gets through (S2)."""
    try:
        parsed = urlparse(str(url).strip())
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or parsed.hostname not in _YOUTUBE_HOSTS:
        return None
 
    video_id = None
    if parsed.hostname == "youtu.be":
        video_id = parsed.path.lstrip("/")
    elif parsed.path == "/watch":
        video_id = (parse_qs(parsed.query).get("v") or [""])[0]
    elif parsed.path.startswith(("/shorts/", "/live/")):
        video_id = parsed.path.split("/")[2] if parsed.path.count("/") >= 2 else ""
 
    if video_id and re.fullmatch(r"[\w-]{11}", video_id):
        return f"https://www.youtube.com/watch?v={video_id}"
    return None
 
 
def _databank_key(url):
    """Databank key: the video ID for YouTube links, otherwise the stripped input.
    Reads and writes MUST use the same key (H8: they didn't before)."""
    url = url.strip()
    parsed = urlparse(url)
    if parsed.hostname == "youtu.be":
        return parsed.path.lstrip("/")
    video_id = (parse_qs(parsed.query).get("v") or [None])[0]
    return video_id or url
 
 
def _load_title_from_databank(url):
    databank_path = config_module.NAME_DATABANK_PATH
    if not databank_path.is_file():
        return None
    try:
        with _DATABANK_LOCK, open(databank_path, 'r', encoding='utf-8') as f:
            databank = json.load(f)
    except Exception:
        return None
    title = databank.get(_databank_key(url))
    # yt-dlp prints "NA" for unknown titles; treat that as a miss so it gets re-fetched
    if not title or title.strip().upper() in {"NA", "N/A", "NONE"}:
        return None
    return title
 
 
def _write_title_to_databank(url, title):
    """Add one title to the databank: thread-safe, atomic, and never overwrites a file it couldn't read."""
    path = Path(config_module.NAME_DATABANK_PATH)
    with _DATABANK_LOCK:
        databank = {}
        if path.is_file():
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    databank = json.load(f)
            except Exception as e:
                print(f"{MARKER_DATABANK} Unreadable, not overwriting it:", e)
                return
        databank[_databank_key(url)] = title
        tmp_path = path.with_suffix(".json.tmp")
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(databank, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)  # atomic: a crash can't leave half a file
 
 
# ---------------------------------------------------------------------------
# Title fetching
# ---------------------------------------------------------------------------
 
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
        args = ["yt-dlp", "--get-title", "--no-download", "--quiet"]
        if conf_args is None:
            conf_args = _build_ytdlp_auth_args(config)
        args.extend(conf_args)
        args.extend(["--", url])  # "--": the URL can never be read as an option (S2)
 
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
        print(f"{MARKER_TITLE} yt-dlp failed with exit code {proc.returncode}")
        error_output = stderr.decode("utf-8", errors="replace").strip()
        if error_output:
            print(f"{MARKER_TITLE} stderr:\n{error_output}")
    except Exception as e:
        print(f"{MARKER_TITLE} Async fetch failed for {url}: {e}")
 
    return url
 
 
async def _fetch_playlist_title_async(url, conf_args=None):
    """Fetches the title of a playlist using yt-dlp efficiently."""
    if conf_args is None:
        conf_args = _build_ytdlp_auth_args(getattr(config_module, "CONFIG", {}))
 
    args = [
        "yt-dlp",
        "--playlist-items", "0",
        "--print", "playlist_title",
        "--quiet"
    ]
    args.extend(conf_args)
    args.extend(["--", url])  # (S2)
 
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
        print(f"{MARKER_TITLE} yt-dlp failed to fetch playlist title with exit code {proc.returncode}")
        error_output = stderr.decode("utf-8", errors="replace").strip()
        if error_output:
            print(f"{MARKER_TITLE} stderr:\n{error_output}")
        return url
 
 
# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
 
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
 
 
def _format_percentage(value, width=None):
    text = f"{float(value):.0%}"
    return text.ljust(width) if width is not None else text


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
 
 
def _build_format_selector(config, relaxed=False):
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
 
    # No codec preference: we re-encode anyway, so the best (highest quality) source wins.
    # SDR only, because the final encode doesn't tone-map and HDR would look washed out.
    # "=?" lets formats with an unknown dynamic range pass.
    video = f"bv*[height<={max_height}][dynamic_range=?SDR]"
    selector = (
        f"{video}+ba[language_preference>=10]/"
        f"{video}+ba/"
        f"b[height<={max_height}][dynamic_range=?SDR]"
    )
    if relaxed:
        # Retry-ladder fallback, widest last: drop the SDR filter, then the height cap,
        # then accept anything. This is the "catch-all" the old selector was missing.
        selector += f"/bv*[height<={max_height}]+ba/bv*+ba/b"
    return selector
 
 
def _resolve_ffmpeg_location(config):
    """Configured ffmpeg path (supports the .env/ syntax) or None if unset/missing."""
    ffmpeg_location = config.get("ffmpeg_loc")
    if ffmpeg_location and str(ffmpeg_location).startswith(".env/"):
        ffmpeg_location = os.environ.get(str(ffmpeg_location).removeprefix(".env/"))
    if not ffmpeg_location:
        return None
    ffmpeg_path = _resolve_project_path(ffmpeg_location)
    return ffmpeg_path if ffmpeg_path.exists() else None
 
 
def _unique_path(path):
    """Never overwrite an existing library file: append ' (2)', ' (3)', ... instead."""
    path = Path(path)
    candidate, n = path, 2
    while candidate.exists():
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        n += 1
    return candidate
 
 
def _get_download_slots():
    """One semaphore shared by the queue worker and runall(), so together they
    respect max_concurrent_downloads (H12)."""
    global _DOWNLOAD_SLOTS
    if _DOWNLOAD_SLOTS is None:
        try:
            limit = max(1, int(config_module.CONFIG.get("max_concurrent_downloads", 1)))
        except (TypeError, ValueError):
            limit = 1
        _DOWNLOAD_SLOTS = asyncio.Semaphore(limit)
    return _DOWNLOAD_SLOTS
 
 
def _classify_ytdlp_failure(output):
    """Rough failure category from yt-dlp's output: 'format', 'post' or 'other'."""
    # only look at ERROR lines; warnings about thumbnails etc. are often harmless
    errors = "\n".join(l for l in output.splitlines() if l.startswith("ERROR"))
    text = (errors or output).lower()
    if re.search(r"requested format is not available|no video formats found|only images are available|format.*not available", text):
        return "format"
    if re.search(r"postprocessing|ffmpeg|thumbnail|subtitle|embed|unable to convert|conversion failed", text):
        return "post"
    return "other"
 

def _video_codec_args(config, crf=None):
    """(encoder args, pixel format) for the single final encode.
    Software encoders at slow presets give the smallest files for a given quality.
    `crf` overrides video_crf (used by the CRF search and the corrective re-encodes)."""
    codec = str(config.get("video_codec", "h264")).lower()
    default_pixel_format = "yuv420p10le" if codec in ("av1", "hevc") else "yuv420p"
    pixel_format = str(config.get("video_pixel_format", default_pixel_format)).lower()
    if pixel_format not in {"yuv420p", "yuv420p10le"}:
        raise ValueError(
            f"Unknown video_pixel_format {pixel_format!r} "
            "(use yuv420p for 8-bit or yuv420p10le for 10-bit)"
        )
    if crf is None:
        crf = config.get("video_crf")
    preset = config.get("video_preset")
 
    if codec == "av1":
        return [
            "-c:v", "libsvtav1",
            "-preset", str(4 if preset is None else preset),
            "-crf", str(_DEFAULT_CRF["av1"] if crf is None else crf),
            "-svtav1-params", "tune=0",  # tune for visual quality instead of PSNR
        ], pixel_format
    if codec == "hevc":
        return [
            "-c:v", "libx265",
            "-preset", str("slow" if preset is None else preset),
            "-crf", str(_DEFAULT_CRF["hevc"] if crf is None else crf),
            "-tag:v", "hvc1",  # Apple players need this tag
            "-x265-params", "log-level=error",
        ], pixel_format
    if codec == "h264":
        return [
            "-c:v", "libx264",
            "-preset", str("slower" if preset is None else preset),
            "-crf", str(_DEFAULT_CRF["h264"] if crf is None else crf),
        ], pixel_format
    raise ValueError(f"Unknown video_codec {codec!r} (use av1, hevc or h264)")
 
 
def _audio_codec_args(config):
    """Opus is clearly more efficient than AAC at 80-96 kbps."""
    bitrate = str(config.get("audio_bitrate", "96k"))
    return ["-c:a", "libopus", "-b:a", bitrate, "-vbr", "on", "-application", "audio"]


def _warn_once(key, message):
    """Print a configuration warning only once per process (the worker re-reads CONFIG for every video)."""
    if key not in _WARNED_CONFIG:
        _WARNED_CONFIG.add(key)
        print(message)


def _config_get(config, name, default=None):
    """config.get() that tolerates a differently cased key.
    TOML keys are case-sensitive: `max_CRF = 32` was silently ignored by config.get("max_crf")."""
    if name in config:
        return config[name]
    for key, value in config.items():
        if str(key).lower() == name.lower():
            _warn_once(f"case:{name}", f"[CONFIG] Key {key!r} should be spelled {name!r}; using it anyway.")
            return value
    return default


def _config_number(config, name, default, kind=float, minimum=None):
    """Read a numeric setting; a missing or invalid value falls back to `default` with a warning."""
    raw = _config_get(config, name)
    if raw is None:
        return default
    try:
        value = kind(raw)
        if kind is float and not math.isfinite(value):
            raise ValueError("not finite")
    except (TypeError, ValueError):
        _warn_once(f"bad:{name}", f"[CONFIG] {name} = {raw!r} is not a valid number; using {default}.")
        return default
    if minimum is not None and value < minimum:
        _warn_once(f"low:{name}", f"[CONFIG] {name} = {raw!r} is below the minimum of {minimum}; using {default}.")
        return default
    return value


def _resolve_crf_range(config):
    """(first CRF to try, highest CRF allowed). The first CRF is the user's preferred quality (video_crf);
    max_crf is a hard ceiling that the search and every retry must respect."""
    codec = str(config.get("video_codec", "h264")).lower()
    hard_limit = _CRF_HARD_LIMIT.get(codec, 51)
    start = min(_config_number(config, "video_crf", _DEFAULT_CRF.get(codec, 22), int, 0), hard_limit)
    if _config_get(config, "max_crf") is None:
        ceiling = min(start + 8, hard_limit)
        _warn_once("max_crf-unset", f"[CONFIG] max_crf is not set; allowing CRF {start}..{ceiling}.")
    else:
        ceiling = _config_number(config, "max_crf", min(start + 8, hard_limit), int, 0)
        if ceiling > hard_limit:
            _warn_once("max_crf-high", f"[CONFIG] max_crf {ceiling} exceeds the {codec} encoder limit; using {hard_limit}.")
            ceiling = hard_limit
    if ceiling < start:
        _warn_once("max_crf-low", f"[CONFIG] max_crf ({ceiling}) is below video_crf ({start}); using {start}.")
        ceiling = start
    return start, ceiling


def _resolve_on_size_fail(config):
    value = str(config.get("on_size_fail", "keep_source")).strip().lower()
    if value not in _ON_SIZE_FAIL_CHOICES:
        _warn_once("on_size_fail", f"[CONFIG] on_size_fail = {value!r} is invalid (use one of "
                                    f"{', '.join(_ON_SIZE_FAIL_CHOICES)}); using 'keep_source'.")
        return "keep_source"
    return value


def _parse_bitrate(value):
    """'96k' / '96000' / '1.5M' -> bits per second, or None."""
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kKmM]?)\s*", str(value))
    if not match:
        return None
    return float(match.group(1)) * {"": 1, "k": 1_000, "m": 1_000_000}[match.group(2).lower()]


def _format_duration(seconds):
    if seconds is None or seconds != seconds or seconds < 0:
        return "?"
    seconds = int(round(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{f'{secs}s':<10}"


def _pick_sample_windows(keep, count, length):
    """Evenly spread (source_start, seconds) clips over the KEPT part of the video, each inside one kept range
    (so a clip never contains a removed SponsorBlock segment). Returns [] when the video is too short."""
    total = sum(b - a for a, b in keep)
    if count < 1 or total <= 0:
        return []
    length = min(float(length), total * _SAMPLE_MAX_SHARE / count)
    if length < _SAMPLE_MIN_SECONDS:
        return []
    windows = []
    for i in range(count):
        out_start = min(max(total * (i + 0.5) / count - length / 2, 0.0), total - length)
        offset = 0.0
        for n, (a, b) in enumerate(keep):
            if out_start < offset + (b - a) or n == len(keep) - 1:
                src_start = a + max(out_start - offset, 0.0)
                if src_start + length > b:          # keep the clip inside this kept range
                    src_start = max(a, b - length)
                seconds = min(length, b - src_start)
                if seconds >= _SAMPLE_MIN_SECONDS and (not windows or src_start >= windows[-1][0] + windows[-1][1]):
                    windows.append((src_start, seconds))
                break
            offset += b - a
    return windows


def _crf_for_ratio(points, goal, default_slope):
    """CRF at which size/reference is expected to equal `goal`.
    ln(size) is close to linear in CRF, so two measurements are interpolated (or extrapolated) in log space;
    with a single measurement `default_slope` (ln-size lost per CRF step) stands in for the unknown slope."""
    def ln(ratio):
        return math.log(max(ratio, 1e-6))

    items = sorted(points.items())
    ln_goal = ln(goal)
    if len(items) >= 2:
        under = [it for it in items if it[1] <= goal]
        over = [it for it in items if it[1] > goal]
        pair = None
        if under and over:                          # prefer the two points that enclose the goal
            lowest_under = min(under)
            below = [it for it in over if it[0] < lowest_under[0]]
            if below:
                pair = (max(below), lowest_under)
        if pair is None:                            # otherwise the two points closest to the goal
            pair = tuple(sorted(sorted(items, key=lambda it: abs(ln(it[1]) - ln_goal))[:2]))
        (c1, r1), (c2, r2) = pair
        if c2 > c1:
            slope = (ln(r1) - ln(r2)) / (c2 - c1)
            if slope >= 0.01:                       # a flat or inverted curve is measurement noise
                return c1 + (ln(r1) - ln_goal) / slope
    crf, ratio = min(items, key=lambda it: abs(ln(it[1]) - ln_goal))
    return crf + (ln(ratio) - ln_goal) / default_slope


class _SizeModel:
    """crf -> size / reference size. `samples` are estimates from short clips, `full` are real (or projected)
    results of full encodes. A full result at the same CRF calibrates every sample estimate."""

    def __init__(self, default_slope):
        self.default_slope = default_slope
        self.samples = {}
        self.full = {}

    def bias(self):
        common = [c for c in self.full if c in self.samples]
        if not common:
            return 1.0
        return math.exp(sum(math.log(self.full[c] / self.samples[c]) for c in common) / len(common))

    def points(self):
        bias = self.bias()
        points = {crf: ratio * bias for crf, ratio in self.samples.items()}
        points.update(self.full)
        return points

    def crf_for(self, goal):
        return _crf_for_ratio(self.points(), goal, self.default_slope)


def _ffmpeg_resource_args(config):
    """Keep decode/filter buffering bounded for long AV1 sources."""
    try:
        threads = int(config.get("ffmpeg_threads", 0))
    except (TypeError, ValueError) as e:
        raise ValueError("ffmpeg_threads must be a non-negative integer") from e
    if threads < 0:
        raise ValueError("ffmpeg_threads must be a non-negative integer")
    return [
        "-threads", str(threads),
        "-filter_threads", str(threads),
        "-filter_complex_threads", str(threads),
    ]


def _progress_elapsed(progress_data, duration):
    """Convert FFmpeg progress timestamps to bounded seconds."""
    timestamp = progress_data.get("out_time")
    if timestamp:
        try:
            hours, minutes, seconds = timestamp.split(":")
            elapsed = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        except (ValueError, TypeError):
            elapsed = None
        if elapsed is not None:
            return min(max(elapsed, 0.0), duration)

    for key in ("out_time_us", "out_time_ms"):
        value = progress_data.get(key)
        if value is None:
            continue
        try:
            # FFmpeg names both fields in time-base units of microseconds.
            elapsed = float(value) / 1_000_000
        except (ValueError, TypeError):
            continue
        return min(max(elapsed, 0.0), duration)
    return None


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
    cmd = [ffprobe, "-v", "error", *flags, "-of", "json", str(path)]
    _print_command(cmd)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    return json.loads(stdout.decode("utf-8", errors="replace") or "{}")
 
 
async def _stop_process(proc):
    """Stop a subprocess without hanging if it ignores the first signal."""
    if proc.returncode is not None:
        return
 
    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
 
 
def _merge_ranges(ranges):
    merged = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 0.001:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged
 
 
def _remap_time(t, keep):
    """Map a timestamp of the ORIGINAL video onto the cut video.
    `keep` is the list of kept (start, end) ranges; times inside a removed range snap to the cut point."""
    offset = 0.0
    for start, end in keep:
        if t <= start:
            return offset
        if t < end:
            return offset + (t - start)
        offset += end - start
    return offset
 
 
def _shift_srt(text, keep):
    """Re-time SRT cues to the cut timeline; cues that lie fully inside a removed range are dropped."""
    def to_seconds(h, m, s, ms):
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000
 
    def to_stamp(t):
        ms = int(round(t * 1000))
        h, ms = divmod(ms, 3600000)
        m, ms = divmod(ms, 60000)
        s, ms = divmod(ms, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
 
    cues = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").strip()):
        lines = block.split("\n")
        for n, line in enumerate(lines):
            match = _SRT_TIME_RE.search(line)
            if not match:
                continue
            groups = match.groups()
            start = _remap_time(to_seconds(*groups[:4]), keep)
            end = _remap_time(to_seconds(*groups[4:]), keep)
            body = "\n".join(lines[n + 1:])
            if end - start > 0.05 and body.strip():
                cues.append(f"{len(cues) + 1}\n{to_stamp(start)} --> {to_stamp(end)}\n{body}")
            break
    return "\n\n".join(cues) + "\n"
 
 
def _build_chapter_metadata(chapters, keep):
    """FFMETADATA text with the ORIGINAL (non-SponsorBlock) chapters re-timed to the cut video, or None.
    yt-dlp splits chapters around SponsorBlock marks; pieces with the same title that
    become adjacent after the cut are merged back together."""
    merged = []  # [start, end, title]
    for chapter in chapters:
        title = (chapter.get("tags") or {}).get("title", "")
        if title.startswith(_SB_CHAPTER_PREFIX):
            continue
        start = _remap_time(float(chapter["start_time"]), keep)
        end = _remap_time(float(chapter["end_time"]), keep)
        if end - start < 0.5:
            continue
        if merged and merged[-1][2] == title and start - merged[-1][1] <= 0.5:
            merged[-1][1] = end
        else:
            merged.append([start, end, title])
    if not merged:
        return None
 
    lines = [";FFMETADATA1"]
    for start, end, title in merged:
        safe_title = re.sub(r"([=;#\\\n])", r"\\\1", title)  # ffmetadata escaping
        lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={int(start * 1000)}", f"END={int(end * 1000)}", f"title={safe_title}"]
    return "\n".join(lines) + "\n"
 
 
def _build_filter_graph(
    keep,
    video_idx,
    pix_fmt,
    audio_filter=None,
    audio_only=False,
    frame_rate=None,
):
    """filter_complex text: trim every kept range from video AND audio with the same
    timestamps (no A/V drift), concat them, then convert pixel format / filter audio.
    audio_only=True is used for the loudness measuring pass."""
    parts, labels = [], []
    for i, (a, b) in enumerate(keep):
        if not audio_only:
            parts.append(f"[0:{video_idx}]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[v{i}]")
        parts.append(f"[0:a:0]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS[a{i}]")
        labels.append(f"[a{i}]" if audio_only else f"[v{i}][a{i}]")
    if audio_only:
        parts.append(f"{''.join(labels)}concat=n={len(keep)}:v=0:a=1[ac]")
    else:
        parts.append(f"{''.join(labels)}concat=n={len(keep)}:v=1:a=1[vc][ac]")
        video_filters = []
        if frame_rate is not None:
            video_filters.append(f"fps={frame_rate}")
        video_filters.append(f"format={pix_fmt}")
        parts.append(f"[vc]{','.join(video_filters)}[v]")
    parts.append(f"[ac]{audio_filter or 'anull'}[a]")
    return ";\n".join(parts)


def _source_frame_rate(stream):
    """Return a sane constant frame rate for encoders that reject invalid timing."""
    for key in ("r_frame_rate", "avg_frame_rate"):
        value = stream.get(key)
        if not value or value == "0/0":
            continue
        try:
            rate = float(Fraction(str(value)))
        except (ValueError, ZeroDivisionError):
            continue
        if 0 < rate <= 240:
            return f"{rate:.6f}".rstrip("0").rstrip(".")
    return None
 
 
def _resolve_frame_rate(stream, config):
    """Return the source rate capped by max_video_fps, or None when no rate is usable."""
    source_rate = _source_frame_rate(stream)
    maximum = _config_number(config, "max_video_fps", 0.0, float, 0.0)
    if maximum <= 0 or source_rate is None:
        return source_rate
    return f"{min(float(source_rate), maximum):.6f}".rstrip("0").rstrip(".")


def _video_frame_count(stream):
    """Return a stream's frame count when ffprobe provided one."""
    for key in ("nb_frames", "nb_read_frames"):
        value = stream.get(key)
        try:
            frame_count = int(value)
        except (TypeError, ValueError):
            continue
        if frame_count > 0:
            return frame_count
    return None


async def _run_capture(cmd):
    """Run a command to completion; returns (exit code, stderr text). Cancel-safe."""
    _print_command(cmd)
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        _, stderr = await proc.communicate()
    except asyncio.CancelledError:
        await _stop_process(proc)
        raise
    return proc.returncode, stderr.decode("utf-8", errors="replace")
 
 
def _print_command(cmd):
    if config_module.CONFIG.get("print_command"):
        print(*cmd)
        print("============================")
 
 
def _is_progress_key(key):
    return key in _PROGRESS_KEYS or key.startswith("stream_")


async def _run_ffmpeg_stream(cmd, on_progress=None):
    """Run ffmpeg (started with `-progress pipe:1`) and call on_progress(dict) for every progress block.
    If the callback returns True the process is stopped. Returns (exit code, last output lines, aborted)."""
    _print_command(cmd)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    block = {}
    output_tail = deque(maxlen=30)
    aborted = False
    try:
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                break
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            key, sep, value = line.partition("=")
            if not sep or not _is_progress_key(key):
                output_tail.append(line)             # error text, never swallowed as "progress"
                continue
            block[key] = value
            if key != "progress":
                continue
            snapshot, block = block, {}
            if on_progress is not None and on_progress(snapshot):
                aborted = True
                await _stop_process(proc)
                break
        returncode = await proc.wait()
    except asyncio.CancelledError:
        # a timeout/cancel must not leave an orphaned encode running (H1)
        await _stop_process(proc)
        raise
    return returncode, "".join(output_tail), aborted


def _progress_size(progress, out_path):
    """Bytes written so far: the real file size if we know the file, else ffmpeg's own counter."""
    if out_path is not None:
        try:
            return Path(out_path).stat().st_size
        except OSError:
            pass
    try:
        return int(progress.get("total_size"))
    except (TypeError, ValueError):
        return None


def _progress_fps(progress):
    try:
        fps = float(progress.get("fps"))
    except (TypeError, ValueError):
        return None
    return fps if fps > 0 else None


def _progress_speed(progress):
    speed = (progress.get("speed") or "").strip()
    return "" if speed in ("", "N/A") else speed


async def _measure_samples(ctx, windows, crf, label):
    """Encode the short sample clips at `crf` (same codec, preset and filters as the real encode) and
    extrapolate the size of the finished file. Returns a dict with the estimate and timing statistics."""
    config = ctx["config"]
    vargs, _ = _video_codec_args(config, crf=crf)
    filters = ([f"fps={ctx['frame_rate']}"] if ctx["frame_rate"] else []) + [f"format={ctx['pix_fmt']}"]
    total_seconds = sum(seconds for _, seconds in windows)
    started = time.monotonic()
    sample_bytes = 0
    frames = 0
    bar_format = "{desc}: {percentage:5.1f}%|{bar}| [{elapsed}<{remaining}] {postfix}"
    with tqdm(
        total=total_seconds, desc=f"{label} CRF {crf}", unit="s",
        dynamic_ncols=True, bar_format=bar_format, file=sys.stdout, leave=False,
    ) as bar:
        for number, (start, seconds) in enumerate(windows, 1):
            clip = ctx["workdir"] / f"sample_crf{crf}_{number}.mkv"
            cmd = [
                ctx["ffmpeg"], "-y", "-hide_banner", "-loglevel", "error",
                *_ffmpeg_resource_args(config),
                "-progress", "pipe:1", "-stats_period", "0.5", "-nostats",
                "-ss", f"{start:.3f}", "-t", f"{seconds:.3f}", "-i", str(ctx["path"]),
                "-map", f"0:{ctx['video_idx']}", "-sn", "-dn",
                "-vf", ",".join(filters),
                "-threads", str(config.get("ffmpeg_threads", 0)),
                *vargs, "-f", "matroska", str(clip),
            ]
            clip_done = 0.0
            clip_frames = 0

            def on_progress(progress):
                nonlocal clip_done, clip_frames
                elapsed = _progress_elapsed(progress, seconds)
                if elapsed is not None and elapsed > clip_done:
                    bar.update(elapsed - clip_done)
                    clip_done = elapsed
                try:
                    clip_frames = int(progress.get("frame"))
                except (TypeError, ValueError):
                    pass
                fps = _progress_fps(progress)
                bar.set_postfix_str(
                    f"clip {number}/{len(windows)} | {_progress_speed(progress)} "
                    f"{'' if fps is None else f'{fps:.1f}fps'}".rstrip(),
                    refresh=True,
                )
                return False

            returncode, tail, _ = await _run_ffmpeg_stream(cmd, on_progress)
            if returncode != 0:
                clip.unlink(missing_ok=True)
                raise RuntimeError(f"Sample encode failed (CRF {crf}, clip {number}/{len(windows)}):\n{tail[-800:]}")
            if clip_done < seconds:
                bar.update(seconds - clip_done)
            sample_bytes += clip.stat().st_size
            frames += clip_frames
            clip.unlink(missing_ok=True)

    wall = time.monotonic() - started
    predicted = sample_bytes / total_seconds * ctx["output_duration"] + ctx["audio_bytes"]
    return {
        "predicted": predicted,
        "ratio": predicted / ctx["ref_size"],
        "wall": wall,
        "seconds": total_seconds,
        "fps": frames / wall if wall > 0 and frames else None,
        # a full encode runs at about the same speed as the clips did
        "full_eta": wall * ctx["output_duration"] / total_seconds,
    }


async def _run_ffmpeg(cmd, duration, total_frames=None, desc="Encode", *,
                      out_path=None, ref_size=None, abort_above=None, snapshot_every=0.0):
    """Run the final encode with a live progress bar.

    Next to percent / ETA the bar shows speed, fps, the output size so far and the size it is heading for
    (relative to `ref_size`). Every `snapshot_every` seconds a permanent status line is written above the bar,
    so the log can be scrolled back later. If `abort_above` (bytes) is given and the projected final size
    stays above it once enough of the video is done, the encode is stopped (it cannot succeed anyway).
    Returns a statistics dict; raises RuntimeError if ffmpeg fails."""
    use_frame_progress = total_frames is not None and total_frames > 0
    progress_unit = "frame" if use_frame_progress else "s"
    progress_unit_label = "frames" if use_frame_progress else "s"
    bar_format = (
        "{desc}: {percentage:6.2f}%|{bar}| "
        "{n:.0f}/{total:.0f} " + progress_unit_label + " "
        "[{elapsed}<{remaining}] {postfix}"
    )
    started = time.monotonic()
    stats = {"aborted": False, "projected": None, "fraction": 0.0, "size": None, "frames": 0, "wall": 0.0}
    last_snapshot = started
    last_position = 0.0

    with tqdm(
        total=max(total_frames if use_frame_progress else duration, 1),
        desc=desc,
        unit=progress_unit,
        dynamic_ncols=True,
        bar_format=bar_format,
        file=sys.stdout,
        leave=True,
    ) as bar:
        def on_progress(progress):
            nonlocal last_position, last_snapshot
            elapsed = _progress_elapsed(progress, duration)
            try:
                frame = int(progress.get("frame"))
            except (TypeError, ValueError):
                frame = None

            if use_frame_progress and frame is not None:
                position = min(frame, bar.total)
                stats["frames"] = max(stats["frames"], frame)
            elif elapsed is not None:
                position = elapsed / duration * bar.total if duration > 0 else 0.0
            else:
                position = None
            if position is not None and position > last_position:
                bar.update(position - last_position)
                last_position = position

            fraction = bar.n / bar.total if bar.total else 0.0
            size = _progress_size(progress, out_path)
            projected = size / fraction if size and fraction >= 0.02 else None
            stats.update(fraction=fraction, size=size, projected=projected)

            fps = _progress_fps(progress)
            parts = [part for part in (_progress_speed(progress), None if fps is None else f"{fps:.1f}fps") if part]
            text = " ".join(parts)
            if size is not None:
                size_text = _format_bytes(size)
                if projected:
                    size_text += f" -> ~{_format_bytes(projected)}"
                    if ref_size:
                        size_text += f" ({projected / ref_size:.0%} of source)"
                text = f"{text} | {size_text}" if text else size_text
            bar.set_postfix_str(text, refresh=True)

            now = time.monotonic()
            if snapshot_every and now - last_snapshot >= snapshot_every:
                last_snapshot = now
                spent = now - started
                eta = spent * (1 - fraction) / fraction if fraction > 0 else None
                line = (f"[{time.strftime('%H:%M:%S')}] {desc}: {fraction:.1%} | elapsed {_format_duration(spent)}"
                        f" | ETA {_format_duration(eta)}")
                if size is not None:
                    line += f" | {_format_bytes(size)} written"
                if projected:
                    line += f" | projected {_format_bytes(projected)}"
                    if ref_size:
                        line += f" = {projected / ref_size:.0%} of source"
                tqdm.write(line, file=sys.stdout)

            if (abort_above is not None and projected is not None
                    and fraction >= _ABORT_MIN_FRACTION and projected > abort_above):
                stats["aborted"] = True
                return True
            return False

        returncode, tail, aborted = await _run_ffmpeg_stream(cmd, on_progress)
        stats["wall"] = time.monotonic() - started
        if aborted:
            tqdm.write(
                f"{MARKER_ENCODE} {desc}: stopped at {stats['fraction']:.0%}; projected size "
                f"{_format_bytes(stats['projected'])} is far above the limit "
                f"({_format_bytes(abort_above)} incl. margin).",
                file=sys.stdout,
            )
            return stats
        if returncode == 0:
            # Finish cleanly despite small timestamp/rounding differences.
            if bar.n < bar.total:
                bar.update(bar.total - bar.n)
            stats["fraction"] = 1.0

    if returncode != 0:
        raise RuntimeError(
            f"FFmpeg {desc} failed (exit {returncode}):\n"
            f"{tail[-1000:]}"
        )
    return stats


def _print_attempts(rows):
    """Table of everything that was measured or encoded for one video."""
    if not rows:
        return
    labels = {"sample": "CRF search (estimate)", "full": "full encode", "partial": "stopped early (proj.)"}
    print(f"{MARKER_ENCODE} Attempts:")
    print(f"  {'step':<22}{'CRF':>4}  {'size':>10}  {'of source':>9}  {'time':>10}")
    for kind, crf, size, ratio, wall in rows:
        print(f"  {labels[kind]:<22}{crf:>4}  {_format_bytes(size):>10}  {ratio:>9.0%}  {_format_duration(wall):>10}")


async def _keep_source(path, ffmpeg, out_path):
    """Use the untouched download as the result. Only a stream copy is needed if it is not an .mkv already."""
    out_path.unlink(missing_ok=True)
    if path.suffix.lower() == f".{_FINAL_CONTAINER}":
        return path
    # only stream types Matroska can hold (an mp4 may carry data/timecode tracks); text subtitles become srt
    try:
        rc, err = await _run_capture([
            ffmpeg, "-y", "-v", "error", "-i", str(path),
            "-map", "0:v", "-map", "0:a?", "-map", "0:s?", "-map", "0:t?", "-map_chapters", "0",
            "-c", "copy", "-c:s", "srt", str(out_path),
        ])
    except Exception as e:
        rc, err = 1, str(e)
    if rc != 0:
        out_path.unlink(missing_ok=True)
        raise RuntimeError(f"Could not copy {path.name} into {_FINAL_CONTAINER}: {err.strip()[-300:]}")
    return out_path


async def _encode_to_size_target(*, config, ctx, make_cmd, windows, out_path, total_frames):
    """Produce the final file at the best quality that is still SMALLER than the download.

    There is no size target. video_crf is the quality you want; the CRF is raised only as far as needed
    to end up below the source, and never above max_crf (the worst quality you accept).

    1. CRF search: short clips spread over the video are encoded at a few CRF values (minutes, not hours)
       to find the lowest CRF whose estimated size is clearly below the source.
    2. One full encode at that CRF, with live size projection. A hopeless encode is stopped early.
    3. If the finished file is still not smaller than the source, the real result calibrates the model and
       at most `max_encode_attempts` - 1 corrective encodes follow, still capped by `max_crf`.

    Returns (outcome, message); outcome is "accepted" (out_path holds the result), "rejected" (no
    acceptable encode; the caller applies `on_size_fail`) or "search_only"."""
    output_duration, ref_size = ctx["output_duration"], ctx["ref_size"]
    codec = str(config.get("video_codec", "h264")).lower()
    start_crf, max_crf = _resolve_crf_range(config)
    snapshot_every = _config_number(config, "stats_interval", 300.0, float, 0.0)

    if not config.get("avoid_size_increase", True):
        # size policy switched off: one encode at video_crf, whatever size comes out
        await _run_ffmpeg(
            make_cmd(start_crf), duration=output_duration, total_frames=total_frames,
            desc=f"Encode CRF {start_crf}", out_path=out_path, ref_size=ref_size, snapshot_every=snapshot_every,
        )
        return "accepted", f"avoid_size_increase is off; encoded once at CRF {start_crf}."

    max_attempts = _config_number(config, "max_encode_attempts", 3, int, 1)
    search_only = bool(config.get("crf_search_only", False))
    accept_oversized = _resolve_on_size_fail(config) == "encode_anyway"
    limit_ratio = 1.0                         # never publish more than the (cut-adjusted) download size
    goal = limit_ratio * _SEARCH_SAFETY       # the search wants an estimate clearly below the limit
    model = _SizeModel(_DEFAULT_SLOPE.get(codec, 0.1))
    rows = []

    def finish(outcome, message):
        _print_attempts(rows)
        return outcome, message

    # ---------------------------------------------------------------- 1. CRF search on short clips
    chosen = start_crf
    if windows:
        covered = sum(seconds for _, seconds in windows)
        print(f"{MARKER_SEARCH} Estimating size on {len(windows)} clip(s) ({covered:.0f}s of {output_duration:.0f}s), "
              f"at most {_SEARCH_MAX_TRIALS} CRF values + the ceiling if needed.")

        async def trial(crf, label):
            result = await _measure_samples(ctx, windows, crf, label)
            model.samples[crf] = result["ratio"]
            rows.append(("sample", crf, result["predicted"], result["ratio"], result["wall"]))
            speed = "" if result["fps"] is None else f" at {result['fps']:.1f} fps"
            ratio_text = _format_percentage(result["ratio"], width=6)
            tmp_marker = f"[{label.upper()}]"
            print(
                f"{tmp_marker:<15} CRF {str(crf):<3}: estimated {str(_format_bytes(result['predicted'])):<10} = {ratio_text} "
                f"of the source (aiming for <= {goal:.0%}) | {result['seconds']:.0f}s of video in "
                f"{_format_duration(result['wall'])}{speed} | full encode would take ~{_format_duration(result['full_eta'])}"
            )

        candidate = start_crf
        for number in range(1, _SEARCH_MAX_TRIALS + 1):
            await trial(candidate, f"Search {number}/{_SEARCH_MAX_TRIALS}")
            meets = sorted(c for c, r in model.samples.items() if math.floor(r) <= goal)
            best = meets[0] if meets else None
            if best is not None and (best == start_crf or best - 1 in model.samples):
                break                                       # found the lowest CRF that is clearly small enough
            if best is None and max_crf in model.samples:
                break                                       # even the ceiling is not enough
            too_big = [c for c, r in model.samples.items() if r > goal and (best is None or c < best)]
            low_edge = max(too_big) + 1 if too_big else start_crf
            high_edge = best - 1 if best is not None else max_crf
            if low_edge > high_edge:
                break
            guess = math.ceil(model.crf_for(goal) - 1e-9)
            candidate = min(max(guess, low_edge), high_edge)
            if candidate in model.samples:
                break
        meets = sorted(c for c, r in model.samples.items() if math.floor(r) <= goal)
        if not meets and max_crf not in model.samples:
            await trial(max_crf, "Search ceiling")
            meets = sorted(c for c, r in model.samples.items() if math.floor(r) <= goal)
        reached = bool(meets)
        chosen = meets[0] if meets else max_crf

        if reached:
            note = " - your video_crf already does it, no quality compromise needed" if chosen == start_crf else ""
            print(f"{MARKER_SEARCH} Chosen CRF {chosen}: estimated {model.samples[chosen]:.0%} of the source{note}.")
        else:
            message = (f"Even at max_crf {max_crf} the estimate is {model.samples[max_crf]:.0%} of the source "
                       f"(aiming for <= {goal:.0%}).")
            if max_crf - min(model.samples) >= 4:
                message += f" The measured curve suggests CRF ~{model.crf_for(goal):.0f} would be needed."
            print(f"{MARKER_SEARCH} {message}")

        if search_only:
            return finish("search_only", "crf_search_only is enabled: no full encode was run.")
        if not reached and not accept_oversized and model.samples[max_crf] > limit_ratio * _SKIP_MARGIN:
            return finish("rejected", f"Even at max_crf {max_crf} the estimated size is "
                                      f"{model.samples[max_crf]:.0%} of the source (limit {limit_ratio:.0%}).")
    else:
        print(f"{MARKER_SEARCH} Skipped (video too short for sample clips, or crf_search_samples = 0); "
              "the first full encode is used as the measurement.")
        if search_only:
            return finish("search_only", "crf_search_only is enabled but no sample clips could be taken.")

    # ---------------------------------------------------------------- 2./3. full encode (+ corrections)
    crf = chosen
    attempt = 0
    while True:
        attempt += 1
        out_path.unlink(missing_ok=True)
        started = time.monotonic()
        stats = await _run_ffmpeg(
            make_cmd(crf), duration=output_duration, total_frames=total_frames,
            desc=f"{MARKER_ENCODE} CRF:{crf} (try {attempt}/{max_attempts})",
            out_path=out_path, ref_size=ref_size,
            abort_above=(None if accept_oversized or output_duration < _ABORT_MIN_DURATION
                         else ref_size * limit_ratio * _ABORT_MARGIN),
            snapshot_every=snapshot_every,
        )
        wall = time.monotonic() - started
        if stats["aborted"]:
            size = stats["projected"]
            ratio = size / ref_size
            rows.append(("partial", crf, size, ratio, wall))
            out_path.unlink(missing_ok=True)
        else:
            size = out_path.stat().st_size
            ratio = size / ref_size
            rows.append(("full", crf, size, ratio, wall))
            speed = f" ({stats['frames'] / wall:.1f} fps)" if wall > 0 and stats["frames"] else ""
            print(f"{MARKER_ENCODE} CRF {crf}: {_format_bytes(size)} = {ratio:.0%} of the source "
                  f"in {_format_duration(wall)}{speed}")
            if crf in model.samples:
                print(f"{MARKER_ENCODE} The sample estimate for CRF {crf} was {model.samples[crf]:.0%}, the real result is "
                      f"{ratio:.0%} (estimate off by {model.samples[crf] / ratio - 1:+.0%}).")
        model.full[crf] = ratio

        if not stats["aborted"] and ratio <= limit_ratio:
            return finish("accepted", f"CRF {crf}: {ratio:.0%} of the source.")

        if crf >= max_crf or attempt >= max_attempts:
            if not stats["aborted"] and accept_oversized:
                return finish("accepted", f"CRF {crf}: {ratio:.0%} of the source - larger than the download "
                                          f"(on_size_fail = encode_anyway).")
            why = "max_crf reached" if crf >= max_crf else f"max_encode_attempts ({max_attempts}) used up"
            return finish("rejected", f"{why}; the encode at CRF {crf} is {ratio:.0%} of the source "
                                      f"(limit {limit_ratio:.0%}).")

        next_crf = min(max(math.ceil(model.crf_for(goal) - 1e-9), crf + 1), max_crf)
        print(f"{MARKER_ENCODE} Not smaller than the source; next try at CRF {next_crf} (calibrated with the real result).")
        crf = next_crf


async def _postprocess_video(path, ffmpeg_location=None, config=None):
    """Single ffmpeg pass over a downloaded file: SponsorBlock cut + loudnorm + final encode.
 
    One pass = only ONE lossy generation. ffmpeg can't re-time chapters/subtitles after a cut,
    so that is done here in Python (H3). Returns the encoded temp file; the caller moves it.
 
    With avoid_size_increase the CRF is chosen by _encode_to_size_target (short sample encodes instead of
    blind full retries). If no acceptable encode exists, on_size_fail decides: "keep_source" returns the
    untouched download, "fail" raises, "encode_anyway" accepts the encode at max_crf even if it is larger."""
    config = config if config is not None else getattr(config_module, "CONFIG", {})
    path = Path(path)
    ffmpeg, ffprobe = _resolve_ffmpeg_tools(ffmpeg_location)
 
    print(f"{MARKER_SOURCE} Analysing {path.name if len(path.name) <= 75 else path.name[:72] + '...'}")
    chapters = (await _probe_json(ffprobe, path, "-show_chapters")).get("chapters", [])
    # No -count_frames: it decodes the whole file silently (minutes of black box). The output is resampled
    # to a constant frame rate below, so duration x fps predicts its frame count at least as well.
    streams = (await _probe_json(ffprobe, path, "-show_streams")).get("streams", [])
    duration = float((await _probe_json(ffprobe, path, "-show_format"))["format"]["duration"])
 
    # main video stream (not an attached cover picture) and at least one audio stream
    video_idx = next(
        (st["index"] for st in streams
         if st.get("codec_type") == "video" and not (st.get("disposition") or {}).get("attached_pic")),
        None,
    )
    if video_idx is None or not any(st.get("codec_type") == "audio" for st in streams):
        raise RuntimeError(f"{path.name}: expected one video and one audio stream")
    video_stream = next(st for st in streams if st.get("index") == video_idx)
    frame_rate = _resolve_frame_rate(video_stream, config)
 
    # SponsorBlock marks -> ranges to cut -> inverted into ranges to keep
    cuts = _merge_ranges(
        (float(c["start_time"]), float(c["end_time"]))
        for c in chapters
        if (c.get("tags") or {}).get("title", "").startswith(_SB_CHAPTER_PREFIX)
    )
    keep, cursor = [], 0.0
    for start, end in cuts:
        if start - cursor > 0.05:
            keep.append((cursor, start))
        cursor = max(cursor, end)
    if duration - cursor > 0.05:
        keep.append((cursor, duration))
    if not keep:
        print(f"[SPONSORBLOCK] Whole video is marked as removable; ignoring the marks for {path.name}.")
        keep = [(0.0, duration)]
    output_duration = sum(b - a for a, b in keep)
    if frame_rate is not None:
        total_frames = max(1, round(output_duration * float(frame_rate)))
    else:
        source_frame_count = _video_frame_count(video_stream)
        if source_frame_count is not None and duration > 0:
            total_frames = max(1, round(source_frame_count * output_duration / duration))
        else:
            total_frames = None
 
    # validate the loudness target once (H4)
    loudness = None
    normalize_value = config.get("normalize_audio")
    if normalize_value is not None:
        try:
            loudness = float(normalize_value)
            if not -30.0 <= loudness <= -5.0:
                raise ValueError("must be between -30 and -5 LUFS")
        except (TypeError, ValueError) as e:
            print(f"{MARKER_NORMALIZE} Invalid normalize_audio value {normalize_value!r} ({e}); disabling normalization.")
            loudness = None
 
    vargs, pix_fmt = _video_codec_args(config)
    aargs = _audio_codec_args(config)
    out_path = path.with_name(f"{path.stem}.final.{_FINAL_CONTAINER}")

    # What we are up against: the size the kept part of the download "should" have. Comparing with the whole
    # file would be too generous whenever SponsorBlock removed something.
    source_size = path.stat().st_size
    ref_size = source_size * output_duration / duration if duration > 0 else float(source_size)
    audio_bps = _parse_bitrate(config.get("audio_bitrate", "96k"))
    if audio_bps is None:
        _warn_once("audio_bitrate", f"[CONFIG] Cannot parse audio_bitrate {config.get('audio_bitrate')!r}; "
                                    f"size estimates will ignore the audio track.")
    audio_bytes = (audio_bps or 0.0) * output_duration / 8

    print(f"{MARKER_SOURCE} {video_stream.get('codec_name', '?')} {video_stream.get('width', '?')}x{video_stream.get('height', '?')}"
          f" @ {frame_rate or '?'} fps | {_format_bytes(source_size)} = "
          f"{source_size * 8 / duration / 1e6 if duration > 0 else 0:.2f} Mb/s overall | {duration / 60:.1f} min")
    print(f"{MARKER_ENCODE} Cutting {len(cuts)} segment(s) | Kept ranges: {len(keep)} | Output duration: {output_duration:.2f}s")
 
    try:
        with tempfile.TemporaryDirectory(prefix="ytpost_") as tmp:
            workdir = Path(tmp)
            prepare_started = time.monotonic()
            print(f"{MARKER_ENCODE} Preparing subtitles, chapters and loudness measurement...")
 
            # Subtitles: extract each track, shift its cues, feed it back in as an extra input
            sub_inputs = []
            subtitle_streams = [st for st in streams if st.get("codec_type") == "subtitle"]
            for ordinal, st in enumerate(subtitle_streams):
                raw = workdir / f"sub{ordinal}.srt"
                rc, err = await _run_capture([
                    ffmpeg, "-y", "-v", "error", "-i", str(path),
                    "-map", f"0:{st['index']}", "-c:s", "srt", str(raw),
                ])
                if rc != 0:
                    print(f"{MARKER_ENCODE} Could not extract subtitle stream {st['index']}; skipping it: {err.strip()[-200:]}")
                    continue
                shifted = _shift_srt(raw.read_text(encoding="utf-8-sig", errors="replace"), keep)
                raw.write_text(shifted, encoding="utf-8")
                sub_inputs.append((raw, (st.get("tags") or {}).get("language", "und")))
 
            # Chapters: original ones only, re-timed (SponsorBlock marks are dropped)
            chapter_text = _build_chapter_metadata(chapters, keep)
            meta_path = None
            if chapter_text:
                meta_path = workdir / "chapters.ffmeta"
                meta_path.write_text(chapter_text, encoding="utf-8")
 
            # Loudness: pass 1 measures the already-cut audio, pass 2 applies it linearly (more accurate)
            audio_filter = None
            if loudness is not None:
                print(f"{MARKER_NORMALIZE} Measuring loudness (audio-only pass, no progress available)...")
                target = f"I={loudness}:TP=-1.5:LRA=11"
                measure_script = workdir / "measure.txt"
                measure_script.write_text(
                    _build_filter_graph(keep, video_idx, pix_fmt,
                                        audio_filter=f"loudnorm={target}:print_format=json",
                                        audio_only=True),
                    encoding="utf-8",
                )
                _, err = await _run_capture([
                    ffmpeg, "-hide_banner", "-nostats", "-i", str(path),
                    "-filter_complex_script", str(measure_script),
                    "-map", "[a]", "-f", "null", "-",
                ])
                match = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", err, re.S)
                try:
                    stats = json.loads(match.group(0))
                    audio_filter = (
                        f"loudnorm={target}:measured_I={stats['input_i']}:measured_TP={stats['input_tp']}"
                        f":measured_LRA={stats['input_lra']}:measured_thresh={stats['input_thresh']}"
                        f":offset={stats['target_offset']}:linear=true,aresample=48000"
                    )
                except (AttributeError, KeyError, ValueError):
                    print(f"{MARKER_NORMALIZE} Could not read the loudness measurement; using single-pass loudnorm.")
                    audio_filter = f"loudnorm={target},aresample=48000"
            else:
                audio_filter = "aresample=48000"
            print(f"{MARKER_ENCODE} Preparation took {_format_duration(time.monotonic() - prepare_started)}.")
 
            # The graph goes through a file so long segment lists can't hit the command-line limit
            script_path = workdir / "graph.txt"
            script_path.write_text(
                _build_filter_graph(
                    keep,
                    video_idx,
                    pix_fmt,
                    audio_filter=audio_filter,
                    frame_rate=frame_rate,
                ),
                encoding="utf-8",
            )
 
            head = [
                ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                *_ffmpeg_resource_args(config),
                "-progress", "pipe:1", "-stats_period", "0.5", "-nostats",
                "-i", str(path),                              # input 0: the download
            ]
            for srt_path, _ in sub_inputs:                    # inputs 1..n: shifted subtitles
                head += ["-i", str(srt_path)]
            if meta_path:                                     # input n+1: rebuilt chapters
                head += ["-f", "ffmetadata", "-i", str(meta_path)]
 
            head += ["-filter_complex_script", str(script_path), "-map", "[v]", "-map", "[a]"]
            for i in range(len(sub_inputs)):
                head += ["-map", f"{i + 1}:0"]
            head += ["-map", "0:t?"]                          # cover art / attachments, copied untouched
            head += ["-threads", str(config.get("ffmpeg_threads", 0))]   # output side: encoder threads

            tail_args = []
            if sub_inputs:
                tail_args += ["-c:s", "srt"]
                for i, (_, lang) in enumerate(sub_inputs):
                    tail_args += [f"-metadata:s:s:{i}", f"language={lang}"]
            tail_args += [
                "-map_metadata", "0",
                "-map_chapters", str(len(sub_inputs) + 1) if meta_path else "-1",
                str(out_path),
            ]

            def make_cmd(crf):
                """The complete final-encode command for one CRF value."""
                crf_args, _ = _video_codec_args(config, crf=crf)
                return [*head, *crf_args, *aargs, *tail_args]

            windows = []
            if config.get("avoid_size_increase", True):
                windows = _pick_sample_windows(
                    keep,
                    _config_number(config, "crf_search_samples", 6, int, 0),
                    _config_number(config, "crf_search_seconds", 15.0, float, 1.0),
                )
            ctx = {
                "config": config, "ffmpeg": ffmpeg, "path": path, "video_idx": video_idx,
                "pix_fmt": pix_fmt, "frame_rate": frame_rate, "workdir": workdir,
                "output_duration": output_duration, "ref_size": ref_size, "audio_bytes": audio_bytes,
            }
            outcome, message = await _encode_to_size_target(
                config=config, ctx=ctx, make_cmd=make_cmd, windows=windows,
                out_path=out_path, total_frames=total_frames,
            )
            if outcome == "accepted":
                print(f"{MARKER_ENCODE} Accepted - {message}")
                return out_path

            # No acceptable encode exists.
            out_path.unlink(missing_ok=True)
            if outcome == "rejected" and _resolve_on_size_fail(config) == "fail":
                raise RuntimeError(f"FFmpeg could not encode {path.name} without increasing size: {message}")
            if outcome == "search_only":
                print(f"{MARKER_ENCODE} {message} Keeping the download unchanged.")
            else:
                print(f"\033[33m{MARKER_ENCODE} {message} Keeping the download unchanged "
                      f"(no SponsorBlock cut, no loudness normalisation).\033[0m")
            return await _keep_source(path, ffmpeg, out_path)
    except BaseException:
        # includes cancellation: never leave half-written output behind
        out_path.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# yt-dlp download, retry ladder, status handling
# ---------------------------------------------------------------------------
 
def _build_download_args(url, title, config, auth_args, ffmpeg_path, output_template, paths_file,
                         relaxed=False, no_embeds=False):
    """Assemble the yt-dlp command. yt-dlp only downloads+merges+embeds; our own pass does the encode.
    relaxed / no_embeds are the two steps of the retry ladder."""
    prefix_args = []
    prefix = _detect_episode_prefix(title)
    if prefix:
        prefix_args = ["--replace-in-metadata", "title", "^", prefix]
 
    args = [
        "yt-dlp",
        "--no-playlist",
        "--windows-filenames",
        "--force-overwrites",  # output goes to a staging dir, so stale partial files are just replaced
        "--retries", "10",
        "--fragment-retries", "10",
        "--file-access-retries", "5",
        *prefix_args,
        "--format", _build_format_selector(config, relaxed=relaxed),
        "--output", str(output_template),
        "--merge-output-format", _FINAL_CONTAINER,
        # report the real output path instead of guessing it (H10)
        "--no-simulate", "--print-to-file", "after_move:filepath", paths_file,
    ]
 
    args.append("-v" if config.get("ytdlp_verbal") else "--quiet")
    if config.get("progress"):
        args.extend(["--progress"])
    else:
        args.extend(["--no-progress"])
 
    if config.get("embed_thumbnail") and not no_embeds:
        args.append("--embed-thumbnail")
    if config.get("write_subtitles") and not no_embeds:
        args.extend(["--write-subs", "--embed-subs", "--convert-subs", "srt"])
        subtitle_languages = config.get("subtitle_languages")
        if isinstance(subtitle_languages, list):
            subtitle_languages = [str(l).strip() for l in subtitle_languages if str(l).strip()]
            if subtitle_languages:
                args.extend(["--sub-langs", ",".join(subtitle_languages)])
    if config.get("embed_metadata"):
        args.append("--embed-metadata")
 
    # SponsorBlock is only MARKED here (as chapters); _postprocess_video does the frame-accurate cut
    if config.get("sponsorblock_enabled"):
        sb_categories = [c for c in config.get("sponsorblock_categories", [])
                         if c in config_module.SPONSORBLOCK_CATEGORIES]
        if not sb_categories:
            print(f"{MARKER_DOWNLOAD}SponsorBlock is enabled but no valid categories are set. Using all cuttable categories.")
            sb_categories = list(config_module.SPONSORBLOCK_CATEGORIES)
        args.extend(["--sponsorblock-mark", ",".join(sb_categories)])
    if config.get("embed_chapters") or config.get("sponsorblock_enabled"):
        args.append("--embed-chapters")  # marks only reach the file as chapters
 
    if ffmpeg_path:
        args.extend(["--ffmpeg-location", str(ffmpeg_path)])
    args.extend(auth_args)
    args.extend(["--", url])  # "--" must stay last: the URL can never be parsed as an option (S2)
    return args

async def _read_output_lines(stream):
    buffer = bytearray()

    while True:
        chunk = await stream.read(1)

        if not chunk:
            if buffer:
                yield bytes(buffer)
            break

        if chunk in (b"\r", b"\n"):
            if buffer:
                yield bytes(buffer)
                buffer.clear()
        else:
            buffer.extend(chunk)
 
async def _run_ytdlp(args, config):
    """Run yt-dlp; returns (exit code, last output lines).
    The tail is kept even in quiet mode, so a failure always carries yt-dlp's own error text (H5)."""
    verbose = bool(config.get("ytdlp_verbal"))
    show_progress = bool(config.get("progress"))
    if config.get("print_command"):
        print(*args)
        print("============================")
 
    tail = deque(maxlen=30)
    progress_width = 0

    def clear_progress_line():
        nonlocal progress_width
        if progress_width:
            sys.stdout.write("\r" + (" " * progress_width) + "\r")
            sys.stdout.flush()
            progress_width = 0

    def print_status(message):
        clear_progress_line()
        print(message)

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT
    )
 
    try:
        async for line in _read_output_lines(proc.stdout):
            decoded_line = line.decode(errors="ignore").rstrip()

            if not decoded_line.startswith(MARKER_DOWNLOAD):
                tail.append(decoded_line)

            if verbose:
                print(decoded_line)
                continue

            if show_progress:
                if decoded_line.startswith(YTDLP_PROGRESS_MARKER):
                    padded_line = decoded_line.ljust(progress_width)
                    sys.stdout.write("\r" + padded_line)
                    sys.stdout.flush()
                    progress_width = max(progress_width, len(decoded_line))
                    sys.stdout.flush()
                    progress_width = max(progress_width, len(decoded_line))

                elif decoded_line.startswith("[Merger]"):
                    print_status(f"[YT-DLP] Merging: {decoded_line}")

                elif decoded_line.startswith("[Metadata]"):
                    print_status(f"[YT-DLP] Metadata: {decoded_line}")

                elif decoded_line.startswith("[EmbedThumbnail]"):
                    print_status(f"[YT-DLP] Thumbnail: {decoded_line}")

                elif decoded_line.startswith("[EmbedSubtitle]"):
                    print_status(f"[YT-DLP] Subtitles: {decoded_line}")

                elif decoded_line.startswith("[Fixup"):
                    print_status(f"[YT-DLP] Fixup: {decoded_line}")

                elif decoded_line:
                    print_status(decoded_line)

            elif decoded_line.startswith(("ERROR", "WARNING")):
                print(decoded_line)

        if progress_width:
            sys.stdout.write("\n")
            sys.stdout.flush()
            progress_width = 0
        rc = await proc.wait()
    except asyncio.CancelledError:
        await _stop_process(proc)
        raise
    return rc, "\n".join(tail)
 
 
async def _download_async(url, database, title, config=None, auth_args=None):
    """Download with yt-dlp (retry ladder), run the single-pass encode, move the result into the library."""
    if config is None:
        config = getattr(config_module, 'CONFIG', {})
    if auth_args is None:
        auth_args = _build_ytdlp_auth_args(config)
    download_started = time.monotonic()
    download_logger = Logger()
 
    # yt-dlp writes into a staging dir; only finished, encoded files ever appear in the library
    template = Path(config["output_template"])
    final_dir = template.parent
    staging_dir = final_dir / ".staging"
    staging_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg_resolved = _resolve_ffmpeg_location(config)
 
    print("\n========== YT-DLP ==========")
    abrev_title: str = title if len(title) <= 75 else title[:72] + "..."
    print(f"{url}\n{abrev_title}")
    print(f"\033[37m[SYSTEM] Queue length: {len(database.get_queued())}\033[0m")
    time_now: str = time.strftime("%y/%m/%d %H:%M", time.gmtime())
    print("======", time_now, "======\n")
 
    # Retry ladder: full command -> relaxed format selector (format errors)
    # -> no embedded thumbnail/subs (post-processing errors). Each step is tried at most once.
    relaxed = False
    no_embeds = False
    while True:
        fd, paths_file = tempfile.mkstemp(suffix=".txt")
        os.close(fd)
        args = _build_download_args(
            url, title, config, auth_args, ffmpeg_resolved,
            staging_dir / template.name, paths_file,
            relaxed=relaxed, no_embeds=no_embeds,
        )
        downloaded_files = []
        try:
            rc, tail = await _run_ytdlp(args, config)
            if rc == 0:
                with open(paths_file, encoding="utf-8") as fh:
                    downloaded_files = [ln.strip() for ln in fh if ln.strip()]
        except Exception as e:
            print(f"[ERROR] An error occurred while running yt-dlp: {e}")
            rc, tail = 1, str(e)
        finally:
            Path(paths_file).unlink(missing_ok=True)
 
        if rc == 0:
            break
 
        kind = _classify_ytdlp_failure(tail)
        if kind == "format" and not relaxed:
            relaxed = True
            print("[RETRY] Requested format not available; retrying with a relaxed format selector.")
        elif kind == "post" and not no_embeds:
            no_embeds = True
            print("[RETRY] Post-processing failed; retrying without embedded thumbnail/subtitles.")
        else:
            raise RuntimeError(f"yt-dlp exit {rc}:\n{tail}")
 
    if not downloaded_files:
        raise RuntimeError("yt-dlp finished but reported no output file")
    else:
        print(f"{MARKER_DOWNLOAD} raw download completed: {len(downloaded_files)} file(s) downloaded. Post-processing...")
    download_duration = time.monotonic() - download_started

    _, ffprobe_resolved = _resolve_ffmpeg_tools(ffmpeg_resolved)
    video_resolution = None
    try:
        for downloaded in downloaded_files:
            source = Path(downloaded)
            if source.is_file():
                video_resolution = await _get_video_resolution(source, ffprobe_resolved)
                break
    except Exception as e:
        print(f"[ERROR] Could not determine video resolution: {e}")

    print(f"{MARKER_INFO} Video resolution: {video_resolution}" if video_resolution else "Video resolution: Unknown")
 
    saved_bytes = 0
    final_paths = []
    for num, downloaded in enumerate(downloaded_files):
        source = Path(downloaded)
        source_size = source.stat().st_size
        encoding_started = time.monotonic()
        encoded = await _postprocess_video(source, ffmpeg_resolved, config)
        encoding_duration = time.monotonic() - encoding_started
        final = _unique_path(final_dir / f"{source.stem}.{_FINAL_CONTAINER}")
 
        encoded_size = encoded.stat().st_size
        saved_bytes += max(0, source_size - encoded_size)
        print(f"{MARKER_ENCODE} {_format_bytes(source_size)} -> {_format_bytes(encoded_size)} "
              f"({encoded_size / max(source_size, 1):.0%} of the download)")
 
        shutil.move(str(encoded), str(final))
        source.unlink(missing_ok=True)
        final_paths.append(final)
        print(f"{MARKER_POSTPROCESS} Completed {num + 1}/{len(downloaded_files)}: {final.name}")
 
        reduction = max(0, source_size - encoded_size) / max(source_size, 1) * 100
        selected_sb_categories = config.get("sponsorblock_categories", [])
        download_logger.new()
        download_logger.current(Log.TIMESTAMP, time.strftime("%Y-%m-%d %H:%M:%S"))
        download_logger.current(Log.DOWNLOAD_DURATION, f"{download_duration:.2f}s")
        download_logger.current(Log.ENCODING_DURATION, f"{encoding_duration:.2f}s")
        download_logger.current(Log.FILENAME, final.name)
        download_logger.current(Log.URL, url)
        download_logger.current(
            Log.SBCUT,
            ", ".join(selected_sb_categories) if config.get("sponsorblock_enabled") else "disabled",
        )
        download_logger.current(Log.ORIGINAL_SIZE, _format_bytes(source_size))
        download_logger.current(Log.FINAL_SIZE, _format_bytes(encoded_size))
        download_logger.current(Log.PERCENTAGE_REDUCTION, f"{reduction:.1f}")
        download_logger.write()

    print(f"\n{MARKER_DOWNLOAD}Finished downloading: {url}")
    database.set_filepath(url, str(final_paths[0]))
    database.update_status(url, "completed")
    return {
        'status': 'downloaded',
        'saved_bytes': saved_bytes,
    }
 
 
async def download_video(url, conf_args=None):
    """Claim a queued video, download it and run the single-pass encode.
    Safe to call from the queue worker and from runall() at the same time (H12)."""
    database = db.Database()
    config = getattr(config_module, "CONFIG", {})
 
    if _normalize_url(url) is None:
        database.update_status(url, "failed")
        raise ValueError(f"Not a supported YouTube video URL: {url!r}")
 
    async with _get_download_slots():
        if database.is_done(url):
            print(f"{MARKER_DOWNLOAD}Skipping already downloaded video: {url}")
            return {'status': 'skipped', 'reason': 'already_downloaded', 'saved_bytes': 0}
        if not database.claim(url):
            print(f"{MARKER_DOWNLOAD}Skipping {url}: already being processed")
            return {'status': 'skipped', 'reason': 'in_progress', 'saved_bytes': 0}
 
        try:
            if conf_args is None:
                conf_args = _build_ytdlp_auth_args(config)
            try:
                title = await _fetch_video_title_async(url=url, conf_args=conf_args)
            except Exception as e:
                title = url
                print(f"Error fetching video title for url {url}: \n{e}")
 
            return await _download_async(url, database, title, config=config, auth_args=conf_args)
        except Exception:
            database.update_status(url, "failed")
            raise
 
 
async def runall():
    """Process every queued video once. Shares the concurrency limit and the DB claim with the queue worker."""
    database = db.Database()
    queued_urls = database.get_queued()
 
    summary = {
        'succeeded': 0, 'failed': 0, 'skipped': 0, 'saved_bytes': 0,
        'failed_downloads': [],
    }
 
    async def worker(url):
        try:
            result = await download_video(url, conf_args=_build_ytdlp_auth_args(config_module.CONFIG))
            if result and result.get('status') == 'downloaded':
                summary['succeeded'] += 1
                summary['saved_bytes'] += result.get('saved_bytes', 0)
            elif result and result.get('status') == 'skipped':
                summary['skipped'] += 1
        except Exception as e:
            # download_video already set the status to 'failed'
            print(f"\n[ERROR] Download failed for {url}: {e}")
            summary['failed'] += 1
            summary['failed_downloads'].append({
                "url": url,
                "title": _RUNTIME_TITLE_CACHE.get(url, url),
                "reason": str(e),
            })
 
    await asyncio.gather(*(worker(url) for url in queued_urls))
 
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
    asyncio.run(runall())