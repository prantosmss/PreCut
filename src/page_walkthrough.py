import os
import pathlib
import threading
import tkinter as tk
from tkinter import messagebox

import customtkinter as ctk

from components import DownloadCard
from config import app_config
from ign_dl import (
    IgnExtractionError,
    is_ign_url,
    normalize_ign_url,
    scan_ign_videos,
)
from ui_page import Page
from ui_theme import COLORS, FONTS

# IGN serves H.264/AAC HLS renditions between 360p and 1080p.
WALKTHROUGH_QUALITY_OPTIONS = {
    "Up to 1080p": 1080,
    "Up to 720p": 720,
    "Up to 480p": 480,
    "Up to 360p": 360,
}
DEFAULT_WALKTHROUGH_QUALITY = "Up to 1080p"


class WalkthroughDownloader(Page):
    def __init__(self, master, **kw):
        super().__init__(master, **kw)
        self.create_frame_content().pack(fill=tk.BOTH, expand=True)

    def create_frame_content(self) -> ctk.CTkFrame:
        self.frame_content = ctk.CTkFrame(self, fg_color="transparent")

        ctk.CTkLabel(
            self.frame_content,
            text="Enter IGN Video/Walkthrough URL:",
            font=FONTS["header"],
            text_color='white'
        ).pack(anchor="w", padx=30, pady=(40, 5))

        self.url_var = tk.StringVar(value="")
        self.url_var.trace_add("write", self._on_url_changed)

        self.url_box = ctk.CTkEntry(
            self.frame_content,
            textvariable=self.url_var,
            fg_color='#2B2B2B',
            text_color='white',
            border_color='#3E3E42',
            border_width=1,
            font=FONTS["body"],
            placeholder_text="https://www.ign.com/wikis/... or https://www.ign.com/videos/..."
        )
        self.url_box.pack(fill=tk.X, padx=30, pady=(0, 12), ipady=8)
        self.url_box.bind("<Return>", lambda event: self._start_scan())

        options_frame = ctk.CTkFrame(self.frame_content, fg_color="transparent")
        options_frame.pack(fill=tk.X, anchor="w", padx=30, pady=(0, 8))

        ctk.CTkLabel(options_frame, text="Video:", font=FONTS["body_bold"], text_color='white').pack(side=tk.LEFT, padx=(0, 10))

        self.video_var = tk.StringVar(value="")
        self.video_cb = ctk.CTkComboBox(
            options_frame,
            variable=self.video_var,
            values=[],
            state="disabled",
            font=FONTS["body"],
            width=430
        )
        self.video_cb.pack(side=tk.LEFT, padx=(0, 30))

        ctk.CTkLabel(options_frame, text="Quality:", font=FONTS["body_bold"], text_color='white').pack(side=tk.LEFT, padx=(0, 10))

        stored_quality = app_config.get("walkthrough_quality")
        if stored_quality not in WALKTHROUGH_QUALITY_OPTIONS:
            stored_quality = DEFAULT_WALKTHROUGH_QUALITY
        self.quality_var = tk.StringVar(value=stored_quality)
        self.quality_cb = ctk.CTkComboBox(
            options_frame,
            variable=self.quality_var,
            values=list(WALKTHROUGH_QUALITY_OPTIONS.keys()),
            state="readonly",
            font=FONTS["body"],
            width=150
        )
        self.quality_cb.pack(side=tk.LEFT)

        self.status_label = ctk.CTkLabel(
            self.frame_content,
            text="Paste an IGN link to load its videos.",
            font=FONTS["small"],
            text_color=COLORS["text_dim"],
            anchor="w"
        )
        self.status_label.pack(fill=tk.X, padx=30, pady=(0, 12))

        buttons_frame = ctk.CTkFrame(self.frame_content, fg_color="transparent")
        buttons_frame.pack(pady=(0, 20))

        self.download_button = ctk.CTkButton(
            buttons_frame,
            text="Download Walkthrough",
            font=FONTS["body_bold"],
            fg_color=COLORS["accent_crimson"],
            hover_color=COLORS["accent_glow"],
            corner_radius=8,
            command=self.download
        )
        self.download_button.pack(side=tk.LEFT, padx=(0, 15), ipady=8, ipadx=30)

        self.show_dir_button = ctk.CTkButton(
            buttons_frame,
            text="Show Downloads",
            font=FONTS["body_bold"],
            fg_color='#3E3E42',
            hover_color='#4A4A4F',
            corner_radius=8,
            command=self.open_downloads_folder
        )
        self.show_dir_button.pack(side=tk.LEFT, padx=(0, 15), ipady=8, ipadx=20)

        self.scan_button = ctk.CTkButton(
            buttons_frame,
            text="Scan Videos",
            font=FONTS["body_bold"],
            fg_color="transparent",
            hover_color='#3E3E42',
            border_width=1,
            border_color='#3E3E42',
            corner_radius=8,
            command=self._force_scan
        )
        self.scan_button.pack(side=tk.LEFT, ipady=8, ipadx=20)

        self.panels_frame = ctk.CTkScrollableFrame(
            self.frame_content,
            fg_color="transparent",
            label_text="Active Walkthroughs",
            label_font=FONTS["small"]
        )
        self.panels_frame.pack(fill=tk.BOTH, expand=True, padx=30, pady=(0, 10))

        self.active_downloads = {}

        self._videos = []
        self._video_labels = []
        self._scanned_url = None
        self._scan_token = 0
        self._scan_timer = None
        self._pending_download = False

        return self.frame_content

    # ------------------------------------------------------------------ scan

    def _cancel_scan_timer(self):
        if self._scan_timer is not None:
            try:
                self.after_cancel(self._scan_timer)
            except Exception:
                pass
            self._scan_timer = None

    def _reset_scan_state(self):
        self._videos = []
        self._video_labels = []
        self._scanned_url = None
        self.video_var.set("")
        self.video_cb.configure(values=[], state="disabled")
        self.scan_button.configure(state="normal")
        self.download_button.configure(state="normal")

    def _on_url_changed(self, *args):
        # Invalidate any in-flight scan so stale results are ignored.
        self._scan_token += 1
        self._pending_download = False
        self._cancel_scan_timer()
        self._reset_scan_state()

        url = normalize_ign_url(self.url_var.get())
        if is_ign_url(url):
            self._set_status("Loading videos from IGN…")
            self._scan_timer = self.after(900, self._start_scan)
        else:
            self._set_status("Paste an IGN link to load its videos.")

    def _set_status(self, text, error=False):
        self.status_label.configure(
            text=text,
            text_color=COLORS["accent_crimson"] if error else COLORS["text_dim"]
        )

    def _force_scan(self):
        self._scanned_url = None
        self._videos = []
        self._start_scan()

    def _start_scan(self):
        self._cancel_scan_timer()

        url = normalize_ign_url(self.url_var.get())
        if not is_ign_url(url):
            self._set_status("Please enter a valid IGN link (ign.com).", error=True)
            return

        if self._scanned_url == url and self._videos:
            if self._pending_download:
                self._pending_download = False
                self._start_download()
            return

        self._scan_token += 1
        token = self._scan_token

        self._scanned_url = None
        self._videos = []
        self._video_labels = []
        self.video_var.set("")
        self.video_cb.configure(values=[], state="disabled")
        self.scan_button.configure(state="disabled")
        self.download_button.configure(state="disabled")
        self._set_status("Scanning IGN page…")

        threading.Thread(target=self._scan_worker, args=(url, token), daemon=True).start()

    def _scan_worker(self, url, token):
        try:
            videos = scan_ign_videos(url)
            error = None
        except IgnExtractionError as exc:
            videos, error = [], exc
        except Exception as exc:
            videos, error = [], exc

        self.after(0, lambda: self._on_scan_done(url, token, videos, error))

    def _on_scan_done(self, url, token, videos, error):
        if token != self._scan_token:
            return

        self.scan_button.configure(state="normal")
        self.download_button.configure(state="normal")

        if error is not None or not videos:
            self._videos = []
            self._video_labels = []
            self._scanned_url = None
            self.video_var.set("")
            self.video_cb.configure(values=[], state="disabled")
            message = str(error) if error is not None else "No video found on this IGN page."
            self._set_status(message, error=True)
            self._pending_download = False
            return

        self._scanned_url = url
        self._videos = videos

        labels = []
        counts = {}
        for video in videos:
            title = (video.get("title") or "Untitled IGN video").strip()
            if title in counts:
                counts[title] += 1
                title = f"{title} ({counts[title]})"
            else:
                counts[title] = 1
            labels.append(title)

        self._video_labels = labels
        self.video_cb.configure(values=labels, state="readonly")
        self.video_var.set(labels[0])

        if len(videos) == 1:
            self._set_status("1 video found.")
        else:
            self._set_status(f"{len(videos)} videos found — pick one, then click Download.")

        if self._pending_download:
            self._pending_download = False
            if len(videos) == 1:
                self._start_download()

    def _selected_video(self):
        if not self._videos:
            return None
        label = self.video_var.get()
        try:
            index = self._video_labels.index(label)
        except ValueError:
            index = 0
        return self._videos[index]

    # -------------------------------------------------------------- download

    def download(self):
        if not Page.project_location:
            messagebox.showerror("Error", "Please select a Project Folder location in the Home menu first.")
            return

        url = normalize_ign_url(self.url_var.get())
        if not is_ign_url(url):
            messagebox.showerror('Invalid Link', "Please enter a valid IGN video or wiki URL (ign.com).")
            return

        if len(self.active_downloads) >= 2:
            messagebox.showwarning('Limit Reached', "You can only run a maximum of 2 downloads at a time.")
            return

        if self._scanned_url != url or not self._videos:
            self._pending_download = True
            self._start_scan()
            return

        self._start_download()

    def _start_download(self):
        if not Page.project_location:
            messagebox.showerror("Error", "Please select a Project Folder location in the Home menu first.")
            return

        entry = self._selected_video()
        if not entry or not entry.get("m3u"):
            messagebox.showerror("Error", "No downloadable video is selected. Scan the page again.")
            return

        video_key = entry.get("video_url") or entry["m3u"]

        if video_key in self.active_downloads.values():
            messagebox.showwarning('Duplicate Download', "This video is already being downloaded!")
            return

        if len(self.active_downloads) >= 2:
            messagebox.showwarning('Limit Reached', "You can only run a maximum of 2 downloads at a time.")
            return

        quality_key = self.quality_var.get()
        if quality_key not in WALKTHROUGH_QUALITY_OPTIONS:
            quality_key = DEFAULT_WALKTHROUGH_QUALITY
        max_height = WALKTHROUGH_QUALITY_OPTIONS[quality_key]
        app_config.set("walkthrough_quality", quality_key)

        source_url = self._scanned_url or normalize_ign_url(self.url_var.get())
        output_path = str(pathlib.Path(Page.project_location) / "Walkthrough" / "%(title)s.%(ext)s")
        ydl_opts = {
            "format": f"bestvideo[height<={max_height}]+bestaudio/best[height<={max_height}]",
            "outtmpl": output_path,
            "merge_output_format": "mp4",
            "quiet": True,
            "no_warnings": False,
            "no_color": True,
        }

        resolved = {
            "url": entry["m3u"],
            "info": {
                "id": entry.get("id") or "",
                "title": entry.get("title") or "",
                "thumbnail": entry.get("thumbnail") or "",
            },
        }

        panel = DownloadCard(
            self.panels_frame,
            source_url,
            ydl_opts,
            on_finish_callback=self.on_download_complete,
            url_resolver=lambda _url, result=resolved: result
        )
        panel.pack(fill=tk.X, padx=15, pady=(0, 10))
        self.active_downloads[panel] = video_key

    def on_download_complete(self, panel):
        if panel in self.active_downloads:
            del self.active_downloads[panel]

    def open_downloads_folder(self):
        if not Page.project_location:
            messagebox.showerror("Error", "Please select a Project Folder location in the Home menu first.")
            return

        target_dir = pathlib.Path(Page.project_location) / "Walkthrough"
        target_dir.mkdir(parents=True, exist_ok=True)
        os.startfile(str(target_dir))
