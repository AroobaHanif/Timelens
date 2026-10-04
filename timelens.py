"""TimeLens - a local screen-time tracker.

Single-file desktop app built with tkinter (ttk + Canvas) and the Python
standard library only, so it can be frozen with PyInstaller later.
All data stays on the user's machine (sqlite3 in AppData).
"""

import os
import sys
import time
import json
import csv
import threading
import sqlite3
import ctypes
import winreg
from datetime import datetime, timedelta, date
from ctypes import wintypes

import tkinter as tk
from tkinter import ttk, messagebox, simpledialog


# --------------------------------------------------------------------------- #
# Theme + constants
# --------------------------------------------------------------------------- #
THEME = {
    "bg":            "#1e1e2e",
    "card":          "#313244",
    "card_hover":    "#45475a",
    "accent":        "#89b4fa",
    "productive":    "#a6e3a1",
    "communication": "#f9e2af",
    "entertainment": "#f38ba8",
    "other":         "#cba6f7",
    "text":          "#cdd6f4",
    "muted":         "#9399b2",
    "border":        "#45475a",
}

CATEGORY_COLORS = {
    "Productive":    THEME["productive"],
    "Communication": THEME["communication"],
    "Entertainment": THEME["entertainment"],
    "Other":         THEME["other"],
}

IDLE_THRESHOLD = 120      # seconds of inactivity before we stop counting
FLUSH_INTERVAL = 30       # seconds between db flushes
POLL_INTERVAL = 1         # seconds between tracker polls
DISTRACTION_DEFAULT = 30  # minutes of continuous entertainment before alert

DEFAULT_CATEGORIES = {
    # Productive
    "code.exe": "Productive", "pycharm64.exe": "Productive", "pycharm.exe": "Productive",
    "winword.exe": "Productive", "excel.exe": "Productive", "powerpnt.exe": "Productive",
    "devenv.exe": "Productive", "notepad++.exe": "Productive", "sublime_text.exe": "Productive",
    "idea64.exe": "Productive", "rstudio.exe": "Productive", "obsidian.exe": "Productive",
    "vim.exe": "Productive", "gvim.exe": "Productive",
    # Communication
    "whatsapp.exe": "Communication", "discord.exe": "Communication", "slack.exe": "Communication",
    "teams.exe": "Communication", "outlook.exe": "Communication", "telegram.exe": "Communication",
    "zoom.exe": "Communication", "skype.exe": "Communication", "thunderbird.exe": "Communication",
    # Entertainment
    "spotify.exe": "Entertainment", "vlc.exe": "Entertainment", "steam.exe": "Entertainment",
    "netflix.exe": "Entertainment", "youtube.exe": "Entertainment", "epicgameslauncher.exe": "Entertainment",
    "battle.net.exe": "Entertainment", "music.exe": "Entertainment",
}
BROWSERS = {"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe",
            "opera.exe", "iexplore.exe", "vivaldi.exe"}


# --------------------------------------------------------------------------- #
# Path helpers (PyInstaller friendly)
# --------------------------------------------------------------------------- #
def resource_path(relative_path):
    """Return absolute path to a bundled asset (works under PyInstaller)."""
    base = getattr(sys, "_MEIPASS", os.path.abspath("."))
    return os.path.join(base, relative_path)


ICON_FILE = "icon.ico"


def icon_path():
    """Absolute path to the bundled application icon."""
    return resource_path(ICON_FILE)


def apply_icon(window):
    """Set the custom .ico on a Tk window; ignore errors if missing/invalid."""
    try:
        window.iconbitmap(icon_path())
    except (tk.TclError, OSError):
        pass


def set_app_user_model_id(app_id="TimeLens.App"):
    """Give the process a distinct AppUserModelID so the taskbar shows our icon."""
    try:
        shell32 = ctypes.windll.shell32
        shell32.SetCurrentProcessExplicitAppUserModelID.argtypes = [ctypes.c_wchar_p]
        shell32.SetCurrentProcessExplicitAppUserModelID.restype = ctypes.c_long
        shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
        return True
    except (AttributeError, OSError):
        return False


def install_toplevel_icon_hook():
    """Make every new Toplevel (dialogs, etc.) inherit the app icon."""
    _orig_init = tk.Toplevel.__init__

    def _patched(self, *args, **kwargs):
        _orig_init(self, *args, **kwargs)
        apply_icon(self)

    tk.Toplevel.__init__ = _patched


def data_path():
    """Return the AppData folder where the db / json live."""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    folder = os.path.join(base, "TimeLens")
    os.makedirs(folder, exist_ok=True)
    return folder


# Registry key for the "run at logon" entry.
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "TimeLens"


def startup_command():
    """Command line to launch TimeLens at logon (exe when frozen, pythonw + script otherwise)."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    # Not frozen: use pythonw (no console) + this script's absolute path.
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = sys.executable
    return f'"{pythonw}" "{os.path.abspath(__file__)}"'


def is_start_with_windows():
    """True if the Run entry for TimeLens exists under HKCU."""
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ)
        try:
            winreg.QueryValueEx(key, RUN_VALUE)
            return True
        except FileNotFoundError:
            return False
        finally:
            winreg.CloseKey(key)
    except OSError:
        return False


def set_start_with_windows(enable):
    """Add or remove the HKCU Run entry. Returns True on success."""
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                             winreg.KEY_SET_VALUE)
        try:
            if enable:
                winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ,
                                  startup_command())
            else:
                try:
                    winreg.DeleteValue(key, RUN_VALUE)
                except FileNotFoundError:
                    pass
        finally:
            winreg.CloseKey(key)
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# Win32 bindings
# --------------------------------------------------------------------------- #
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetLastInputInfo.argtypes = [ctypes.c_void_p]
user32.GetLastInputInfo.restype = wintypes.BOOL


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def get_idle_seconds():
    """Seconds since the last keyboard / mouse input."""
    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(lii)
    if not user32.GetLastInputInfo(ctypes.byref(lii)):
        return 0.0
    now = kernel32.GetTickCount()
    return (now - lii.dwTime) / 1000.0


def get_foreground_info():
    """Return (exe_name, window_title) for the foreground window, or (None, None)."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None, None
    title_buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, title_buf, 512)
    title = title_buf.value or ""

    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return None, title

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return None, title
    try:
        path_buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(handle, 0, path_buf, ctypes.byref(size)):
            return os.path.basename(path_buf.value).lower(), title
    finally:
        kernel32.CloseHandle(handle)
    return None, title


