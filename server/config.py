from pathlib import Path
import tomllib

BASE_DIR = Path(__file__).resolve().parent

CONFIG_PATH = BASE_DIR / "config.toml"

with open(CONFIG_PATH, "rb") as f:
    try:
        CONFIG = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise(f"Error reading config file: {e}")
    
print("Configuration loaded successfully; using the following settings:")
max_key_length = max(len(key) for key in CONFIG.keys())
for key, value in CONFIG.items():   
    print(f"  {key:>{max_key_length}}: {value}")