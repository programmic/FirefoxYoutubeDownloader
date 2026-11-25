
import re
import sys
from pathlib import Path
from statistics import mean

# ANSI colors
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
CYAN = "\033[96m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"

def color_ratio(ratio):
    if ratio < 75:
        return GREEN
    elif ratio < 90:
        return YELLOW
    return RED

def format_size(mb):
    if mb >= 1024:
        return f"{mb / 1024:.2f} GB"
    return f"{mb:.2f} MB"

def format_time(seconds):
    minutes, seconds = divmod(int(seconds), 60)
    return f"{minutes}m {seconds}s"

def parse_file(content):
    entries = []

    # Find every filename and its position
    matches = list(re.finditer(
        r"(?m)^(.+\.mkv)\s*$", content
    ))

    for i, match in enumerate(matches):
        # Everything belonging to this video
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        block = content[start:end]

        sizes = re.search(
            r"([\d.]+)\s*MB\s*>>>\s*([\d.]+)\s*MB\s*"
            r"\(([\d.]+)%\s*(of original|reduction)\)",
            block,
            re.IGNORECASE
        )

        download = re.search(
            r"Download Duration:\s*([\d.]+)s", block
        )
        encoding = re.search(
            r"Encoding Duration:\s*([\d.]+)s", block
        )
        selected = re.search(r"selected:\s*(.*)", block)

        if not all([sizes, download, encoding]):
            continue

        original = float(sizes.group(1))
        compressed = float(sizes.group(2))
        percentage = float(sizes.group(3))

        kind = sizes.group(4).lower()
        ratio = 100 - percentage if kind == "reduction" else percentage

        entries.append({
            "name": match.group(1).strip(),
            "original": original,
            "compressed": compressed,
            "ratio": ratio,
            "download": float(download.group(1)),
            "encoding": float(encoding.group(1)),
            "selected": selected.group(1).strip() if selected else ""
        })

    return entries

def main():
    if len(sys.argv) < 2:
        print(f"Usage: python stats.py <input.txt>")
        return

    content = Path(sys.argv[1]).read_text(encoding="utf-8")
    entries = parse_file(content)

    if not entries:
        print(f"{RED}No valid entries found.{RESET}")
        return

    total_original = sum(e["original"] for e in entries)
    total_compressed = sum(e["compressed"] for e in entries)
    total_saved = total_original - total_compressed
    total_download = sum(e["download"] for e in entries)
    total_encoding = sum(e["encoding"] for e in entries)

    overall_ratio = total_compressed / total_original * 100

    print(f"\n{BOLD}{CYAN}=============== VIDEOS ==============={RESET}")

    for i, e in enumerate(entries, 1):
        color = color_ratio(e["ratio"])
        saved = e["original"] - e["compressed"]

        print(f"\n{BOLD}{i:02d}. {e['name']}{RESET}")
        print(f"    {e['original']:.1f} MB -> {e['compressed']:.1f} MB")
        print(f"    Saved: {GREEN}{saved:.1f} MB{RESET} "
              f"| Ratio: {color}{e['ratio']:.1f}%{RESET}")
        print(f"    Download: {format_time(e['download'])} "
              f"| Encoding: {format_time(e['encoding'])}")


    COL_WIDTH = 24  # Change this to adjust all column widths

    print(f"\n{BOLD}{CYAN}========== VIDEO STATISTICS =========={RESET}")

    print(f" {'Videos processed:':<{COL_WIDTH}} {BOLD}{len(entries)}{RESET}")
    print(f" {'Original size:':<{COL_WIDTH}} {format_size(total_original)}")
    print(f" {'Compressed size:':<{COL_WIDTH}} {format_size(total_compressed)}")
    print(f" {'Space saved:':<{COL_WIDTH}} {GREEN}{format_size(total_saved)}{RESET}")
    print(f" {'Compression:':<{COL_WIDTH}} {color_ratio(overall_ratio)}{overall_ratio:.1f}%{RESET} of original")
    print(f" {'Average ratio:':<{COL_WIDTH}} {mean(e['ratio'] for e in entries):.1f}%")

    print(f" {'Download time:':<{COL_WIDTH}} {format_time(total_download)}")
    print(f" {'Encoding time:':<{COL_WIDTH}} {format_time(total_encoding)}")

    # Average download time and encoding time
    print(f" {'Average download:':<{COL_WIDTH}} {format_time(total_download / len(entries))}")
    print(f" {'Average encoding:':<{COL_WIDTH}} {format_time(total_encoding / len(entries))}")

    # Average video length and size
    print(f" {'Average video size:':<{COL_WIDTH}} {format_size(total_original / len(entries))}")
    print(f" {'Average video length:':<{COL_WIDTH}} {format_time(total_download / len(entries))}")

    # Average download speed and encoding speed
    avg_download_speed = total_original / total_download if total_download > 0 else 0
    avg_encoding_speed = total_original / total_encoding if total_encoding > 0 else 0

    print(f" {'Average download speed:':<{COL_WIDTH}} {format_size(avg_download_speed)} / s")
    print(f" {'Average encoding speed:':<{COL_WIDTH}} {format_size(avg_encoding_speed)} / s")

    print(f"\n{DIM}======================================{RESET}\n")




if __name__ == "__main__":
    main()
