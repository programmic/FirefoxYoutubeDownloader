# server/download_worker.py
 
import asyncio
import json
import os
import re
import shutil
import tempfile
import threading
from collections import deque
from fractions import Fraction
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from dotenv import load_dotenv
from tqdm import tqdm
 
from . import database as db
from . import config as config_module
 
PROJECT_ROOT = Path(__file__).resolve().parents[1]
 
env = load_dotenv()
 
_EPISODE_PREFIX_PATTERNS = (
    re.compile(r'(?i)\bS(?P<season>\d{1,2})\s*E(?P<episode>\d{1,3})\b'),
    re.compile(r'(?i)\bEP(?:ISODE)?\.?\s*(?P<episode>\d{1,3})\b'),
    re.compile(r'(?i)(?<!\w)#\s*(?P<episode>\d{1,3})\b'),
)
 
_RUNTIME_TITLE_CACHE = {}
 
_SB_CHAPTER_PREFIX = "[SponsorBlock]"
_FINAL_CONTAINER = "mkv"            # holds AV1/HEVC + Opus + srt + chapters + cover cleanly
_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
_DATABANK_LOCK = threading.Lock()
_DOWNLOAD_SLOTS = None              # created lazily, see _get_download_slots()
_SRT_TIME_RE = re.compile(r"(\d+):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d+):(\d{2}):(\d{2})[,.](\d{3})")
 
 
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
            js_runtime_path /= "node.exe" if os.name == "nt" else "node"
 
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
                print('[DATABANK] Unreadable, not overwriting it:', e)
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
        print(f"[TITLE] yt-dlp failed to fetch playlist title with exit code {proc.returncode}")
        error_output = stderr.decode("utf-8", errors="replace").strip()
        if error_output:
            print(f"[TITLE] stderr:\n{error_output}")
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
 
 
# ---------------------------------------------------------------------------
# Single-pass encode: SponsorBlock cut + loudnorm + final compression
#
# yt-dlp only *marks* sponsor segments as chapters. We then cut them ourselves in
# ONE ffmpeg pass that trims video and audio with the same timestamps (no A/V drift)
# and applies the final encode, so there is only one lossy generation.
# ffmpeg can't re-time chapters/subtitles after a cut, so that is done in Python.
# ---------------------------------------------------------------------------
 
def _video_codec_args(config):
    """(encoder args, pixel format) for the single final encode.
    Software encoders at slow presets give the smallest files for a given quality."""
    codec = str(config.get("video_codec", "h264")).lower()
    crf = config.get("video_crf")
    preset = config.get("video_preset")
 
    if codec == "av1":
        return [
            "-c:v", "libsvtav1",
            "-preset", str(4 if preset is None else preset),
            "-crf", str(30 if crf is None else crf),
            "-svtav1-params", "tune=0",  # tune for visual quality instead of PSNR
        ], "yuv420p10le"  # 10-bit avoids banding and compresses better
    if codec == "hevc":
        return [
            "-c:v", "libx265",
            "-preset", str("slow" if preset is None else preset),
            "-crf", str(25 if crf is None else crf),
            "-tag:v", "hvc1",  # Apple players need this tag
            "-x265-params", "log-level=error",
        ], "yuv420p10le"
    if codec == "h264":
        return [
            "-c:v", "libx264",
            "-preset", str("slower" if preset is None else preset),
            "-crf", str(22 if crf is None else crf),
        ], "yuv420p"
    raise ValueError(f"Unknown video_codec {codec!r} (use av1, hevc or h264)")
 
 
def _audio_codec_args(config):
    """Opus is clearly more efficient than AAC at 80-96 kbps."""
    bitrate = str(config.get("audio_bitrate", "96k"))
    return ["-c:a", "libopus", "-b:a", bitrate, "-vbr", "on", "-application", "audio"]


def _higher_crf(config):
    """Return one quality step that is more likely to reduce an oversized output."""
    try:
        current = int(config.get("video_crf", 30))
    except (TypeError, ValueError):
        return None
    if current >= 51:
        return None
    return min(current + 4, 51)


