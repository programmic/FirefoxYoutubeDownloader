"""select.py
CustomTkinter selection dialog to let the user choose which URLs to enqueue.

This runs in a separate thread from the FastAPI event loop. Selected URLs
are persisted to the database and then scheduled onto the asyncio queue via
`state.loop.call_soon_threadsafe(state.queue.put_nowait, url)` so the queue
operations run on the server's event loop.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import urllib.request
import urllib.error
import subprocess
import tkinter as tk
from tkinter import messagebox

import customtkinter as ctk
from urllib.parse import urlencode, urlparse, parse_qs, urlunparse

import os
import pathlib
import re

from . import config as server_config
from .download_worker import _fetch_video_title as _fetch_title
try:
    import tqdm
    _tqdm = tqdm.tqdm
except Exception:
    _tqdm = lambda x, **kw: x
    
from urllib.parse import urlparse, parse_qs


_EPISODE_PREFIX_PATTERNS = (
    re.compile(r'(?i)\bS(?P<season>\d{1,2})\s*E(?P<episode>\d{1,3})\b'),
    re.compile(r'(?i)\bEP(?:ISODE)?\.?\s*(?P<episode>\d{1,3})\b'),
    re.compile(r'(?i)(?<!\w)#\s*(?P<episode>\d{1,3})\b'),
)


# Appearance settings: dark + blue accent
ctk.set_appearance_mode("dark")
try:
    ctk.set_default_color_theme("blue")
except Exception:
    pass

def _check_url_name_databank(url: str | list[str]) -> str | None:
    """Check if the URL is in the name databank. If so, return the title."""
    if pathlib.Path(server_config.NAME_DATABANK_PATH).is_file():
        try:
            with open(server_config.NAME_DATABANK_PATH, 'r', encoding='utf-8') as f:
                databank = json.load(f)
                if isinstance(url, list):
                    return [databank.get(u) for u in url]
                elif isinstance(url, str):
                    return databank.get(url)
                else:
                    raise TypeError("[select/_check_url_name_databank]: url must be a string or a list of strings")
            return databank.get(url)
        except Exception as e:
            print('Failed to read name databank:', e)
    return None

def _write_title_to_databank(url, title):
    """Write the URL and title to the name databank."""
    databank = {}
    if pathlib.Path(server_config.NAME_DATABANK_PATH).is_file():
        try:
            with open(server_config.NAME_DATABANK_PATH, 'r', encoding='utf-8') as f:
                databank = json.load(f)
        except Exception as e:
            print('Failed to read name databank for writing:', e)
    short_url = urlparse(url).query.split('v=')[-1] if 'v=' in url else url # store only the video ID
    databank[short_url] = title
    try:
        with open(server_config.NAME_DATABANK_PATH, 'w', encoding='utf-8') as f:
            json.dump(databank, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print('Failed to write to name databank:', e)

def _process_url(url: str) -> str:
    """Clean up whitespace and remove unwanted YouTube URL parameters."""
    if not url:
        return ""

    url = re.sub(r'\s+', ' ', url.strip())

    parsed = urlparse(url)

    if (
        parsed.scheme in ("http", "https")
        and parsed.netloc in ("youtube.com", "www.youtube.com")
        and parsed.path == "/watch"
    ):
        query = parse_qs(parsed.query)

        # Keep only the video ID.
        if "v" in query:
            parsed = parsed._replace(
                query=urlencode({"v": query["v"][0]}),
                fragment=""
            )
            url = urlunparse(parsed)

    return url


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


def _post_selected_to_server(signal, selected):
    host = server_config.CONFIG.get('host', '127.0.0.1')
    port = server_config.CONFIG.get('port', 8765)
    target = f"http://{host}:{port}/select/{signal}"
    payload = json.dumps({"selected": selected}).encode('utf-8')
    req = urllib.request.Request(target, data=payload, headers={"Content-Type": "application/json"}, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return None, str(e).encode('utf-8')


def _get_title(url):
    url = _process_url(url)

    title = _check_url_name_databank(url)

    if not title or title == url or title.strip().upper() in {"NA", "N/A", "NONE"}:
        title = _fetch_title(url)

        if title and title != url and title.strip().upper() not in {"NA", "N/A", "NONE"}:
            _write_title_to_databank(url, title)

    return title


def show_selection_window(signal, urls):
    print("[SELECT] Fetching video titles...\n")

    titles = [None] * len(urls)

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(_get_title, url): i
            for i, url in enumerate(urls)
        }

        for future in _tqdm(
            as_completed(futures),
            total=len(futures),
            desc="Fetching titles",
            unit="video"
        ):
            i = futures[future]
            titles[i] = future.result()
    

    root = ctk.CTk()
    root.title(f"Select Videos — {signal}")
    root.geometry("720x520")

    main = ctk.CTkFrame(root, fg_color="#0b0f14")
    main.pack(fill='both', expand=True, padx=12, pady=12)

    header = ctk.CTkLabel(main, text="Select videos to enqueue", font=ctk.CTkFont(size=18, weight="bold"))
    header.pack(anchor='w', pady=(0, 8))

    sf = ctk.CTkScrollableFrame(main, width=680, height=380)
    sf.pack(fill='both', expand=True)

    check_items = []
    for title, url in zip(titles, urls):
        var = tk.BooleanVar(value=True)
        episode_prefix = _detect_episode_prefix(title)
        display_title = f"[EP] {title}" if episode_prefix else title
        item_frame = ctk.CTkFrame(sf, fg_color="#0b0f14")
        item_frame.pack(fill='x', pady=6, padx=4)

        cb = ctk.CTkCheckBox(item_frame, text=display_title, variable=var)
        cb.pack(side='top', anchor='w')

        small = ctk.CTkLabel(item_frame, text=url, font=ctk.CTkFont(size=11), text_color="#9fbfe6")
        small.pack(side='top', anchor='w', padx=(26, 0))

        check_items.append((var, url))

    btn_frame = ctk.CTkFrame(main, fg_color='transparent')
    btn_frame.pack(fill='x', pady=12)

    def select_all():
        for v, _ in check_items:
            v.set(True)

    def deselect_all():
        for v, _ in check_items:
            v.set(False)

    select_all_btn = ctk.CTkButton(btn_frame, text="Select All", command=select_all, width=140)
    select_all_btn.pack(side='left', padx=6)

    deselect_all_btn = ctk.CTkButton(btn_frame, text="Deselect All", command=deselect_all, width=140)
    deselect_all_btn.pack(side='left', padx=6)

    def on_enqueue():
        selected = [url for var, url in check_items if var.get()]
        if not selected:
            messagebox.showwarning("No Selection", "Please select at least one URL to enqueue.")
            return

        status, body = _post_selected_to_server(signal, selected)
        if status and 200 <= status < 300:
            messagebox.showinfo("Enqueued", f"Enqueued {len(selected)} URLs for download.")
            root.destroy()
            return

        # failure
        try:
            err = body.decode('utf-8')
        except Exception:
            err = str(body)
        messagebox.showerror("Failed", f"Failed to enqueue: {status} {err}")

    enqueue_btn = ctk.CTkButton(btn_frame, text="Enqueue Selected", command=on_enqueue, width=180)
    enqueue_btn.pack(side='right', padx=6)

    root.mainloop()