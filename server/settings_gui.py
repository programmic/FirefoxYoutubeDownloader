import json
import urllib.request
import urllib.error
import tkinter as tk
from tkinter import messagebox

import customtkinter as ctk

# appearance
ctk.set_appearance_mode("dark")
try:
    ctk.set_default_color_theme("blue")
except Exception:
    pass


def _get_settings():
    try:
        with urllib.request.urlopen('http://127.0.0.1:8765/settings', timeout=5) as resp:
            return json.load(resp)
    except Exception as e:
        return {"sponsorblock_enabled": False, "sponsorblock_categories": []}


def _post_settings(enabled, categories):
    payload = json.dumps({
        "sponsorblock_enabled": enabled,
        "sponsorblock_categories": categories,
    }).encode('utf-8')
    req = urllib.request.Request('http://127.0.0.1:8765/settings/update', data=payload,
                                 headers={"Content-Type": "application/json"}, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except Exception as e:
        print('Failed to post settings:', e)
        return False


def show_settings_window():
    cfg = _get_settings()
    root = ctk.CTk()
    root.title('Downloader Settings')
    root.geometry('480x340')

    main = ctk.CTkFrame(root, fg_color='#0b0f14')
    main.pack(fill='both', expand=True, padx=12, pady=12)

    header = ctk.CTkLabel(main, text='Settings', font=ctk.CTkFont(size=18, weight='bold'))
    header.pack(anchor='w', pady=(0, 8))

    sb_frame = ctk.CTkFrame(main, fg_color='transparent')
    sb_frame.pack(fill='x', pady=8)

    sb_toggle = ctk.CTkSwitch(sb_frame, text='Enable SponsorBlock', command=lambda v=None: None)
    sb_toggle.pack(side='left')
    if cfg.get('sponsorblock_enabled'):
        sb_toggle.select()

    cats_label = ctk.CTkLabel(main, text='SponsorBlock categories (one per line)', font=ctk.CTkFont(size=12))
    cats_label.pack(anchor='w', pady=(12, 6))

    cats_text = ctk.CTkTextbox(main, height=140)
    cats_text.pack(fill='both', expand=False)
    cats = cfg.get('sponsorblock_categories', [])
    cats_text.insert('0.0', '\n'.join(cats))

    def on_save():
        enabled = sb_toggle.get() == 1
        lines = cats_text.get('0.0', 'end').strip().splitlines()
        categories = [l.strip() for l in lines if l.strip()]
        ok = _post_settings(enabled, categories)
        if ok:
            messagebox.showinfo('Saved', 'Settings saved')
            root.destroy()
        else:
            messagebox.showerror('Error', 'Failed to save settings')

    btn_frame = ctk.CTkFrame(main, fg_color='transparent')
    btn_frame.pack(fill='x', pady=12)

    save_btn = ctk.CTkButton(btn_frame, text='Save', command=on_save)
    save_btn.pack(side='right', padx=6)
    cancel_btn = ctk.CTkButton(btn_frame, text='Cancel', command=root.destroy)
    cancel_btn.pack(side='right', padx=6)

    root.mainloop()
"""Settings GUI using CustomTkinter.

Provides a simple panel for updating SponsorBlock settings and writing
them back to `server/config.toml` via `server.config.update_sponsorblock()`.
"""
import customtkinter as ctk
import tkinter as tk
from tkinter import messagebox
from . import config as config_module


ctk.set_appearance_mode("dark")
try:
    ctk.set_default_color_theme("blue")
except Exception:
    pass


DEFAULT_CATEGORIES = [
    "sponsor",
    "selfpromo",
    "interaction",
    "intro",
    "outro",
    "preview",
    "music_offtopic",
]


def show_settings_window():
    cfg = getattr(config_module, 'CONFIG', {})
    sb_enabled = cfg.get('sponsorblock_enabled', True)
    sb_cats = cfg.get('sponsorblock_categories', DEFAULT_CATEGORIES)

    root = ctk.CTk()
    root.title('Downloader Settings')
    root.geometry('560x420')

    main = ctk.CTkFrame(root, fg_color="#0b0f14")
    main.pack(fill='both', expand=True, padx=12, pady=12)

    header = ctk.CTkLabel(main, text='Settings', font=ctk.CTkFont(size=18, weight='bold'))
    header.pack(anchor='w')

    # SponsorBlock section
    sb_frame = ctk.CTkFrame(main, fg_color='transparent')
    sb_frame.pack(fill='x', pady=(12,8))

    sb_label = ctk.CTkLabel(sb_frame, text='SponsorBlock', font=ctk.CTkFont(size=14, weight='bold'))
    sb_label.pack(anchor='w')

    enabled_var = tk.BooleanVar(value=sb_enabled)
    enabled_cb = ctk.CTkCheckBox(sb_frame, text='Enable SponsorBlock removal', variable=enabled_var)
    enabled_cb.pack(anchor='w', pady=6)

    cats_frame = ctk.CTkFrame(sb_frame, fg_color='transparent')
    cats_frame.pack(fill='both', pady=6)

    cat_vars = {}
    for cat in DEFAULT_CATEGORIES:
        var = tk.BooleanVar(value=(cat in sb_cats))
        cb = ctk.CTkCheckBox(cats_frame, text=cat, variable=var)
        cb.pack(anchor='w', padx=6, pady=2)
        cat_vars[cat] = var

    custom_entry = ctk.CTkEntry(sb_frame, placeholder_text='Add custom category and press Add')
    custom_entry.pack(fill='x', pady=(8,4))

    def add_custom():
        value = custom_entry.get().strip()
        if not value:
            return
        if value in cat_vars:
            messagebox.showinfo('Info', 'Category already exists')
            return
        var = tk.BooleanVar(value=True)
        cb = ctk.CTkCheckBox(cats_frame, text=value, variable=var)
        cb.pack(anchor='w', padx=6, pady=2)
        cat_vars[value] = var
        custom_entry.delete(0, tk.END)

    add_btn = ctk.CTkButton(sb_frame, text='Add', command=add_custom, width=80)
    add_btn.pack(anchor='e', pady=(0,6))

    # Save / Cancel
    btn_frame = ctk.CTkFrame(main, fg_color='transparent')
    btn_frame.pack(fill='x', pady=12)

    def on_save():
        enabled = bool(enabled_var.get())
        categories = [name for name, var in cat_vars.items() if var.get()]
        try:
            config_module.update_sponsorblock(enabled, categories)
            messagebox.showinfo('Saved', 'SponsorBlock settings saved')
        except Exception as e:
            messagebox.showerror('Error', f'Failed to save settings: {e}')
        root.destroy()

    def on_cancel():
        root.destroy()

    save_btn = ctk.CTkButton(btn_frame, text='Save', command=on_save, width=120)
    save_btn.pack(side='right', padx=6)

    cancel_btn = ctk.CTkButton(btn_frame, text='Cancel', command=on_cancel, width=120)
    cancel_btn.pack(side='right', padx=6)

    root.mainloop()
