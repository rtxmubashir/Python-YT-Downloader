# main.py
# YouTube Downloader - Improved Edition

import os
import sys
import socket
import pathlib
import traceback
import multiprocessing as mp
import threading
import time
import json
import re
import subprocess
import platform
from datetime import timedelta
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field

import customtkinter as ctk
import yt_dlp
from tkinter import filedialog, messagebox

# ---------- App configuration ----------
APP_TITLE  = "YouTube Downloader"
APP_VER    = "2.0"
DEFAULT_QUALITIES  = ["Best", "2160", "1440", "1080", "720", "480", "360"]
AUDIO_QUALITIES    = ["Best", "320", "256", "192", "128", "64"]
DEFAULT_AUDIO_FMT  = "mp3"
DEFAULT_OUT_TPL    = "%(title)s.%(ext)s"
SINGLE_INSTANCE_PORT = 53535
RETRY_ATTEMPTS     = 3
RETRY_SLEEP_SEC    = 5

BROWSER_OPTIONS = ["None", "Chrome", "Firefox", "Edge", "Safari", "Chromium", "Brave", "Opera"]

# ---------- Appearance ----------
ctk.set_appearance_mode("System")
ctk.set_default_color_theme("blue")

# ---------- Data Classes ----------
@dataclass
class DownloadProgress:
    status: str = ""
    percent: float = 0.0
    downloaded: int = 0
    total: int = 0
    speed: float = 0.0
    eta: Optional[int] = None
    playlist_index: int = 0
    playlist_count: int = 0
    playlist_title: str = ""
    filename: Optional[str] = None
    error: Optional[str] = None

@dataclass
class QueueItem:
    url: str
    status: str = "pending"   # pending | downloading | done | error | skipped
    title: str = ""
    error: str = ""

@dataclass
class MetaResult:
    title: str = ""
    uploader: str = ""
    duration: int = 0
    view_count: int = 0
    description: str = ""
    thumbnail_url: str = ""
    formats: List[str] = field(default_factory=list)
    error: str = ""

# ---------- Utilities ----------
def human_seconds(seconds: Optional[int]) -> str:
    try:
        if seconds is None or seconds < 0:
            return "--:--"
        return str(timedelta(seconds=int(seconds)))
    except Exception:
        return "--:--"

def human_bytes(num: Optional[float]) -> str:
    try:
        if num is None:
            return "--"
        num = float(num)
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if abs(num) < 1024.0:
                return f"{num:.1f} {unit}"
            num /= 1024.0
        return f"{num:.1f} PB"
    except Exception:
        return str(num)

def safe_makedirs(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path

def open_folder(folder: str):
    """Open a folder in the native file manager, cross-platform."""
    if not os.path.exists(folder):
        messagebox.showwarning("Not found", "Folder does not exist.")
        return
    system = platform.system()
    try:
        if system == "Windows":
            os.startfile(folder)
        elif system == "Darwin":
            subprocess.Popen(["open", folder])
        else:
            subprocess.Popen(["xdg-open", folder])
    except Exception as e:
        messagebox.showerror("Error", f"Could not open folder:\n{e}")

def get_ydl_opts_base(folder: str, browser: str, playlist: bool,
                      playlist_start: int, playlist_end: int,
                      mode: str, quality: str, audio_fmt: str) -> Dict[str, Any]:
    """Build a clean yt-dlp options dict. No hooks/logger – added by caller."""
    opts: Dict[str, Any] = {
        'outtmpl':       os.path.join(folder, DEFAULT_OUT_TPL),
        'quiet':         True,
        'no_warnings':   True,
        'ignoreerrors':  True,
        'noplaylist':    not playlist,
        'retries':       RETRY_ATTEMPTS,
        'fragment_retries': RETRY_ATTEMPTS,
        'extractor_retries': RETRY_ATTEMPTS,
        'sleep_interval_requests': 1,
        'http_chunk_size': 10485760,  # 10 MB
    }

    # Browser cookies
    browser_lower = browser.lower()
    if browser_lower != "none":
        try:
            opts['cookiesfrombrowser'] = (browser_lower,)
        except Exception:
            pass  # silently skip if not supported

    # Playlist range
    if playlist and playlist_start > 1:
        opts['playliststart'] = playlist_start
    if playlist and playlist_end > 0:
        opts['playlistend'] = playlist_end

    # Format selection
    if mode == "Audio":
        opts['format'] = 'bestaudio/best'
        opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': audio_fmt,
            'preferredquality': quality if quality != "Best" else "192",
        }]
    else:
        if quality == "Best":
            opts['format'] = 'bestvideo+bestaudio/best'
        else:
            opts['format'] = (
                f'bestvideo[height<={quality}][ext=mp4]+bestaudio[ext=m4a]/'
                f'bestvideo[height<={quality}]+bestaudio/best'
            )
        opts['merge_output_format'] = 'mp4'

    return opts

