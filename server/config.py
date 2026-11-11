from pathlib import Path
import tomllib
import re
import os
import shutil
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR.parent / ".env")

SPONSORBLOCK_CATEGORIES = (
    "sponsor", "selfpromo", "interaction", "intro",
    "outro", "preview", "filler", "music_offtopic",
)

# Optional shared secret for the local API (S1)
API_TOKEN = os.environ.get("API_TOKEN") or None



CONFIG_PATH = BASE_DIR / "config.toml"
NAME_DATABANK_PATH = BASE_DIR / "name_databank.json"

with open(CONFIG_PATH, "rb") as f:
    try:
        CONFIG = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise RuntimeError(f"Error reading config file: {e}") from e

def auth_headers() -> dict:
    """Headers the local GUIs must send to the server (token only if one is configured)."""
    return {"X-API-Token": API_TOKEN} if API_TOKEN else {}

def _assemble_config_line(key: str, value, max_key_length: int) -> str:
    """Align keys and place dots at every third absolute column."""
    padding = "".join(
        "." if (i + len(key) + 1) % 2 != 0 else " "
        for i in range(max(0, max_key_length - len(key)))
    )
    return f"{key}{padding}: {value}"

if CONFIG.get("show_config_on_startup", True):
    print("Configuration loaded successfully; using the following settings:")
    max_key_length = max(len(key) for key in CONFIG.keys())
    for key, value in CONFIG.items():
        print(_assemble_config_line(key, value, max_key_length))
        
    # Check the configured executable or PATH when no executable is configured.
    max_l:int = 0
    configured_keys = {
        "ffprobe": "ffprobe_loc",
        "ffmpeg": "ffmpeg_loc",
        "yt-dlp": "yt_dlp_loc",
        "node": "js_runtime_loc",
    }
    for k in configured_keys:
        max_l = len(k) if len(k) > max_l else max_l
    for tool, config_key in configured_keys.items():
        configured_path = CONFIG.get(config_key)
        if isinstance(configured_path, str) and configured_path.startswith(".env/"):
            configured_path = os.environ.get(configured_path.removeprefix(".env/"))
        if configured_path:
            tool_path = configured_path
            found_path = shutil.which(tool_path)
        else:
            tool_path = tool
            found_path = shutil.which(tool_path)



        if found_path is None:
            print(f"\033[33m[NO]\033[0m {str(tool)[:max_l]:<{max_l}} not found at configured path or in PATH: '{tool_path}'")
        else:
            print(f"\033[32m[OK]\033[0m {str(tool)[:max_l]:<{max_l}} found at: {found_path}")


def reload_config():
    """Reload CONFIG from disk and return it.

    The dict is updated IN PLACE: modules that did `from config import CONFIG`
    would otherwise keep a stale copy forever (H7)."""
    with open(CONFIG_PATH, 'rb') as f:
        new_config = tomllib.load(f)
    CONFIG.clear()
    CONFIG.update(new_config)
    return CONFIG


def update_sponsorblock(enabled: bool, categories: list[str]):
    """Update the sponsorblock settings in the TOML file and reload CONFIG.

    Categories are filtered against SPONSORBLOCK_CATEGORIES, so nothing
    user-controlled can ever be written into the TOML text (S3)."""
    categories = [c for c in categories if c in SPONSORBLOCK_CATEGORIES]
    text = CONFIG_PATH.read_text(encoding='utf-8')

    enabled_str = 'true' if enabled else 'false'
    # lambdas: the replacement is used literally (no backslash/group interpretation)
    text, n1 = re.subn(
        r'(?m)^[ \t]*sponsorblock_enabled[ \t]*=.*$',
        lambda m: f'sponsorblock_enabled = {enabled_str}',
        text,
    )

    if categories:
        items = ',\n  '.join(f'"{c}"' for c in categories)
        cats_text = f'sponsorblock_categories = [\n  {items},\n]'
    else:
        cats_text = 'sponsorblock_categories = []'

    text, n2 = re.subn(
        r'(?ms)^[ \t]*sponsorblock_categories[ \t]*=[ \t]*\[.*?\]',
        lambda m: cats_text,
        text,
    )

    if n1 or n2:
        CONFIG_PATH.write_text(text, encoding='utf-8')
        return reload_config()
    return CONFIG