# --------------------------------------------------------------------------- #
# Tracker (background thread)
# --------------------------------------------------------------------------- #
class Tracker(threading.Thread):
    """Polls the foreground window every second and accumulates seconds per app."""

    def __init__(self, app):
        super().__init__(daemon=True)
        self.app = app
        self.paused = False
        self.running = True
        self._buffer = {}          # (date, app, title) -> seconds (float)
        self._last_flush = time.time()
        self._last_exe = None
        self._continuous_entertainment_start = None
        self._entertainment_alerted = False
        # Flags the GUI main thread reads via after()
        self.goal_reached_flag = False
        self.distraction_flag = None  # exe name when triggered

    def stop(self):
        self.running = False

    def toggle_pause(self, paused):
        self.paused = paused

    def _accumulate(self, exe, title, seconds):
        if not exe:
            exe = "unknown"
        key = (date.today().isoformat(), exe, title)
        self._buffer[key] = self._buffer.get(key, 0.0) + seconds

    def _flush(self):
        if not self._buffer:
            return
        items = list(self._buffer.items())
        self._buffer.clear()
        self.app.db_save(items)

    def run(self):
        while self.running:
            time.sleep(POLL_INTERVAL)
            if self.paused:
                self._continuous_entertainment_start = None
                self._entertainment_alerted = False
                continue

            idle = get_idle_seconds()
            if idle >= IDLE_THRESHOLD:
                # User is idle: don't count, reset continuous tracking.
                self._last_exe = None
                self._continuous_entertainment_start = None
                self._entertainment_alerted = False
                continue

            exe, title = get_foreground_info()
            if exe is None:
                self._last_exe = None
                continue

            # Privacy option: keep window titles out of the database.
            if not self.app.store_titles:
                title = ""

            self._accumulate(exe, title, POLL_INTERVAL)

            # Distraction detection: continuous entertainment usage.
            category = self.app.classify(exe)
            if category == "Entertainment":
                if self._continuous_entertainment_start is None:
                    self._continuous_entertainment_start = time.time()
                    self._entertainment_alerted = False
                elapsed_min = (time.time() - self._continuous_entertainment_start) / 60.0
                if (elapsed_min >= self.app.distraction_minutes
                        and not self._entertainment_alerted):
                    self.distraction_flag = exe
                    self._entertainment_alerted = True
            else:
                self._continuous_entertainment_start = None
                self._entertainment_alerted = False

            # Goal reached flag (productive time today >= goal).
            self.app.maybe_set_goal_flag()

            # Periodic flush.
            if time.time() - self._last_flush >= FLUSH_INTERVAL:
                self._flush()
                self._last_flush = time.time()

        # Final flush on exit.
        self._flush()