# ---------- Worker Process: Download ----------
def download_worker(cmd_opts: Dict[str, Any], url: str,
                    q_progress: mp.Queue, q_log: mp.Queue):

    class ProcLogger:
        def __init__(self, q):
            self.q = q
        def debug(self, msg):
            if msg.startswith('[debug]'):
                return
            self.q.put(("log", f"DEBUG: {msg}"))
        def warning(self, msg):
            self.q.put(("log", f"WARNING: {msg}"))
        def error(self, msg):
            self.q.put(("log", f"ERROR: {msg}"))

    last_percent = [0.0]

    def progress_hook(d):
        try:
            if not d:
                return
            status = d.get('status')
            if status == 'downloading':
                total      = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                downloaded = d.get('downloaded_bytes') or 0
                speed      = d.get('speed') or 0
                eta        = d.get('eta')
                percent    = (downloaded / total * 100.0) if total > 0 else last_percent[0]
                last_percent[0] = percent
                q_progress.put(DownloadProgress(
                    status='downloading',
                    percent=percent,
                    downloaded=downloaded,
                    total=total,
                    speed=speed,
                    eta=eta,
                    playlist_index=d.get('playlist_index') or 0,
                    playlist_count=d.get('playlist_count') or 0,
                    playlist_title=d.get('playlist') or '',
                ))
            elif status == 'finished':
                q_progress.put(DownloadProgress(
                    status='finished',
                    filename=d.get('filename'),
                    playlist_index=d.get('playlist_index') or 0,
                ))
            elif status == 'error':
                q_progress.put(DownloadProgress(
                    status='error',
                    error=str(d.get('error', 'Unknown error')),
                ))
        except Exception as exc:
            q_log.put(("log", f"Progress hook error: {exc}"))

    attempt = 0
    last_error = ""
    while attempt < RETRY_ATTEMPTS:
        attempt += 1
        try:
            opts = dict(cmd_opts)
            opts['progress_hooks'] = [progress_hook]
            opts['logger'] = ProcLogger(q_log)
            q_log.put(("log", f"Attempt {attempt}/{RETRY_ATTEMPTS}: {url}"))
            yt_dlp.YoutubeDL(opts).download([url])
            q_progress.put(DownloadProgress(status='all_done'))
            q_log.put(("log", "Download completed successfully."))
            return
        except yt_dlp.utils.DownloadError as exc:
            last_error = str(exc)
            q_log.put(("log", f"DownloadError: {last_error}"))
            # Diagnose common issues
            if "cookies" in last_error.lower():
                q_log.put(("log",
                    "TIP: Cookie extraction failed. Try a different browser or disable cookies."))
            elif "ffmpeg" in last_error.lower():
                q_log.put(("log",
                    "TIP: FFmpeg not found. Install it and add it to PATH."))
            elif "403" in last_error or "forbidden" in last_error.lower():
                q_log.put(("log",
                    "TIP: Access denied (403). Try enabling browser cookies."))
            elif "geo" in last_error.lower() or "not available" in last_error.lower():
                q_log.put(("log",
                    "TIP: Content may be geo-restricted or private."))
            if attempt < RETRY_ATTEMPTS:
                q_log.put(("log", f"Retrying in {RETRY_SLEEP_SEC}s..."))
                time.sleep(RETRY_SLEEP_SEC)
        except Exception as exc:
            last_error = str(exc)
            tb = traceback.format_exc()
            q_log.put(("log", f"Unexpected error: {exc}\n{tb}"))
            break

    q_progress.put(DownloadProgress(status='error', error=last_error))

# ---------- Worker Process: Fetch Metadata ----------
def meta_worker(url: str, browser: str, q_out: mp.Queue):
    opts: Dict[str, Any] = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        'noplaylist': True,
    }
    browser_lower = browser.lower()
    if browser_lower != "none":
        try:
            opts['cookiesfrombrowser'] = (browser_lower,)
        except Exception:
            pass
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            q_out.put(MetaResult(error="No info returned."))
            return
        fmts = []
        for f in info.get('formats', []):
            h = f.get('height')
            note = f.get('format_note', '')
            ext  = f.get('ext', '')
            if h:
                fmts.append(f"{h}p {ext}")
        fmts = sorted(set(fmts), key=lambda x: int(x.split('p')[0]), reverse=True)
        q_out.put(MetaResult(
            title       = info.get('title', 'Unknown'),
            uploader    = info.get('uploader', 'Unknown'),
            duration    = info.get('duration', 0),
            view_count  = info.get('view_count', 0),
            description = (info.get('description') or '')[:300],
            thumbnail_url = info.get('thumbnail', ''),
            formats     = fmts[:12],
        ))
    except Exception as exc:
        q_out.put(MetaResult(error=str(exc)))

