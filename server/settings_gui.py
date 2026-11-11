# settings_gui.py
"""Settings GUI using CustomTkinter.

Talks to the running server (/settings and /settings/update) instead of editing
config.toml itself, so the server reloads its CONFIG right away.
"""
import json
import urllib.request
import tkinter as tk
from tkinter import messagebox

import customtkinter as ctk

from . import config as config_module

ctk.set_appearance_mode("dark")
try:
    ctk.set_default_color_theme("blue")
except Exception:
    pass


def _base_url():
    host = config_module.CONFIG.get('host', '127.0.0.1')
    port = config_module.CONFIG.get('port', 8765)
    return f"http://{host}:{port}"


def _get_settings():
    req = urllib.request.Request(f"{_base_url()}/settings", headers=config_module.auth_headers())
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.load(resp)
    except Exception as e:
        print('Failed to read settings from the server:', e)
        return {"sponsorblock_enabled": False, "sponsorblock_categories": []}


def _post_settings(enabled, categories):
    payload = json.dumps({
        "sponsorblock_enabled": enabled,
        "sponsorblock_categories": categories,
    }).encode('utf-8')
    headers = {"Content-Type": "application/json", **config_module.auth_headers()}
    req = urllib.request.Request(f"{_base_url()}/settings/update", data=payload, headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except Exception as e:
        print('Failed to post settings:', e)
        return False


def show_settings_window():
    cfg = _get_settings()
    active = cfg.get('sponsorblock_categories', [])

    root = ctk.CTk()
    root.title('Downloader Settings')
    root.geometry('560x430')

    main = ctk.CTkFrame(root, fg_color="#0b0f14")
    main.pack(fill='both', expand=True, padx=12, pady=12)

    header = ctk.CTkLabel(main, text='Settings', font=ctk.CTkFont(size=18, weight='bold'))
    header.pack(anchor='w')

    sb_frame = ctk.CTkFrame(main, fg_color='transparent')
    sb_frame.pack(fill='x', pady=(12, 8))

    sb_label = ctk.CTkLabel(sb_frame, text='SponsorBlock', font=ctk.CTkFont(size=14, weight='bold'))
    sb_label.pack(anchor='w')

    enabled_var = tk.BooleanVar(value=bool(cfg.get('sponsorblock_enabled')))
    enabled_cb = ctk.CTkCheckBox(sb_frame, text='Enable SponsorBlock removal', variable=enabled_var)
    enabled_cb.pack(anchor='w', pady=6)

    # Only known categories: free-text input could be abused to inject into config.toml (S3)
    cat_vars = {}
    for cat in config_module.SPONSORBLOCK_CATEGORIES:
        var = tk.BooleanVar(value=(cat in active))
        ctk.CTkCheckBox(sb_frame, text=cat, variable=var).pack(anchor='w', padx=6, pady=2)
        cat_vars[cat] = var

    btn_frame = ctk.CTkFrame(main, fg_color='transparent')
    btn_frame.pack(fill='x', pady=12)

    def on_save():
        categories = [name for name, var in cat_vars.items() if var.get()]
        if _post_settings(bool(enabled_var.get()), categories):
            messagebox.showinfo('Saved', 'SponsorBlock settings saved')
            root.destroy()
        else:
            messagebox.showerror('Error', 'Failed to save settings (is the server running?)')

    save_btn = ctk.CTkButton(btn_frame, text='Save', command=on_save, width=120)
    save_btn.pack(side='right', padx=6)

    cancel_btn = ctk.CTkButton(btn_frame, text='Cancel', command=root.destroy, width=120)
    cancel_btn.pack(side='right', padx=6)

    root.mainloop()