# --------------------------------------------------------------------------- #
# Main application
# --------------------------------------------------------------------------- #
class TimeLensApp:
    def __init__(self, root):
        self.root = root
        self.tracker = None
        self.goal_hours = 4.0
        self.distraction_minutes = DISTRACTION_DEFAULT
        self.store_titles = True
        self.start_with_windows = False
        self.goal_already_celebrated_today = False
        self._title_flashing = False

        # User settings (goal, distraction, privacy, startup).
        self._load_settings()

        # Categories: default + user overrides.
        self.categories = dict(DEFAULT_CATEGORIES)
        self._load_categories()

        # Database.
        self.db_path = os.path.join(data_path(), "timelens.db")
        self._init_db()

        # Build UI.
        self._build_style()
        self._build_layout()
        self._build_header()
        self._build_controls()
        self._build_tabs()

        # Start tracker + periodic refresh.
        self.tracker = Tracker(self)
        self.tracker.start()
        self.root.after(2000, self._refresh_loop)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ----------------------- paths / storage ----------------------- #
    def _categories_path(self):
        return os.path.join(data_path(), "categories.json")

    def _load_categories(self):
        try:
            with open(self._categories_path(), "r", encoding="utf-8") as f:
                self.categories.update(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    def _save_categories(self):
        try:
            with open(self._categories_path(), "w", encoding="utf-8") as f:
                json.dump(self.categories, f, indent=2)
        except OSError:
            pass

    # ----------------------- settings ----------------------- #
    def _settings_path(self):
        return os.path.join(data_path(), "settings.json")

    def _load_settings(self):
        try:
            with open(self._settings_path(), "r", encoding="utf-8") as f:
                s = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return
        self.goal_hours = float(s.get("goal_hours", self.goal_hours))
        self.distraction_minutes = int(s.get("distraction_minutes",
                                             self.distraction_minutes))
        self.store_titles = bool(s.get("store_titles", True))
        # Prefer the actual registry state over the stored flag.
        self.start_with_windows = is_start_with_windows()

    def _save_settings(self):
        data = {
            "goal_hours": self.goal_hours,
            "distraction_minutes": self.distraction_minutes,
            "store_titles": self.store_titles,
            "start_with_windows": self.start_with_windows,
        }
        try:
            with open(self._settings_path(), "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except OSError:
            pass

    def classify(self, exe):
        if not exe:
            return "Other"
        exe = exe.lower()
        if exe in self.categories:
            return self.categories[exe]
        if exe in BROWSERS:
            return "Other"
        return "Other"

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS activity (
                date    TEXT NOT NULL,
                app     TEXT NOT NULL,
                title   TEXT NOT NULL,
                seconds REAL NOT NULL,
                source  TEXT NOT NULL DEFAULT 'real'
            )
        """)
        # Migrate older databases that lack the source column.
        cols = [r[1] for r in conn.execute("PRAGMA table_info(activity)").fetchall()]
        if "source" not in cols:
            conn.execute(
                "ALTER TABLE activity ADD COLUMN source TEXT NOT NULL DEFAULT 'real'")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_date ON activity(date)")
        conn.commit()
        conn.close()

    def db_save(self, items, source="real"):
        """Persist a list of ((date, app, title), seconds), tagged with a source."""
        if not items:
            return
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        for (d, app, title), secs in items:
            cur.execute(
                "INSERT INTO activity (date, app, title, seconds, source) "
                "VALUES (?, ?, ?, ?, ?)",
                (d, app, title, secs, source),
            )
        conn.commit()
        conn.close()

    def query(self, sql, params=()):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall()
        conn.close()
        return rows

    def clear_all_data(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("DELETE FROM activity")
        conn.commit()
        conn.close()

    # ----------------------- style ----------------------- #
    def _build_style(self):
        self.root.configure(bg=THEME["bg"])
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(".", background=THEME["bg"], foreground=THEME["text"],
                        font=("Segoe UI", 10))
        style.configure("TFrame", background=THEME["bg"])
        style.configure("Card.TFrame", background=THEME["card"])
        style.configure("TLabel", background=THEME["bg"], foreground=THEME["text"])
        style.configure("Card.TLabel", background=THEME["card"], foreground=THEME["text"])
        style.configure("Title.TLabel", background=THEME["bg"],
                        foreground=THEME["accent"], font=("Segoe UI Semibold", 22))
        style.configure("Tagline.TLabel", background=THEME["bg"],
                        foreground=THEME["muted"], font=("Segoe UI", 10))
        style.configure("Muted.TLabel", background=THEME["bg"],
                        foreground=THEME["muted"], font=("Segoe UI", 9))
        style.configure("CardTitle.TLabel", background=THEME["card"],
                        foreground=THEME["text"], font=("Segoe UI Semibold", 12))
        style.configure("Big.TLabel", background=THEME["card"],
                        foreground=THEME["accent"], font=("Segoe UI Semibold", 26))
        style.configure("Score.TLabel", background=THEME["card"],
                        foreground=THEME["productive"], font=("Segoe UI Semibold", 30))

        # Flat buttons: no border, no focus outline, hover fill.
        style.configure("TButton", background=THEME["card"], foreground=THEME["text"],
                        borderwidth=0, relief="flat", focuscolor=THEME["card"],
                        bordercolor=THEME["card"], lightcolor=THEME["card"],
                        darkcolor=THEME["card"], font=("Segoe UI", 10),
                        padding=(12, 6))
        style.map("TButton",
                  background=[("active", THEME["card_hover"]),
                              ("pressed", THEME["accent"])],
                  foreground=[("pressed", THEME["bg"])])

        style.configure("Accent.TButton", background=THEME["accent"],
                        foreground=THEME["bg"], borderwidth=0, relief="flat",
                        focuscolor=THEME["accent"], bordercolor=THEME["accent"],
                        lightcolor=THEME["accent"], darkcolor=THEME["accent"],
                        font=("Segoe UI Semibold", 10), padding=(12, 6))
        style.map("Accent.TButton",
                  background=[("active", "#b4d4fc"), ("pressed", "#74a8f0")],
                  foreground=[("pressed", THEME["bg"])])

        style.configure("Danger.TButton", background=THEME["entertainment"],
                        foreground=THEME["bg"], borderwidth=0, relief="flat",
                        focuscolor=THEME["entertainment"],
                        bordercolor=THEME["entertainment"],
                        lightcolor=THEME["entertainment"],
                        darkcolor=THEME["entertainment"], padding=(12, 6))
        style.map("Danger.TButton",
                  background=[("active", "#f6a5b5"), ("pressed", "#e5798f")],
                  foreground=[("pressed", THEME["bg"])])

        # Notebook: card-colored tabs, no dotted focus outline.
        style.configure("TNotebook", background=THEME["bg"], borderwidth=0,
                        tabmargins=(4, 6, 4, 0))
        style.configure("TNotebook.Tab", background=THEME["card"],
                        foreground=THEME["muted"], borderwidth=0,
                        focuscolor=THEME["card"], bordercolor=THEME["card"],
                        lightcolor=THEME["card"], darkcolor=THEME["card"],
                        padding=(18, 8), font=("Segoe UI", 10))
        style.map("TNotebook.Tab",
                  background=[("selected", THEME["accent"]),
                              ("active", THEME["card_hover"])],
                  foreground=[("selected", THEME["bg"]),
                              ("active", THEME["text"])])

        # Checkbuttons for the option toggles.
        style.configure("TCheckbutton", background=THEME["bg"],
                        foreground=THEME["text"], borderwidth=0, relief="flat",
                        focuscolor=THEME["bg"], bordercolor=THEME["bg"],
                        lightcolor=THEME["card"], darkcolor=THEME["card"],
                        font=("Segoe UI", 10))
        style.map("TCheckbutton",
                  background=[("active", THEME["bg"])],
                  foreground=[("active", THEME["text"])])

        # Treeview
        style.configure("Treeview", background=THEME["card"],
                        fieldbackground=THEME["card"], foreground=THEME["text"],
                        borderwidth=0, font=("Segoe UI", 10), rowheight=26)
        style.map("Treeview",
                  background=[("selected", THEME["accent"])],
                  foreground=[("selected", THEME["bg"])])
        style.configure("Treeview.Heading", background=THEME["card_hover"],
                        foreground=THEME["text"], borderwidth=0, relief="flat",
                        font=("Segoe UI Semibold", 10), padding=(8, 6))
        style.map("Treeview.Heading",
                  background=[("active", THEME["accent"])],
                  foreground=[("active", THEME["bg"])])

        # Entry: flat, no focus ring.
        style.configure("TEntry", fieldbackground=THEME["card"],
                        foreground=THEME["text"], insertcolor=THEME["text"],
                        borderwidth=0, relief="flat", bordercolor=THEME["card"],
                        lightcolor=THEME["card"], darkcolor=THEME["card"],
                        focuscolor=THEME["card"], padding=6)

    # ----------------------- layout ----------------------- #
    def _build_layout(self):
        self.root.title("TimeLens")
        self.root.geometry("1000x650")
        self.root.minsize(860, 560)
        self._center_window(1000, 650)

        self.container = ttk.Frame(self.root)
        self.container.pack(fill="both", expand=True, padx=18, pady=14)

    def _center_window(self, w, h):
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = (sw - w) // 2
        y = (sh - h) // 2
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def _build_header(self):
        header = ttk.Frame(self.container)
        header.pack(fill="x", pady=(0, 12))

        ttk.Label(header, text="TimeLens", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text="  See where your time really goes.",
                  style="Tagline.TLabel").pack(side="left", pady=(10, 0))

    def _build_controls(self):
        bar = ttk.Frame(self.container)
        bar.pack(fill="x", pady=(0, 8))

        self.pause_var = tk.BooleanVar(value=False)
        self.pause_btn = ttk.Button(bar, text="Pause tracking",
                                    command=self.toggle_pause)
        self.pause_btn.pack(side="left")

        ttk.Button(bar, text="Set focus goal",
                   command=self.set_focus_goal).pack(side="left", padx=6)
        ttk.Button(bar, text="Distraction alert",
                   command=self.set_distraction_minutes).pack(side="left")
        ttk.Button(bar, text="Load demo data",
                   command=self.load_demo_data).pack(side="left", padx=6)
        ttk.Button(bar, text="About",
                   command=self.show_about).pack(side="left")

        ttk.Button(bar, text="Clear all data", style="Danger.TButton",
                   command=self.confirm_clear_data).pack(side="right")

        # Options row: startup toggle + privacy toggle.
        opts = ttk.Frame(self.container)
        opts.pack(fill="x", pady=(0, 12))

        self.startup_var = tk.BooleanVar(value=self.start_with_windows)
        ttk.Checkbutton(opts, text="Start with Windows",
                        variable=self.startup_var,
                        command=self.on_toggle_startup).pack(side="left")

        self.no_titles_var = tk.BooleanVar(value=not self.store_titles)
        ttk.Checkbutton(opts, text="Don't store window titles",
                        variable=self.no_titles_var,
                        command=self.on_toggle_store_titles).pack(side="left",
                                                                   padx=12)
        ttk.Label(opts, text="(extra privacy: only app names are saved)",
                  style="Muted.TLabel").pack(side="left")

    def _build_tabs(self):
        self.notebook = ttk.Notebook(self.container)
        self.notebook.pack(fill="both", expand=True)
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        self.today_frame = ttk.Frame(self.notebook)
        self.week_frame = ttk.Frame(self.notebook)
        self.apps_frame = ttk.Frame(self.notebook)
        self.notebook.add(self.today_frame, text="Today")
        self.notebook.add(self.week_frame, text="This Week")
        self.notebook.add(self.apps_frame, text="All Apps")

        self._build_today_tab()
        self._build_week_tab()
        self._build_apps_tab()

    # ----------------------- Today tab ----------------------- #
    def _build_today_tab(self):
        top = ttk.Frame(self.today_frame)
        top.pack(fill="x", pady=(0, 12))

        # Total active time card
        self.total_card = ttk.Frame(top, style="Card.TFrame")
        self.total_card.pack(side="left", fill="both", expand=True, padx=(0, 8))
        self._style_card(self.total_card)
        ttk.Label(self.total_card, text="Total active time",
                  style="CardTitle.TLabel").pack(anchor="w", padx=16, pady=(14, 0))
        self.total_label = ttk.Label(self.total_card, text="0m",
                                     style="Big.TLabel")
        self.total_label.pack(anchor="w", padx=16, pady=(2, 14))

        # Productivity score card
        self.score_card = ttk.Frame(top, style="Card.TFrame")
        self.score_card.pack(side="left", fill="both", expand=True, padx=8)
        self._style_card(self.score_card)
        ttk.Label(self.score_card, text="Productivity score",
                  style="CardTitle.TLabel").pack(anchor="w", padx=16, pady=(14, 0))
        self.score_label = ttk.Label(self.score_card, text="0", style="Score.TLabel")
        self.score_label.pack(anchor="w", padx=16, pady=(2, 4))
        self.score_hint = ttk.Label(self.score_card, text="productive / total",
                                    style="Muted.TLabel", background=THEME["card"])
        self.score_hint.pack(anchor="w", padx=16, pady=(0, 14))

        # Focus goal ring card
        self.goal_card = ttk.Frame(top, style="Card.TFrame")
        self.goal_card.pack(side="left", fill="both", expand=True, padx=(8, 0))
        self._style_card(self.goal_card)
        ttk.Label(self.goal_card, text="Focus goal",
                  style="CardTitle.TLabel").pack(anchor="w", padx=16, pady=(14, 0))
        ring_wrap = ttk.Frame(self.goal_card, style="Card.TFrame")
        ring_wrap.pack(anchor="w", padx=10, pady=(2, 14))
        self.goal_canvas = tk.Canvas(ring_wrap, width=90, height=90,
                                     bg=THEME["card"], highlightthickness=0)
        self.goal_canvas.pack(side="left", padx=6)
        self.goal_text = ttk.Label(ring_wrap, text="", style="Muted.TLabel",
                                   background=THEME["card"])
        self.goal_text.pack(side="left")

        # Charts row
        charts = ttk.Frame(self.today_frame)
        charts.pack(fill="both", expand=True)

        # Top apps bar chart
        bar_card = ttk.Frame(charts, style="Card.TFrame")
        bar_card.pack(side="left", fill="both", expand=True, padx=(0, 6))
        self._style_card(bar_card)
        ttk.Label(bar_card, text="Top apps today",
                  style="CardTitle.TLabel").pack(anchor="w", padx=16, pady=(12, 4))
        self.bar_canvas = tk.Canvas(bar_card, bg=THEME["card"], highlightthickness=0)
        self.bar_canvas.pack(fill="both", expand=True, padx=10, pady=(0, 12))

        # Donut by category
        donut_card = ttk.Frame(charts, style="Card.TFrame")
        donut_card.pack(side="left", fill="both", expand=True, padx=(6, 0))
        self._style_card(donut_card)
        ttk.Label(donut_card, text="Time by category",
                  style="CardTitle.TLabel").pack(anchor="w", padx=16, pady=(12, 4))
        self.donut_canvas = tk.Canvas(donut_card, bg=THEME["card"],
                                      highlightthickness=0)
        self.donut_canvas.pack(fill="both", expand=True, padx=10, pady=(0, 12))
        self.donut_canvas.bind("<Configure>", lambda e: self.draw_today())

    def _style_card(self, frame):
        frame.configure(style="Card.TFrame")

    # ----------------------- Week tab ----------------------- #
    def _build_week_tab(self):
        stats = ttk.Frame(self.week_frame)
        stats.pack(fill="x", pady=(0, 12))

        self.best_card = ttk.Frame(stats, style="Card.TFrame")
        self.best_card.pack(side="left", fill="both", expand=True, padx=(0, 8))
        self._style_card(self.best_card)
        ttk.Label(self.best_card, text="Best day",
                  style="CardTitle.TLabel").pack(anchor="w", padx=16, pady=(14, 0))
        self.best_label = ttk.Label(self.best_card, text="-", style="Big.TLabel")
        self.best_label.pack(anchor="w", padx=16, pady=(2, 14))

        self.streak_card = ttk.Frame(stats, style="Card.TFrame")
        self.streak_card.pack(side="left", fill="both", expand=True, padx=8)
        self._style_card(self.streak_card)
        ttk.Label(self.streak_card, text="Productivity streak",
                  style="CardTitle.TLabel").pack(anchor="w", padx=16, pady=(14, 0))
        self.streak_label = ttk.Label(self.streak_card, text="0 days",
                                      style="Big.TLabel")
        self.streak_label.pack(anchor="w", padx=16, pady=(2, 14))

        self.week_total_card = ttk.Frame(stats, style="Card.TFrame")
        self.week_total_card.pack(side="left", fill="both", expand=True, padx=(8, 0))
        self._style_card(self.week_total_card)
        ttk.Label(self.week_total_card, text="Week total",
                  style="CardTitle.TLabel").pack(anchor="w", padx=16, pady=(14, 0))
        self.week_total_label = ttk.Label(self.week_total_card, text="0h",
                                          style="Big.TLabel")
        self.week_total_label.pack(anchor="w", padx=16, pady=(2, 14))

        chart_card = ttk.Frame(self.week_frame, style="Card.TFrame")
        chart_card.pack(fill="both", expand=True)
        self._style_card(chart_card)
        ttk.Label(chart_card, text="Last 7 days (stacked by category)",
                  style="CardTitle.TLabel").pack(anchor="w", padx=16, pady=(12, 4))
        self.week_canvas = tk.Canvas(chart_card, bg=THEME["card"], highlightthickness=0)
        self.week_canvas.pack(fill="both", expand=True, padx=10, pady=(0, 12))
        self.week_canvas.bind("<Configure>", lambda e: self.draw_week())

    # ----------------------- All Apps tab ----------------------- #
    def _build_apps_tab(self):
        top = ttk.Frame(self.apps_frame)
        top.pack(fill="x", pady=(0, 10))

        ttk.Label(top, text="Search:").pack(side="left")
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *a: self.refresh_apps_tree())
        search_entry = ttk.Entry(top, textvariable=self.search_var, width=28)
        search_entry.pack(side="left", padx=8)
        ttk.Button(top, text="Export CSV", style="Accent.TButton",
                   command=self.export_csv).pack(side="right")

        tree_card = ttk.Frame(self.apps_frame, style="Card.TFrame")
        tree_card.pack(fill="both", expand=True)
        self._style_card(tree_card)

        cols = ("app", "category", "time", "percent")
        self.tree = ttk.Treeview(tree_card, columns=cols, show="headings",
                                 selectmode="browse")
        self.tree.heading("app", text="App")
        self.tree.heading("category", text="Category")
        self.tree.heading("time", text="Total time")
        self.tree.heading("percent", text="% of total")
        self.tree.column("app", width=220, anchor="w")
        self.tree.column("category", width=130, anchor="w")
        self.tree.column("time", width=140, anchor="e")
        self.tree.column("percent", width=90, anchor="e")
        self.tree.pack(fill="both", expand=True, padx=10, pady=10)

        # Right-click menu to change category.
        self.ctx_menu = tk.Menu(self.root, tearoff=0, bg=THEME["card"],
                                fg=THEME["text"], activebackground=THEME["accent"],
                                activeforeground=THEME["bg"],
                                borderwidth=0)
        self.tree.bind("<Button-3>", self._on_tree_right_click)

    # ----------------------- data queries ----------------------- #
    def today_iso(self):
        return date.today().isoformat()

    def get_app_totals(self, day_iso=None):
        if day_iso is None:
            day_iso = self.today_iso()
        rows = self.query(
            "SELECT app, SUM(seconds) AS s FROM activity WHERE date=? "
            "GROUP BY app ORDER BY s DESC",
            (day_iso,),
        )
        return [(r["app"], r["s"] or 0) for r in rows]

    def get_category_totals(self, day_iso=None):
        if day_iso is None:
            day_iso = self.today_iso()
        rows = self.query(
            "SELECT app, SUM(seconds) AS s FROM activity WHERE date=? GROUP BY app",
            (day_iso,),
        )
        cats = {k: 0.0 for k in CATEGORY_COLORS}
        for r in rows:
            cats[self.classify(r["app"])] += r["s"] or 0
        return cats

    def get_total_seconds(self, day_iso=None):
        if day_iso is None:
            day_iso = self.today_iso()
        rows = self.query(
            "SELECT SUM(seconds) AS s FROM activity WHERE date=?", (day_iso,))
        return (rows[0]["s"] or 0) if rows else 0

    def get_week_data(self):
        """Return list of (iso_date, {category: seconds}) for last 7 days."""
        today = date.today()
        out = []
        for i in range(6, -1, -1):
            d = today - timedelta(days=i)
            iso = d.isoformat()
            cats = self.get_category_totals(iso)
            out.append((iso, d.strftime("%a"), cats))
        return out

    def get_all_apps(self):
        rows = self.query(
            "SELECT app, SUM(seconds) AS s FROM activity GROUP BY app ORDER BY s DESC")
        return [(r["app"], r["s"] or 0) for r in rows]

    def get_productive_seconds(self, day_iso=None):
        cats = self.get_category_totals(day_iso)
        return cats.get("Productive", 0.0)

    # ----------------------- drawing: Today ----------------------- #
    def draw_today(self):
        total = self.get_total_seconds()
        self.total_label.configure(text=self._fmt_duration(total))

        productive = self.get_productive_seconds()
        score = int((productive / total * 100) if total > 0 else 0)
        self.score_label.configure(text=str(score))

        self.draw_goal_ring(productive)
        self.draw_top_apps()
        self.draw_donut()

    def draw_goal_ring(self, productive_seconds):
        goal_seconds = self.goal_hours * 3600
        progress = min(1.0, productive_seconds / goal_seconds) if goal_seconds > 0 else 0
        c = self.goal_canvas
        c.delete("all")
        w, h = 90, 90
        r = 36
        x0, y0 = w / 2 - r, h / 2 - r
        x1, y1 = w / 2 + r, h / 2 + r
        # Background ring
        c.create_oval(x0, y0, x1, y1, outline=THEME["card_hover"], width=8)
        # Progress arc
        if progress > 0:
            extent = 360 * progress
            c.create_arc(x0, y0, x1, y1, start=90, extent=-extent,
                         style="arc", outline=THEME["accent"], width=8)
        # Center text
        c.create_text(w / 2, h / 2 - 6, text=f"{int(progress * 100)}%",
                      fill=THEME["text"], font=("Segoe UI Semibold", 12))
        c.create_text(w / 2, h / 2 + 12, text="of goal",
                      fill=THEME["muted"], font=("Segoe UI", 8))
        self.goal_text.configure(
            text=f"{self.goal_hours:.1f}h goal\n{self._fmt_duration(productive_seconds)} done")

    def draw_top_apps(self):
        apps = self.get_app_totals()[:8]
        c = self.bar_canvas
        c.delete("all")
        c.update_idletasks()
        w = max(c.winfo_width(), 100)
        h = max(c.winfo_height(), 100)
        if not apps:
            c.create_text(w / 2, h / 2, text="No data yet",
                          fill=THEME["muted"], font=("Segoe UI", 11))
            return
        max_s = max(a[1] for a in apps) or 1
        pad_x, pad_top, pad_bot = 16, 10, 10
        label_w = 130
        bar_area_w = w - label_w - pad_x - 60
        n = len(apps)
        row_h = (h - pad_top - pad_bot) / n
        bar_h = min(row_h * 0.6, 22)
        for i, (app, secs) in enumerate(apps):
            y = pad_top + i * row_h + row_h / 2
            # Label
            c.create_text(pad_x, y, anchor="w", text=self._short(app, 18),
                          fill=THEME["text"], font=("Segoe UI", 9))
            # Bar
            bw = max(2, (secs / max_s) * bar_area_w)
            bx0 = pad_x + label_w
            color = CATEGORY_COLORS[self.classify(app)]
            c.create_rectangle(bx0, y - bar_h / 2, bx0 + bw, y + bar_h / 2,
                               fill=color, outline="")
            c.create_text(bx0 + bw + 6, y, anchor="w",
                          text=self._fmt_duration(secs),
                          fill=THEME["muted"], font=("Segoe UI", 9))

    def draw_donut(self):
        cats = self.get_category_totals()
        c = self.donut_canvas
        c.delete("all")
        c.update_idletasks()
        w = max(c.winfo_width(), 100)
        h = max(c.winfo_height(), 100)
        total = sum(cats.values())
        if total <= 0:
            c.create_text(w / 2, h / 2, text="No data yet",
                          fill=THEME["muted"], font=("Segoe UI", 11))
            return
        cx, cy = w * 0.32, h / 2
        r = min(w * 0.30, h * 0.40)
        start = 90
        for cat, secs in cats.items():
            if secs <= 0:
                continue
            extent = -360 * secs / total
            c.create_arc(cx - r, cy - r, cx + r, cy + r, start=start,
                         extent=extent, style="pieslice",
                         fill=CATEGORY_COLORS[cat], outline=THEME["card"])
            start += extent
        # Donut hole
        hole = r * 0.58
        c.create_oval(cx - hole, cy - hole, cx + hole, cy + hole,
                      fill=THEME["card"], outline="")
        c.create_text(cx, cy, text=self._fmt_duration(total),
                      fill=THEME["text"], font=("Segoe UI Semibold", 11))
        # Legend
        lx = cx + r + 24
        ly = cy - r
        for cat, secs in cats.items():
            color = CATEGORY_COLORS[cat]
            c.create_rectangle(lx, ly, lx + 12, ly + 12, fill=color, outline="")
            c.create_text(lx + 18, ly + 6, anchor="w",
                          text=f"{cat}  {self._fmt_duration(secs)}",
                          fill=THEME["text"], font=("Segoe UI", 9))
            ly += 22

    # ----------------------- drawing: Week ----------------------- #
    def draw_week(self):
        data = self.get_week_data()
        c = self.week_canvas
        c.delete("all")
        c.update_idletasks()
        w = max(c.winfo_width(), 100)
        h = max(c.winfo_height(), 100)
        if not data:
            return
        max_day = max(sum(cats.values()) for _, _, cats in data) or 1
        pad_l, pad_r, pad_top, pad_bot = 50, 20, 16, 40
        chart_w = w - pad_l - pad_r
        chart_h = h - pad_top - pad_bot
        n = len(data)
        bar_w = chart_w / n * 0.6
        gap = chart_w / n * 0.4

        # Y axis gridlines
        for frac in (0.25, 0.5, 0.75, 1.0):
            y = pad_top + chart_h - chart_h * frac
            c.create_line(pad_l, y, w - pad_r, y, fill=THEME["card_hover"], dash=(2, 4))
            c.create_text(pad_l - 6, y, anchor="e",
                          text=self._fmt_duration(max_day * frac),
                          fill=THEME["muted"], font=("Segoe UI", 8))

        cats_order = ["Productive", "Communication", "Entertainment", "Other"]
        for i, (iso, label, cats) in enumerate(data):
            x = pad_l + i * (bar_w + gap) + gap / 2
            y_bottom = pad_top + chart_h
            y_top = y_bottom
            for cat in cats_order:
                secs = cats.get(cat, 0)
                if secs <= 0:
                    continue
                seg_h = (secs / max_day) * chart_h
                y_top2 = y_top - seg_h
                c.create_rectangle(x, y_top2, x + bar_w, y_top,
                                   fill=CATEGORY_COLORS[cat], outline="")
                y_top = y_top2
            c.create_text(x + bar_w / 2, y_bottom + 14, text=label,
                          fill=THEME["muted"], font=("Segoe UI", 9))
            day_total = sum(cats.values())
            if day_total > 0:
                c.create_text(x + bar_w / 2, y_top - 8,
                              text=self._fmt_duration(day_total, short=True),
                              fill=THEME["text"], font=("Segoe UI", 8))

        # Stats
        best_idx = max(range(n), key=lambda i: sum(data[i][2].values()))
        best_total = sum(data[best_idx][2].values())
        if best_total > 0:
            best_date = date.fromisoformat(data[best_idx][0]).strftime("%a %b %d")
            self.best_label.configure(text=best_date)
        else:
            self.best_label.configure(text="-")
        week_total = sum(sum(d[2].values()) for d in data)
        self.week_total_label.configure(text=self._fmt_duration(week_total, short=True))
        self.streak_label.configure(text=f"{self.compute_streak(data)} days")

    def compute_streak(self, week_data):
        """Consecutive days (ending today) with productivity score > 60."""
        streak = 0
        for iso, _, cats in reversed(week_data):
            total = sum(cats.values())
            if total <= 0:
                break
            score = (cats.get("Productive", 0) / total) * 100
            if score > 60:
                streak += 1
            else:
                break
        return streak

    # ----------------------- All Apps tree ----------------------- #
    def refresh_apps_tree(self):
        apps = self.get_all_apps()
        total = sum(s for _, s in apps) or 1
        q = self.search_var.get().lower().strip()
        for item in self.tree.get_children():
            self.tree.delete(item)
        for app, secs in apps:
            if q and q not in app.lower():
                continue
            cat = self.classify(app)
            pct = secs / total * 100
            self.tree.insert("", "end", iid=app,
                             values=(app, cat, self._fmt_duration(secs),
                                     f"{pct:.1f}%"))

    def _on_tree_right_click(self, event):
        row = self.tree.identify_row(event.y)
        if not row:
            return
        self.tree.selection_set(row)
        app = row
        self.ctx_menu.delete(0, "end")
        for cat in CATEGORY_COLORS:
            self.ctx_menu.add_command(
                label=cat,
                command=lambda a=app, c=cat: self._set_category(a, c))
        self.ctx_menu.tk_popup(event.x_root, event.y_root)

    def _set_category(self, app, category):
        self.categories[app.lower()] = category
        self._save_categories()
        self.refresh_apps_tree()
        self.draw_today()

    def export_csv(self):
        from tkinter import filedialog
        path = filedialog.asksaveasfilename(
            defaultextension=".csv", filetypes=[("CSV", "*.csv")],
            initialfile="timelens_export.csv")
        if not path:
            return
        apps = self.get_all_apps()
        total = sum(s for _, s in apps) or 1
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["App", "Category", "TotalSeconds", "TotalTime", "Percent"])
                for app, secs in apps:
                    w.writerow([app, self.classify(app), f"{secs:.1f}",
                                self._fmt_duration(secs),
                                f"{secs / total * 100:.1f}%"])
            messagebox.showinfo("Export", f"Exported {len(apps)} apps to:\n{path}")
        except OSError as e:
            messagebox.showerror("Export failed", str(e))

    # ----------------------- controls ----------------------- #
    def toggle_pause(self):
        if self.tracker is None:
            return
        new_paused = not self.tracker.paused
        self.tracker.toggle_pause(new_paused)
        self.pause_btn.configure(text="Resume tracking" if new_paused
                                 else "Pause tracking")

    def set_focus_goal(self):
        val = simpledialog.askfloat("Focus goal", "Daily productive hours goal:",
                                    initialvalue=self.goal_hours, minvalue=0.1,
                                    maxvalue=24.0, parent=self.root)
        if val is not None:
            self.goal_hours = val
            self.goal_already_celebrated_today = False
            self._save_settings()
            self.draw_today()

    def set_distraction_minutes(self):
        val = simpledialog.askinteger("Distraction alert",
                                      "Alert after N minutes of continuous entertainment:",
                                      initialvalue=self.distraction_minutes,
                                      minvalue=1, maxvalue=480, parent=self.root)
        if val is not None:
            self.distraction_minutes = val
            self._save_settings()

    def on_toggle_startup(self):
        enable = self.startup_var.get()
        ok = set_start_with_windows(enable)
        if not ok:
            # Revert the checkbox and warn.
            self.startup_var.set(not enable)
            messagebox.showerror(
                "Start with Windows",
                "Could not update the Windows startup entry.\n"
                "Try running TimeLens as administrator once.")
            return
        self.start_with_windows = enable
        self._save_settings()

    def on_toggle_store_titles(self):
        self.store_titles = not self.no_titles_var.get()
        self._save_settings()

    def confirm_clear_data(self):
        if messagebox.askyesno("Clear all data",
                               "This permanently deletes ALL tracked data "
                               "(both real tracking and demo data).\n\n"
                               "Continue?"):
            self.clear_all_data()
            self.refresh_all()

    def show_about(self):
        messagebox.showinfo(
            "About TimeLens",
            "TimeLens 1.0\n\n"
            "A local screen-time tracker that shows where your time really goes.\n\n"
            "Privacy: 100%% local. All data is stored locally on this machine only "
            "in a sqlite database inside your AppData folder. Nothing is ever sent "
            "over the network.\n\n"
            "Tip: tick \"Don't store window titles\" to save only app names.")

    # ----------------------- demo data ----------------------- #
    def load_demo_data(self):
        if not messagebox.askyesno(
                "Load demo data",
                "Add one week of realistic sample data to the database?\n\n"
                "Demo data is tagged separately from your real tracking data. "
                "Use \"Clear all data\" to remove everything."):
            return
        today = date.today()
        demo_apps = [
            ("code.exe", "Productive"),
            ("chrome.exe", "Other"),
            ("slack.exe", "Communication"),
            ("spotify.exe", "Entertainment"),
            ("winword.exe", "Productive"),
            ("teams.exe", "Communication"),
            ("vlc.exe", "Entertainment"),
            ("pycharm64.exe", "Productive"),
            ("whatsapp.exe", "Communication"),
            ("steam.exe", "Entertainment"),
        ]
        # Weighted minutes per app so charts look realistic.
        weights = [180, 120, 60, 45, 90, 40, 30, 70, 25, 35]
        items = []
        for day_offset in range(6, -1, -1):
            d = today - timedelta(days=day_offset)
            iso = d.isoformat()
            for (app, _cat), mins in zip(demo_apps, weights):
                # Vary a bit per day.
                factor = 0.7 + 0.6 * ((day_offset * 37) % 10) / 10.0
                secs = int(mins * factor * 60)
                items.append(((iso, app, "Demo window"), float(secs)))
        self.db_save(items, source="demo")
        self.refresh_all()
        messagebox.showinfo("Demo data",
                            "Loaded one week of sample data (tagged as demo).")

    # ----------------------- goal / distraction flags ----------------------- #
    def maybe_set_goal_flag(self):
        if self.goal_already_celebrated_today:
            return
        productive = self.get_productive_seconds()
        if productive >= self.goal_hours * 3600:
            self.tracker.goal_reached_flag = True
            self.goal_already_celebrated_today = True

    def _check_flags(self):
        if self.tracker and self.tracker.goal_reached_flag:
            self.tracker.goal_reached_flag = False
            self._flash_title("GOAL REACHED! ")
            messagebox.showinfo("Focus goal reached",
                                f"You hit your daily goal of {self.goal_hours:.1f}h "
                                "of productive time. Nice work!")
        if self.tracker and self.tracker.distraction_flag:
            exe = self.tracker.distraction_flag
            self.tracker.distraction_flag = None
            messagebox.showinfo(
                "Distraction reminder",
                f"You've been using {exe} for {self.distraction_minutes} minutes.\n"
                "Consider a quick break or switching back to focus mode.")

    def _flash_title(self, prefix):
        if self._title_flashing:
            return
        self._title_flashing = True
        original = "TimeLens"
        state = {"on": True, "count": 0}

        def tick():
            if state["count"] >= 12:
                self.root.title(original)
                self._title_flashing = False
                return
            self.root.title(prefix + original if state["on"] else original)
            state["on"] = not state["on"]
            state["count"] += 1
            self.root.after(500, tick)
        tick()

    # ----------------------- refresh loop ----------------------- #
    def _refresh_loop(self):
        self._check_flags()
        self._refresh_active_tab()
        self.root.after(2000, self._refresh_loop)

    def _refresh_active_tab(self):
        try:
            current = self.notebook.index(self.notebook.select())
        except tk.TclError:
            return
        if current == 0:
            self.draw_today()
        elif current == 1:
            self.draw_week()
        elif current == 2:
            self.refresh_apps_tree()

    def _on_tab_changed(self, _event=None):
        self._refresh_active_tab()

    def refresh_all(self):
        self.draw_today()
        self.draw_week()
        self.refresh_apps_tree()

    # ----------------------- shutdown ----------------------- #
    def _on_close(self):
        if self.tracker:
            self.tracker.stop()
            self.tracker.join(timeout=3)
        self.root.destroy()

    # ----------------------- helpers ----------------------- #
    @staticmethod
    def _fmt_duration(seconds, short=False):
        seconds = int(seconds or 0)
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        if short:
            if h > 0:
                return f"{h}h{m}m"
            return f"{m}m"
        if h > 0:
            return f"{h}h {m}m"
        if m > 0:
            return f"{m}m"
        return f"{s}s"

    @staticmethod
    def _short(text, n):
        return text if len(text) <= n else text[: n - 1] + "\u2026"


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main():
    # Distinct AppUserModelID so the Windows taskbar shows our icon, not Python's.
    # Must be set before any window is created.
    set_app_user_model_id("TimeLens.App")

    # Apply the custom icon to every Toplevel/dialog created from here on.
    install_toplevel_icon_hook()

    root = tk.Tk()
    apply_icon(root)
    TimeLensApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()