# ============================================================
# Main App
# ============================================================
class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_TITLE} v{APP_VER}")
        self.minsize(920, 680)

        # ---- variables ----
        self.url_var           = ctk.StringVar()
        self.mode_var          = ctk.StringVar(value="Video")
        self.quality_var       = ctk.StringVar(value="Best")
        self.folder_var        = ctk.StringVar(value=str(pathlib.Path.home() / "Downloads"))
        self.audio_fmt_var     = ctk.StringVar(value=DEFAULT_AUDIO_FMT)
        self.playlist_start_var= ctk.StringVar(value="1")
        self.playlist_end_var  = ctk.StringVar(value="0")
        self.playlist_var      = ctk.BooleanVar(value=False)
        self.browser_var       = ctk.StringVar(value="None")

        # ---- state ----
        self.q_progress: mp.Queue = mp.Queue()
        self.q_log:      mp.Queue = mp.Queue()
        self.q_meta:     mp.Queue = mp.Queue()
        self.worker_proc: Optional[mp.Process] = None
        self.meta_proc:   Optional[mp.Process] = None
        self.busy = False
        self.queue_items: List[QueueItem] = []
        self.queue_rows:  List[ctk.CTkFrame] = []

        self._build_ui()
        self.after(100, self._center_window)
        self.after(200, self._process_queues)
        self.protocol("WM_DELETE_WINDOW", self._on_closing)

    # ----------------------------------------------------------
    # Window helpers
    # ----------------------------------------------------------
    def _center_window(self):
        self.update_idletasks()
        w, h = self.winfo_width(), self.winfo_height()
        x = (self.winfo_screenwidth()  // 2) - (w // 2)
        y = (self.winfo_screenheight() // 2) - (h // 2)
        self.geometry(f"{w}x{h}+{x}+{y}")

    def _on_closing(self):
        if self.worker_proc and self.worker_proc.is_alive():
            if messagebox.askyesno("Confirm", "Download in progress. Exit anyway?"):
                self.worker_proc.terminate()
                self.destroy()
        else:
            self.destroy()

    # ----------------------------------------------------------
    # UI builder
    # ----------------------------------------------------------
    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        root = ctk.CTkFrame(self)
        root.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        root.grid_columnconfigure(0, weight=1)
        root.grid_rowconfigure(1, weight=1)

        # ---- top bar ----
        top = ctk.CTkFrame(root, height=50, corner_radius=0)
        top.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ctk.CTkLabel(top, text=f"{APP_TITLE}  v{APP_VER}",
                     font=ctk.CTkFont(size=20, weight="bold")).pack(side="left", padx=20)
        self.status_dot = ctk.CTkFrame(top, width=12, height=12,
                                       corner_radius=6, fg_color="green")
        self.status_dot.place(x=10, y=19)

        # theme selector in top bar
        ctk.CTkOptionMenu(top, values=["System", "Dark", "Light"],
                          width=100, command=ctk.set_appearance_mode).pack(
            side="right", padx=10, pady=8)
        ctk.CTkLabel(top, text="Theme:").pack(side="right")

        # ---- tab view ----
        tabs = ctk.CTkTabview(root)
        tabs.grid(row=1, column=0, sticky="nsew")
        tabs.add("Download")
        tabs.add("Queue")
        tabs.add("Log")

        tabs.tab("Download").grid_columnconfigure(0, weight=1)
        tabs.tab("Download").grid_columnconfigure(1, weight=1)
        tabs.tab("Download").grid_rowconfigure(0, weight=1)
        tabs.tab("Queue").grid_columnconfigure(0, weight=1)
        tabs.tab("Queue").grid_rowconfigure(0, weight=1)
        tabs.tab("Log").grid_columnconfigure(0, weight=1)
        tabs.tab("Log").grid_rowconfigure(0, weight=1)

        self._build_download_tab(tabs.tab("Download"))
        self._build_queue_tab(tabs.tab("Queue"))
        self._build_log_tab(tabs.tab("Log"))

    # ----------------------------------------------------------
    # Download tab
    # ----------------------------------------------------------
    def _build_download_tab(self, parent):
        # LEFT
        left = ctk.CTkFrame(parent)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 5), pady=5)
        left.grid_columnconfigure(0, weight=1)

        # URL row
        url_f = ctk.CTkFrame(left)
        url_f.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 5))
        url_f.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(url_f, text="URL:", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, padx=(10, 5), pady=10)
        ctk.CTkEntry(url_f, textvariable=self.url_var, height=35).grid(
            row=0, column=1, sticky="ew", padx=(0, 5), pady=10)
        ctk.CTkButton(url_f, text="+ Queue", width=70,
                      command=self._add_to_queue).grid(row=0, column=2, padx=(0, 10))

        # Mode
        mode_f = ctk.CTkFrame(left)
        mode_f.grid(row=1, column=0, sticky="ew", padx=10, pady=5)
        ctk.CTkLabel(mode_f, text="Mode:", font=ctk.CTkFont(weight="bold")).pack(
            side="left", padx=10)
        ctk.CTkSegmentedButton(mode_f, values=["Video", "Audio"],
                               variable=self.mode_var,
                               command=self._on_mode_change).pack(side="right", padx=10)

        # Quality
        quality_f = ctk.CTkFrame(left)
        quality_f.grid(row=2, column=0, sticky="ew", padx=10, pady=5)
        ctk.CTkLabel(quality_f, text="Quality:", font=ctk.CTkFont(weight="bold")).pack(
            side="left", padx=10)
        self.quality_menu = ctk.CTkOptionMenu(quality_f, values=DEFAULT_QUALITIES,
                                              variable=self.quality_var)
        self.quality_menu.pack(side="right", padx=10)

        # Audio format (hidden initially)
        self.audio_fmt_f = ctk.CTkFrame(left)
        self.audio_fmt_f.grid(row=3, column=0, sticky="ew", padx=10, pady=5)
        ctk.CTkLabel(self.audio_fmt_f, text="Format:").pack(side="left", padx=10)
        ctk.CTkOptionMenu(self.audio_fmt_f,
                          values=["mp3", "m4a", "opus", "flac", "wav"],
                          variable=self.audio_fmt_var).pack(side="right", padx=10)
        self.audio_fmt_f.grid_remove()

        # Output folder
        folder_f = ctk.CTkFrame(left)
        folder_f.grid(row=4, column=0, sticky="ew", padx=10, pady=5)
        ctk.CTkLabel(folder_f, text="Save to:").pack(side="left", padx=10)
        ctk.CTkEntry(folder_f, textvariable=self.folder_var).pack(
            side="left", fill="x", expand=True, padx=5)
        ctk.CTkButton(folder_f, text="Browse", width=65,
                      command=self._browse_folder).pack(side="right", padx=10)

        # Options
        opt_f = ctk.CTkFrame(left)
        opt_f.grid(row=5, column=0, sticky="ew", padx=10, pady=5)

        ctk.CTkCheckBox(opt_f, text="Download as playlist",
                        variable=self.playlist_var,
                        command=self._toggle_playlist_ui).pack(
            anchor="w", padx=10, pady=(8, 4))

        # Browser cookies selector
        browser_row = ctk.CTkFrame(opt_f, fg_color="transparent")
        browser_row.pack(fill="x", padx=10, pady=(0, 6))
        ctk.CTkLabel(browser_row, text="Browser cookies:").pack(side="left")
        ctk.CTkOptionMenu(browser_row, values=BROWSER_OPTIONS,
                          variable=self.browser_var,
                          width=130).pack(side="right")

        # Playlist range (hidden)
        self.pl_range_f = ctk.CTkFrame(opt_f, fg_color="transparent")
        self.pl_range_f.pack(fill="x", padx=10, pady=(0, 6))
        ctk.CTkLabel(self.pl_range_f, text="Items:").pack(side="left")
        ctk.CTkEntry(self.pl_range_f, textvariable=self.playlist_start_var,
                     width=50).pack(side="left", padx=5)
        ctk.CTkLabel(self.pl_range_f, text="–").pack(side="left")
        ctk.CTkEntry(self.pl_range_f, textvariable=self.playlist_end_var,
                     width=50).pack(side="left", padx=5)
        ctk.CTkLabel(self.pl_range_f, text="(0 = all)").pack(side="left", padx=5)
        self.pl_range_f.pack_forget()

        # Buttons
        btn_f = ctk.CTkFrame(left)
        btn_f.grid(row=6, column=0, sticky="ew", padx=10, pady=10)
        btn_f.grid_columnconfigure(0, weight=1)
        btn_f.grid_columnconfigure(1, weight=1)

        self.start_btn = ctk.CTkButton(btn_f, text="▶  Start Download",
                                       height=40, fg_color="#4CAF50",
                                       command=self._start_download)
        self.start_btn.grid(row=0, column=0, sticky="ew", padx=(0, 4), pady=5)
        self.stop_btn = ctk.CTkButton(btn_f, text="⏹  Stop",
                                      height=40, fg_color="#F44336",
                                      state="disabled",
                                      command=self._stop_download)
        self.stop_btn.grid(row=0, column=1, sticky="ew", padx=(4, 0), pady=5)
        ctk.CTkButton(btn_f, text="📂  Open Folder", height=35,
                      command=lambda: open_folder(self.folder_var.get())).grid(
            row=1, column=0, columnspan=2, sticky="ew", pady=5)

        # RIGHT – progress + metadata
        right = ctk.CTkFrame(parent)
        right.grid(row=0, column=1, sticky="nsew", padx=(5, 0), pady=5)
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(1, weight=1)

        # Progress card
        prog_f = ctk.CTkFrame(right)
        prog_f.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 5))
        ctk.CTkLabel(prog_f, text="Progress",
                     font=ctk.CTkFont(weight="bold")).pack(anchor="w", padx=10, pady=(10, 0))
        self.playlist_lbl = ctk.CTkLabel(prog_f, text="", text_color="#2196F3")
        self.playlist_lbl.pack(anchor="w", padx=10)
        self.progress_var = ctk.DoubleVar(value=0)
        self.progress_bar = ctk.CTkProgressBar(prog_f, variable=self.progress_var, height=15)
        self.progress_bar.pack(fill="x", padx=10, pady=5)
        self.status_lbl = ctk.CTkLabel(prog_f, text="Ready",
                                       font=ctk.CTkFont(weight="bold"))
        self.status_lbl.pack(anchor="w", padx=10)
        self.speed_lbl = ctk.CTkLabel(prog_f, text="")
        self.speed_lbl.pack(anchor="e", padx=10, pady=(0, 10))

        # Metadata card
        meta_f = ctk.CTkFrame(right)
        meta_f.grid(row=1, column=0, sticky="nsew", padx=10, pady=(5, 10))
        meta_f.grid_columnconfigure(0, weight=1)
        meta_f.grid_rowconfigure(1, weight=1)

        meta_hdr = ctk.CTkFrame(meta_f, fg_color="transparent")
        meta_hdr.grid(row=0, column=0, sticky="ew")
        ctk.CTkLabel(meta_hdr, text="Metadata preview",
                     font=ctk.CTkFont(weight="bold")).pack(side="left", padx=10, pady=8)
        self.fetch_btn = ctk.CTkButton(meta_hdr, text="Fetch info", width=90,
                                       command=self._fetch_meta)
        self.fetch_btn.pack(side="right", padx=10)

        self.meta_box = ctk.CTkTextbox(meta_f, wrap="word",
                                       font=("Consolas", 10), state="disabled")
        self.meta_box.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))
        self._meta_write("Enter a URL and press 'Fetch info' to preview video details.")

    # ----------------------------------------------------------
    # Queue tab
    # ----------------------------------------------------------
    def _build_queue_tab(self, parent):
        top = ctk.CTkFrame(parent, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", pady=(8, 4))
        ctk.CTkLabel(top, text="Download queue",
                     font=ctk.CTkFont(weight="bold", size=14)).pack(side="left", padx=10)
        ctk.CTkButton(top, text="Clear done", width=90,
                      command=self._clear_done_queue).pack(side="right", padx=10)
        ctk.CTkButton(top, text="Clear all", width=80,
                      command=self._clear_all_queue).pack(side="right", padx=5)
        ctk.CTkButton(top, text="▶ Run Queue", fg_color="#4CAF50", width=100,
                      command=self._run_queue).pack(side="right", padx=5)

        scroll = ctk.CTkScrollableFrame(parent)
        scroll.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))
        scroll.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(1, weight=1)
        self.queue_scroll = scroll
        self.queue_list_frame = scroll

    # ----------------------------------------------------------
    # Log tab
    # ----------------------------------------------------------
    def _build_log_tab(self, parent):
        hdr = ctk.CTkFrame(parent, fg_color="transparent")
        hdr.grid(row=0, column=0, sticky="ew")
        ctk.CTkLabel(hdr, text="Activity log",
                     font=ctk.CTkFont(weight="bold", size=14)).pack(
            side="left", padx=10, pady=8)
        ctk.CTkButton(hdr, text="Clear", width=60,
                      command=self._clear_log).pack(side="right", padx=10)
        ctk.CTkButton(hdr, text="Copy all", width=80,
                      command=self._copy_log).pack(side="right")

        self.log_text = ctk.CTkTextbox(parent, wrap="word",
                                       font=("Consolas", 10))
        self.log_text.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))
        parent.grid_rowconfigure(1, weight=1)

    # ----------------------------------------------------------
    # UI helpers
    # ----------------------------------------------------------
    def _browse_folder(self):
        folder = filedialog.askdirectory(initialdir=self.folder_var.get())
        if folder:
            self.folder_var.set(folder)

    def _on_mode_change(self, value):
        if value == "Audio":
            self.quality_menu.configure(values=AUDIO_QUALITIES)
            self.quality_var.set("Best")
            self.audio_fmt_f.grid()
        else:
            self.quality_menu.configure(values=DEFAULT_QUALITIES)
            self.quality_var.set("Best")
            self.audio_fmt_f.grid_remove()

    def _toggle_playlist_ui(self):
        if self.playlist_var.get():
            self.pl_range_f.pack(fill="x", padx=10, pady=(0, 6))
        else:
            self.pl_range_f.pack_forget()

    def _set_busy(self, busy: bool):
        self.busy = busy
        state = "disabled" if busy else "normal"
        self.start_btn.configure(state=state)
        self.stop_btn.configure(state="normal" if busy else "disabled")
        self.fetch_btn.configure(state=state)
        self.status_dot.configure(fg_color="orange" if busy else "green")

    # ----------------------------------------------------------
    # Queue management
    # ----------------------------------------------------------
    def _add_to_queue(self):
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("Missing URL", "Enter a URL first.")
            return
        # Basic URL check
        if not re.match(r'https?://', url):
            messagebox.showwarning("Invalid URL", "URL must start with http:// or https://")
            return
        item = QueueItem(url=url)
        self.queue_items.append(item)
        self._render_queue_row(item)
        self.url_var.set("")
        self.log(f"Queued: {url}")

    def _render_queue_row(self, item: QueueItem):
        idx = len(self.queue_rows)
        row = ctk.CTkFrame(self.queue_list_frame)
        row.grid(row=idx, column=0, sticky="ew", pady=3)
        row.grid_columnconfigure(1, weight=1)

        color_map = {
            "pending":     "#888888",
            "downloading": "#FF9800",
            "done":        "#4CAF50",
            "error":       "#F44336",
            "skipped":     "#9C27B0",
        }
        dot = ctk.CTkFrame(row, width=10, height=10, corner_radius=5,
                           fg_color=color_map.get(item.status, "#888"))
        dot.grid(row=0, column=0, padx=(10, 5), pady=8)

        lbl = ctk.CTkLabel(row, text=item.url[:80] + ("…" if len(item.url) > 80 else ""),
                           anchor="w")
        lbl.grid(row=0, column=1, sticky="ew", padx=5)

        status_lbl = ctk.CTkLabel(row, text=item.status.upper(),
                                  width=90, text_color=color_map.get(item.status, "#888"))
        status_lbl.grid(row=0, column=2, padx=5)

        def remove():
            item.status = "skipped"
            self.queue_items.remove(item)
            row.grid_remove()
            self.queue_rows.remove(row_ref)

        ctk.CTkButton(row, text="✕", width=30, fg_color="transparent",
                      command=remove).grid(row=0, column=3, padx=(0, 5))

        row_ref = [row, dot, status_lbl]
        self.queue_rows.append(row_ref)

    def _clear_done_queue(self):
        remaining_items = []
        for item, row_ref in zip(self.queue_items, self.queue_rows):
            if item.status in ("done", "skipped"):
                row_ref[0].grid_remove()
            else:
                remaining_items.append((item, row_ref))
        self.queue_items  = [x[0] for x in remaining_items]
        self.queue_rows   = [x[1] for x in remaining_items]

    def _clear_all_queue(self):
        for row_ref in self.queue_rows:
            row_ref[0].grid_remove()
        self.queue_items.clear()
        self.queue_rows.clear()

    def _run_queue(self):
        pending = [i for i in self.queue_items if i.status == "pending"]
        if not pending:
            messagebox.showinfo("Queue empty", "No pending items in the queue.")
            return
        if self.busy:
            messagebox.showinfo("Busy", "A download is already running.")
            return
        # Download them sequentially by kicking off the first one;
        # completion triggers the next via _on_queue_item_done.
        self._queue_run_next(pending)

    def _queue_run_next(self, remaining: List[QueueItem]):
        if not remaining:
            self.log("Queue finished.")
            messagebox.showinfo("Queue", "All queued downloads finished!")
            return
        item = remaining[0]
        item.status = "downloading"
        self._refresh_queue_row(item)
        self.log(f"Queue: starting {item.url}")
        opts = self._build_opts()
        self._set_busy(True)
        self.progress_var.set(0)
        self.status_lbl.configure(text="Starting...")

        # store callback for completion
        self._queue_remaining = remaining[1:]
        self._queue_current   = item

        self.worker_proc = mp.Process(
            target=download_worker,
            args=(opts, item.url, self.q_progress, self.q_log),
            daemon=True)
        self.worker_proc.start()

    def _refresh_queue_row(self, item: QueueItem):
        color_map = {
            "pending":     "#888888",
            "downloading": "#FF9800",
            "done":        "#4CAF50",
            "error":       "#F44336",
            "skipped":     "#9C27B0",
        }
        try:
            idx = self.queue_items.index(item)
            row_ref = self.queue_rows[idx]
            col = color_map.get(item.status, "#888")
            row_ref[1].configure(fg_color=col)
            row_ref[2].configure(text=item.status.upper(), text_color=col)
        except (ValueError, IndexError):
            pass

    def _on_queue_item_done(self, success: bool, error: str = ""):
        item = getattr(self, '_queue_current', None)
        if item:
            item.status = "done" if success else "error"
            item.error  = error
            self._refresh_queue_row(item)
        remaining = getattr(self, '_queue_remaining', [])
        self._set_busy(False)
        if remaining:
            self.after(500, lambda: self._queue_run_next(remaining))

    # ----------------------------------------------------------
    # Metadata fetch
    # ----------------------------------------------------------
    def _fetch_meta(self):
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("Missing URL", "Enter a URL first.")
            return
        if self.meta_proc and self.meta_proc.is_alive():
            return
        self._meta_write("Fetching info…")
        self.fetch_btn.configure(state="disabled")
        self.meta_proc = mp.Process(
            target=meta_worker,
            args=(url, self.browser_var.get(), self.q_meta),
            daemon=True)
        self.meta_proc.start()

    def _meta_write(self, text: str):
        self.meta_box.configure(state="normal")
        self.meta_box.delete("1.0", "end")
        self.meta_box.insert("end", text)
        self.meta_box.configure(state="disabled")

    def _show_meta(self, m: MetaResult):
        if m.error:
            self._meta_write(f"Error fetching info:\n{m.error}\n\nTip: try enabling browser cookies.")
            return
        lines = [
            f"Title:     {m.title}",
            f"Uploader:  {m.uploader}",
            f"Duration:  {human_seconds(m.duration)}",
            f"Views:     {m.view_count:,}" if m.view_count else "",
            "",
            "Available formats:",
        ]
        lines += [f"  • {f}" for f in m.formats] if m.formats else ["  (none found)"]
        if m.description:
            lines += ["", "Description:", m.description.strip()[:300]]
        self._meta_write("\n".join(l for l in lines))

    # ----------------------------------------------------------
    # Build yt-dlp options from current UI state
    # ----------------------------------------------------------
    def _build_opts(self) -> Dict[str, Any]:
        playlist = self.playlist_var.get()
        try:
            p_start = int(self.playlist_start_var.get())
        except ValueError:
            p_start = 1
        try:
            p_end = int(self.playlist_end_var.get())
        except ValueError:
            p_end = 0

        return get_ydl_opts_base(
            folder        = self.folder_var.get(),
            browser       = self.browser_var.get(),
            playlist      = playlist,
            playlist_start= p_start,
            playlist_end  = p_end,
            mode          = self.mode_var.get(),
            quality       = self.quality_var.get(),
            audio_fmt     = self.audio_fmt_var.get(),
        )

    # ----------------------------------------------------------
    # Download control
    # ----------------------------------------------------------
    def _start_download(self):
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("Missing URL", "Please enter a URL.")
            return
        if not re.match(r'https?://', url):
            messagebox.showwarning("Invalid URL", "URL must start with http:// or https://")
            return
        folder = self.folder_var.get()
        if not folder:
            messagebox.showwarning("Missing folder", "Select an output folder.")
            return
        safe_makedirs(folder)

        if self.busy:
            messagebox.showinfo("Busy", "A download is already running.")
            return

        self.progress_var.set(0)
        self.playlist_lbl.configure(text="")
        self.status_lbl.configure(text="Starting…")
        self.speed_lbl.configure(text="")
        self._clear_log()
        self._set_busy(True)

        self._queue_current   = None
        self._queue_remaining = []

        opts = self._build_opts()
        self.log(f"URL:     {url}")
        self.log(f"Mode:    {self.mode_var.get()} | Quality: {self.quality_var.get()}")
        self.log(f"Browser: {self.browser_var.get()}")
        self.log(f"Folder:  {folder}")
        self.log("-" * 48)

        self.worker_proc = mp.Process(
            target=download_worker,
            args=(opts, url, self.q_progress, self.q_log),
            daemon=True)
        self.worker_proc.start()

    def _stop_download(self):
        if self.worker_proc and self.worker_proc.is_alive():
            self.worker_proc.terminate()
            self.log("Download stopped by user.")
            self._set_busy(False)
            self.status_lbl.configure(text="Stopped")

    # ----------------------------------------------------------
    # Queue polling
    # ----------------------------------------------------------
    def _process_queues(self):
        # Logs
        while not self.q_log.empty():
            try:
                _, msg = self.q_log.get_nowait()
                self._append_log(msg)
            except Exception:
                break

        # Metadata
        while not self.q_meta.empty():
            try:
                result = self.q_meta.get_nowait()
                if isinstance(result, MetaResult):
                    self._show_meta(result)
                self.fetch_btn.configure(state="normal")
            except Exception:
                break

        # Progress
        while not self.q_progress.empty():
            try:
                info = self.q_progress.get_nowait()
                if not isinstance(info, DownloadProgress):
                    continue
                s = info.status

                if s == 'downloading':
                    # Guard against stale 0% when total unknown
                    pct = info.percent
                    if pct > 0 or info.total > 0:
                        self.progress_var.set(pct / 100.0)
                    txt = f"{pct:.1f}%"
                    if info.total:
                        txt += f"  {human_bytes(info.downloaded)} / {human_bytes(info.total)}"
                    if info.speed:
                        self.speed_lbl.configure(text=f"{human_bytes(info.speed)}/s")
                    if info.eta is not None:
                        txt += f"  ETA {human_seconds(info.eta)}"
                    self.status_lbl.configure(text=txt)
                    if info.playlist_count > 0:
                        self.playlist_lbl.configure(
                            text=f"Playlist: {info.playlist_title}  ({info.playlist_index}/{info.playlist_count})")

                elif s == 'finished':
                    fname = os.path.basename(info.filename or '')
                    self.log(f"✓ Finished: {fname}")

                elif s == 'all_done':
                    self.progress_var.set(1.0)
                    self.status_lbl.configure(text="✓ Complete")
                    self.speed_lbl.configure(text="")
                    self.playlist_lbl.configure(text="")
                    self._set_busy(False)
                    self.log("All downloads completed.")
                    # If running queue, advance to next
                    if getattr(self, '_queue_current', None):
                        self._on_queue_item_done(True)
                    else:
                        messagebox.showinfo("Success", "Download finished!")

                elif s == 'error':
                    self.status_lbl.configure(text=f"✗ Error")
                    self.log(f"ERROR: {info.error}")
                    self._set_busy(False)
                    if getattr(self, '_queue_current', None):
                        self._on_queue_item_done(False, info.error or "")
                    else:
                        messagebox.showerror("Download failed",
                            f"{info.error}\n\nCheck the Log tab for details and tips.")
            except Exception:
                break

        self.after(150, self._process_queues)

    # ----------------------------------------------------------
    # Log helpers
    # ----------------------------------------------------------
    def _append_log(self, text: str):
        ts = time.strftime("%H:%M:%S")
        self.log_text.insert("end", f"[{ts}] {text}\n")
        self.log_text.see("end")

    def log(self, msg: str):
        self._append_log(str(msg))

    def _clear_log(self):
        self.log_text.delete("1.0", "end")

    def _copy_log(self):
        content = self.log_text.get("1.0", "end")
        self.clipboard_clear()
        self.clipboard_append(content)
        self.log("Log copied to clipboard.")

# ---------- Single Instance ----------
def ensure_single_instance(port: int = SINGLE_INSTANCE_PORT):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", port))
        s.listen(1)
        return s
    except OSError:
        return None

# ---------- Entry point ----------
if __name__ == "__main__":
    sock = ensure_single_instance()
    if not sock:
        messagebox.showinfo("Already Running",
                            "YouTube Downloader is already open.")
        sys.exit(0)

    mp.set_start_method("spawn", force=True)
    app = App()
    app.geometry("960x720")
    app.mainloop()
    sock.close()