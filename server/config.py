from pathlib import Path
import tomllib
import re

BASE_DIR = Path(__file__).resolve().parent

CONFIG_PATH = BASE_DIR / "config.toml"
NAME_DATABANK_PATH = BASE_DIR / "name_databank.json"

with open(CONFIG_PATH, "rb") as f:
    try:
        CONFIG = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise(f"Error reading config file: {e}")
    
print("Configuration loaded successfully; using the following settings:")
max_key_length = max(len(key) for key in CONFIG.keys())
for key, value in CONFIG.items():   
    print(f"  {key:>{max_key_length}}: {value}")


def reload_config():
    """Reload CONFIG from disk and return it."""
    global CONFIG
    with open(CONFIG_PATH, 'rb') as f:
        CONFIG = tomllib.load(f)
    return CONFIG


def update_sponsorblock(enabled: bool, categories: list[str]):
    """Update the sponsorblock settings in the TOML file and reload CONFIG.

    This performs a best-effort textual replacement of the `sponsorblock_enabled`
    boolean and the `sponsorblock_categories` array in `config.toml`.
    """
    text = CONFIG_PATH.read_text(encoding='utf-8')

    # replace enabled boolean
    enabled_str = 'true' if enabled else 'false'
    text, n1 = re.subn(r'(?m)^\s*sponsorblock_enabled\s*=\s*.*$', f'sponsorblock_enabled = {enabled_str}', text)

    # build categories array text
    if categories:
        items = ',\n  '.join([f'"{c}"' for c in categories])
        cats_text = f'sponsorblock_categories = [\n  {items},\n]'
    else:
        cats_text = 'sponsorblock_categories = []'

    text, n2 = re.subn(r'(?ms)^\s*sponsorblock_categories\s*=\s*\[.*?\]', cats_text, text)

    # write back if something changed
    if n1 or n2:
        CONFIG_PATH.write_text(text, encoding='utf-8')
        return reload_config()
    return CONFIG