def _ffmpeg_resource_args(config):
    """Keep decode/filter buffering bounded for long AV1 sources."""
    try:
        threads = int(config.get("ffmpeg_threads", 1))
    except (TypeError, ValueError) as e:
        raise ValueError("ffmpeg_threads must be a positive integer") from e
    if threads < 1:
        raise ValueError("ffmpeg_threads must be a positive integer")
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
    proc = await asyncio.create_subprocess_exec(
        ffprobe, "-v", "error", *flags, "-of", "json", str(path),
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
 
 
async def _run_capture(cmd):
    """Run a command to completion; returns (exit code, stderr text). Cancel-safe."""
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        _, stderr = await proc.communicate()
    except asyncio.CancelledError:
        await _stop_process(proc)
        raise
    return proc.returncode, stderr.decode("utf-8", errors="replace")
 
 
async def _run_ffmpeg(cmd, duration, desc="Encode"):
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
 
    progress_data = {}
    output_tail = []
    last_elapsed = 0.0
 
    bar_format = (
        "{desc}: {percentage:6.2f}%|{bar}| "
        "{n:.1f}/{total:.1f}s "
        "[{elapsed}<{remaining}] {postfix}"
    )
 
    try:
        with tqdm(
            total=max(duration, 0.001),
            desc=desc,
            unit="s",
            dynamic_ncols=True,
            bar_format=bar_format,
            leave=True,
        ) as bar:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
 
                line = line.decode(errors="replace").strip()
                if not line:
                    continue
 
                if "=" not in line:
                    output_tail.append(line)
                    output_tail = output_tail[-30:]
                    continue
 
                key, value = line.split("=", 1)
                progress_data[key] = value
 
                if key != "progress":
                    continue

                try:
                    elapsed = _progress_elapsed(progress_data, duration)
                    if elapsed is not None:
                        if elapsed > last_elapsed:
                            bar.update(elapsed - last_elapsed)
                            last_elapsed = elapsed
 
                    speed = progress_data.get("speed", "N/A")
                    frame = progress_data.get("frame", "0")
 
                    bar.set_postfix_str(
                        f"{speed} | frame {frame}",
                        refresh=True,
                    )
 
                except (ValueError, TypeError):
                    pass
 
                progress_data = {}
 
            returncode = await proc.wait()
 
            if returncode == 0:
                # Finish cleanly despite small timestamp/rounding differences.
                if bar.n < bar.total:
                    bar.update(bar.total - bar.n)
    except asyncio.CancelledError:
        # a timeout/cancel must not leave an orphaned encode running (H1)
        await _stop_process(proc)
        raise
 
    if returncode != 0:
        error_text = "\n".join(output_tail)[-1000:]
        raise RuntimeError(
            f"FFmpeg {desc} failed (exit {returncode}):\n"
            f"{error_text}"
        )
 
 
async def _postprocess_video(path, ffmpeg_location=None, config=None):
    """Single ffmpeg pass over a downloaded file: SponsorBlock cut + loudnorm + final encode.
 
    One pass = only ONE lossy generation. ffmpeg can't re-time chapters/subtitles after a cut,
    so that is done here in Python (H3). Returns the encoded temp file; the caller moves it."""
    config = config if config is not None else getattr(config_module, "CONFIG", {})
    path = Path(path)
    ffmpeg, ffprobe = _resolve_ffmpeg_tools(ffmpeg_location)
 
    chapters = (await _probe_json(ffprobe, path, "-show_chapters")).get("chapters", [])
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
    frame_rate = _source_frame_rate(
        next(st for st in streams if st.get("index") == video_idx)
    )
 
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
 
    # validate the loudness target once (H4)
    loudness = None
    normalize_value = config.get("normalize_audio")
    if normalize_value is not None:
        try:
            loudness = float(normalize_value)
            if not -30.0 <= loudness <= -5.0:
                raise ValueError("must be between -30 and -5 LUFS")
        except (TypeError, ValueError) as e:
            print(f"[NORMALIZE] Invalid normalize_audio value {normalize_value!r} ({e}); disabling normalization.")
            loudness = None
 
    vargs, pix_fmt = _video_codec_args(config)
    aargs = _audio_codec_args(config)
    out_path = path.with_name(f"{path.stem}.final.{_FINAL_CONTAINER}")
    print(f"[ENCODE] {path.name if len(path.name) <= 75 else path.name[:72] + '...'}")
    print(f"[ENCODE] Cutting {len(cuts)} segment(s) | Kept ranges: {len(keep)} | Output duration: {output_duration:.2f}s")
 
    try:
        with tempfile.TemporaryDirectory(prefix="ytpost_") as tmp:
            workdir = Path(tmp)
 
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
                    print(f"[ENCODE] Could not extract subtitle stream {st['index']}; skipping it: {err.strip()[-200:]}")
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
                    print("[NORMALIZE] Could not read the loudness measurement; using single-pass loudnorm.")
                    audio_filter = f"loudnorm={target},aresample=48000"
            else:
                audio_filter = "aresample=48000"
 
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
 
            cmd = [
                ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                *_ffmpeg_resource_args(config),
                "-progress", "pipe:1", "-stats_period", "0.5", "-nostats",
                "-i", str(path),                              # input 0: the download
            ]
            for srt_path, _ in sub_inputs:                    # inputs 1..n: shifted subtitles
                cmd += ["-i", str(srt_path)]
            if meta_path:                                     # input n+1: rebuilt chapters
                cmd += ["-f", "ffmetadata", "-i", str(meta_path)]
 
            cmd += ["-filter_complex_script", str(script_path), "-map", "[v]", "-map", "[a]"]
            for i in range(len(sub_inputs)):
                cmd += ["-map", f"{i + 1}:0"]
            cmd += ["-map", "0:t?"]                           # cover art / attachments, copied untouched
            cmd += ["-threads", str(config.get("ffmpeg_threads", 1))]
            cmd += [*vargs, *aargs]
            if sub_inputs:
                cmd += ["-c:s", "srt"]
                for i, (_, lang) in enumerate(sub_inputs):
                    cmd += [f"-metadata:s:s:{i}", f"language={lang}"]
            cmd += [
                "-map_metadata", "0",
                "-map_chapters", str(len(sub_inputs) + 1) if meta_path else "-1",
                str(out_path),
            ]
 
            await _run_ffmpeg(cmd, duration=output_duration, desc="Encode")

            # CRF targets visual quality, not file size. A source selected by yt-dlp
            # may already be more compressed than the requested CRF output, so avoid
            # accepting a larger result when a single quality step can correct it.
            if config.get("avoid_size_increase", True) and out_path.stat().st_size > path.stat().st_size:
                higher_crf = _higher_crf(config)
                crf_position = cmd.index("-crf") + 1 if "-crf" in cmd else None
                if higher_crf is not None and crf_position is not None:
                    print(
                        f"[ENCODE] Output is larger than the source; retrying with CRF {higher_crf}."
                    )
                    cmd[crf_position] = str(higher_crf)
                    out_path.unlink()
                    await _run_ffmpeg(cmd, duration=output_duration, desc="Encode retry")
    except BaseException:
        # includes cancellation: never leave half-written output behind
        out_path.unlink(missing_ok=True)
        raise
    return out_path
 
 
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
        args.extend(["--progress", "--newline"])
 
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
            print("[DOWNLOAD] SponsorBlock is enabled but no valid categories are set. Using all cuttable categories.")
            sb_categories = list(config_module.SPONSORBLOCK_CATEGORIES)
        args.extend(["--sponsorblock-mark", ",".join(sb_categories)])
    if config.get("embed_chapters") or config.get("sponsorblock_enabled"):
        args.append("--embed-chapters")  # marks only reach the file as chapters
 
    if ffmpeg_path:
        args.extend(["--ffmpeg-location", str(ffmpeg_path)])
    args.extend(auth_args)
    args.extend(["--", url])  # "--" must stay last: the URL can never be parsed as an option (S2)
    return args
 
 
async def _run_ytdlp(args, config):
    """Run yt-dlp; returns (exit code, last output lines).
    The tail is kept even in quiet mode, so a failure always carries yt-dlp's own error text (H5)."""
    verbose = bool(config.get("ytdlp_verbal"))
    show_progress = bool(config.get("progress"))
    if config.get("print_command"):
        print(*args)
        print("============================")
 
    tail = deque(maxlen=30)
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT
    )
 
    try:
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
 
            decoded_line = line.decode(errors="ignore").rstrip()
            if not decoded_line.startswith("[download]"):  # progress spam would flood the tail
                tail.append(decoded_line)
 
            if verbose:
                print(decoded_line)
                continue
 
            if show_progress:
                if decoded_line.startswith("[download]"):
                    print("\r\033[K" + decoded_line, end="", flush=True)
                else:
                    print(decoded_line)
            elif decoded_line.startswith(("ERROR", "WARNING")):
                print(decoded_line)  # quiet mode must still surface problems
 
        print()  # Ensure the last progress line ends with a newline
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
    print("============================\n")
 
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
 
    saved_bytes = 0
    final_paths = []
    for downloaded in downloaded_files:
        source = Path(downloaded)
        encoded = await _postprocess_video(source, ffmpeg_resolved, config)
        final = _unique_path(final_dir / f"{source.stem}.{_FINAL_CONTAINER}")
 
        source_size, encoded_size = source.stat().st_size, encoded.stat().st_size
        saved_bytes += max(0, source_size - encoded_size)
        print(f"[ENCODE] {_format_bytes(source_size)} -> {_format_bytes(encoded_size)} "
              f"({encoded_size / max(source_size, 1):.0%} of the download)")
 
        shutil.move(str(encoded), str(final))
        source.unlink(missing_ok=True)
        final_paths.append(final)
 
    print(f"\n[DOWNLOAD] Finished downloading: {url}")
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
            print(f"[DOWNLOAD] Skipping already downloaded video: {url}")
            return {'status': 'skipped', 'reason': 'already_downloaded', 'saved_bytes': 0}
        if not database.claim(url):
            print(f"[DOWNLOAD] Skipping {url}: already being processed")
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
 