import customtkinter as ctk
import tkinter as tk
from tkinter import messagebox
import threading
import subprocess
import os
import re
import tempfile
import time
import uuid

import yt_dlp
import requests
from io import BytesIO
from PIL import Image

from config import app_config
from utils import get_aria2c_path
from ui_theme import COLORS, FONTS


# aria2c flags requested for multi-connection direct downloads.
ARIA2C_ARGS = ["-x16", "-s16", "-k1M", "--file-allocation=none"]
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class DownloadCancelled(Exception):
    pass


class DownloadPaused(Exception):
    pass


def _new_aria2c_marker():
    """Unique per-download log path, used to find the aria2c child process."""
    return os.path.join(tempfile.gettempdir(), f"precut-aria2c-{uuid.uuid4().hex}.log")


def apply_aria2c_options(runtime_opts, marker):
    """Use the bundled aria2c for direct HTTP(S) downloads when the setting is on.

    The protocol-keyed mapping intentionally omits m3u8/dash so HLS/DASH stay on
    yt-dlp's native downloaders (aria2c cannot handle those manifests).
    Returns True when the external downloader was configured.
    """
    aria2c_path = get_aria2c_path()
    if not aria2c_path or not bool(app_config.get("use_aria2c")):
        return False

    runtime_opts["external_downloader"] = {"http": aria2c_path, "https": aria2c_path}
    runtime_opts["external_downloader_args"] = {"aria2c": [
        *ARIA2C_ARGS,
        # The marker also lets us find/kill this exact aria2c process on cancel.
        f"--log={marker}",
        "--log-level=warn",
        # Never leave an orphaned aria2c behind if PreCut exits unexpectedly.
        f"--stop-with-process={os.getpid()}",
    ]}
    return True


def _aria2c_pids(marker):
    """PIDs of aria2c processes started for this download (matched by the marker)."""
    if os.name != "nt":
        return []
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='aria2c.exe'\" | "
        "Where-Object { $_.CommandLine -and $_.CommandLine.Contains($env:PRECUT_ARIA2C_MARKER) } | "
        "ForEach-Object { $_.ProcessId }"
    )
    env = os.environ.copy()
    env["PRECUT_ARIA2C_MARKER"] = marker
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, env=env, timeout=20,
            creationflags=_NO_WINDOW,
        )
    except Exception:
        return []
    return [int(token) for token in result.stdout.split() if token.isdigit()]


def terminate_aria2c(marker):
    """Terminate the aria2c process tree used by a download (best effort)."""
    if not marker:
        return
    for pid in _aria2c_pids(marker):
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True, timeout=15, creationflags=_NO_WINDOW,
            )
        except Exception:
            pass


def _human_bytes(num):
    num = float(num or 0)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if num < 1024 or unit == "GiB":
            return f"{num:.0f}{unit}" if unit == "B" else f"{num:.1f}{unit}"
        num /= 1024
    return f"{num:.1f}GiB"


def _watch_aria2c_progress(panel, marker, out_dir, prefix, total_bytes, done):
    """Poll aria2c's .part file: external downloads emit no yt-dlp progress hooks."""
    if not out_dir or not prefix:
        return
    last_bytes, last_time = 0, time.monotonic()
    killed = False
    while not done.wait(0.5):
        # Catch cancel/pause clicks that land while yt-dlp is still spawning
        # aria2c (the progress hooks will not fire until it finishes).
        if not killed and (panel.cancel_requested or panel.is_paused):
            killed = True
            terminate_aria2c(marker)
        try:
            downloaded = sum(
                os.path.getsize(os.path.join(out_dir, name))
                for name in os.listdir(out_dir)
                if name.startswith(prefix) and name.endswith(".part")
            )
        except OSError:
            continue
        now = time.monotonic()
        speed = max(downloaded - last_bytes, 0) / max(now - last_time, 0.001)
        last_bytes, last_time = downloaded, now
        percent = (downloaded / total_bytes * 100.0) if total_bytes else 0.0
        size_info = f"{_human_bytes(downloaded)} / " + (
            _human_bytes(total_bytes) if total_bytes else "--"
        )
        try:
            panel.after(
                0,
                lambda p=percent, s=f"{_human_bytes(speed)}/s", si=size_info:
                    panel.update_progress(p, s, si),
            )
        except Exception:
            return


class YdlPanelLogger:
    def __init__(self, panel):
        self.panel = panel

    def debug(self, msg):
        self.panel.queue_log(f"{msg}\n")

    def warning(self, msg):
        self.panel.queue_log(f"WARNING: {msg}\n")

    def error(self, msg):
        self.panel.queue_log(f"ERROR: {msg}\n")


class DownloadCard(ctk.CTkFrame):
    def __init__(self, master, url, ydl_opts, on_finish_callback=None, post_download_callback=None,
                 url_resolver=None, **kw):
        if 'fg_color' not in kw:
            kw['fg_color'] = COLORS["bg_card"]
        super().__init__(master, corner_radius=12, **kw)

        self.url = url
        self.ydl_opts = dict(ydl_opts)
        self.on_finish_callback = on_finish_callback
        self.post_download_callback = post_download_callback
        # Optional callable(url) -> {"url": target, "info": {...}, "filename_base": str}
        # Used by pages whose links need resolving before yt-dlp can handle them.
        self.url_resolver = url_resolver
        self._resolved_target = None
        self._resolved_info = None
        self._fixed_title = None
        self.cancel_requested = False
        self._closed = False
        self.downloaded_files = []
        self.show_logs = False
        self.aria2c_marker = None
        self._aria2c_done = threading.Event()

        self.grid_columnconfigure(1, weight=1)

        self.thumb_label = ctk.CTkLabel(self, text="", font=("Inter", 24), width=100, height=70, fg_color="#2B2B2B", corner_radius=8)
        self.thumb_label.grid(row=0, column=0, rowspan=2, padx=15, pady=15, sticky="n")

        self.info_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.info_frame.grid(row=0, column=1, sticky="nsew", padx=(0, 15), pady=15)

        self.title_label = ctk.CTkLabel(self.info_frame, text="Analyzing video...", font=FONTS["body_bold"], text_color="white", anchor="w")
        self.title_label.pack(fill=tk.X)

        self.progress_bar = ctk.CTkProgressBar(self.info_frame, orientation="horizontal", height=8, progress_color=COLORS["accent_crimson"])
        self.progress_bar.set(0)
        self.progress_bar.pack(fill=tk.X, pady=(10, 5))

        self.status_frame = ctk.CTkFrame(self.info_frame, fg_color="transparent")
        self.status_frame.pack(fill=tk.X)

        self.speed_label = ctk.CTkLabel(self.status_frame, text="Speed: --", font=FONTS["small"], text_color=COLORS["text_dim"])
        self.speed_label.pack(side=tk.LEFT)

        self.size_label = ctk.CTkLabel(self.status_frame, text="-- / --", font=FONTS["small"], text_color=COLORS["text_dim"])
        self.size_label.pack(side=tk.RIGHT)

        self.actions_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.actions_frame.grid(row=0, column=2, rowspan=2, padx=15, pady=15, sticky="ne")

        self.cancel_btn = ctk.CTkButton(self.actions_frame, text="Cancel", width=80, height=30, fg_color="#3E3E42", hover_color=COLORS["accent_crimson"], command=self.stop_download)
        self.cancel_btn.pack(pady=(0, 10))

        self.pause_btn = ctk.CTkButton(self.actions_frame, text="Pause", width=80, height=30, fg_color="transparent", border_width=1, border_color="#3E3E42", command=self.toggle_pause)
        self.pause_btn.pack(pady=(0, 10))

        self.log_btn = ctk.CTkButton(self.actions_frame, text="Logs", width=80, height=30, fg_color="transparent", border_width=1, border_color="#3E3E42", command=self.toggle_logs)
        self.log_btn.pack()

        self.log_box = ctk.CTkTextbox(self, height=0, fg_color="#1E1E1E", text_color="#A0A0A0", font=("Consolas", 11), border_color="#3E3E42", border_width=1)
        self.log_box.grid(row=2, column=0, columnspan=3, padx=15, pady=(0, 15), sticky="nsew")
        self.log_box.grid_remove()

        self.is_paused = False
        self._last_thumb_url = None
        self.update_idletasks()
        self.download_thread = threading.Thread(target=self.start_download, daemon=True)
        self.download_thread.start()

    def toggle_logs(self):
        self.show_logs = not self.show_logs
        if self.show_logs:
            self.log_box.grid()
            self.log_box.configure(height=120)
        else:
            self.log_box.grid_remove()

    def start_download(self):
        try:
            runtime_opts = dict(self.ydl_opts)
            runtime_opts["logger"] = YdlPanelLogger(self)
            runtime_opts["progress_hooks"] = [self.on_progress_update]

            self.aria2c_marker = _new_aria2c_marker()
            if not apply_aria2c_options(runtime_opts, self.aria2c_marker):
                self.aria2c_marker = None

            if self.url_resolver is not None and self._resolved_target is None:
                resolved = self.url_resolver(self.url)
                if self.cancel_requested:
                    raise DownloadCancelled()

                self._resolved_target = resolved.get("url") or self.url
                self._resolved_info = resolved.get("info") or {}
                self._fixed_title = self._resolved_info.get("title") or None
                self._apply_resolved_outtmpl(
                    resolved.get("filename_base") or self._fixed_title, runtime_opts
                )

            target_url = self._resolved_target or self.url
            info = self._resolved_info

            with yt_dlp.YoutubeDL(runtime_opts) as ydl:
                if info is None:
                    info = ydl.extract_info(target_url, download=False)

                is_playlist = info.get('_type') == 'playlist'
                title = self._fixed_title or info.get("title", "Unknown Video")
                if is_playlist and not self._fixed_title:
                    title = f"Playlist: {title}"

                thumb_url = info.get("thumbnail")
                self._last_thumb_url = thumb_url

                self.after(0, lambda t=title: self.title_label.configure(text=t))

                if thumb_url:
                    threading.Thread(target=self.load_thumbnail, args=(thumb_url,), daemon=True).start()

                if self.aria2c_marker:
                    self._start_aria2c_monitor(ydl, info)

                if self.cancel_requested:
                    raise DownloadCancelled()
                if self.is_paused:
                    raise DownloadPaused()

                self._pre_download_files = self._snapshot_output_dir()
                ydl.download([target_url])

            if self.cancel_requested:
                self.queue_log("[download] Cancelled by user.\n")
                self.after(0, self._close_panel)
                return

            self.downloaded_files = self._collect_final_files(self.downloaded_files)
            self.normalize_downloaded_audio()
            if self.post_download_callback:
                self.post_download_callback(self)
            self.after(0, self.on_finish)
        except (DownloadCancelled, DownloadPaused) as e:
            if isinstance(e, DownloadPaused):
                self.queue_log("[download] Paused by user.\n")
                self.after(0, lambda: self.speed_label.configure(text="Status: Paused", text_color=COLORS["accent_crimson"]))
            else:
                self.queue_log("[download] Cancelled by user.\n")
                self.after(0, self._close_panel)
        except yt_dlp.utils.DownloadError as e:
            # Killing aria2c mid-download surfaces as a DownloadError, which is
            # expected when the user cancelled or paused.
            if self.cancel_requested:
                self.queue_log("[download] Cancelled by user.\n")
                self.after(0, self._close_panel)
            elif self.is_paused:
                self.queue_log("[download] Paused by user.\n")
                self.after(0, lambda: self.speed_label.configure(text="Status: Paused", text_color=COLORS["accent_crimson"]))
            else:
                self.after(0, lambda err=e: messagebox.showerror("Download Error", str(err)))
                self.after(0, self._close_panel)
        except Exception as e:
            self.after(0, lambda err=e: messagebox.showerror("Download Error", str(err)))
            self.after(0, self._close_panel)
        finally:
            self._cleanup_aria2c()

    def _start_aria2c_monitor(self, ydl, info):
        protocols = {info.get("protocol") or ""}
        for fmt in info.get("requested_formats") or []:
            protocols.add(fmt.get("protocol") or "")
        if not protocols & {"http", "https"}:
            return

        try:
            filename = ydl.prepare_filename(info)
        except Exception:
            filename = ""
        out_dir = os.path.dirname(filename) or os.path.dirname(self.ydl_opts.get("outtmpl", ""))
        prefix = os.path.splitext(os.path.basename(filename))[0]

        total_bytes = info.get("filesize") or info.get("filesize_approx") or 0
        requested = info.get("requested_formats") or []
        if requested:
            total_bytes = sum(
                (fmt.get("filesize") or fmt.get("filesize_approx") or 0) for fmt in requested
            )

        done = threading.Event()
        self._aria2c_done = done
        threading.Thread(
            target=_watch_aria2c_progress,
            args=(self, self.aria2c_marker, out_dir, prefix, total_bytes, done),
            daemon=True,
        ).start()

    def _terminate_aria2c(self):
        marker = self.aria2c_marker
        if marker:
            threading.Thread(target=terminate_aria2c, args=(marker,), daemon=True).start()

    def _cleanup_aria2c(self):
        self._aria2c_done.set()
        marker = self.aria2c_marker
        self.aria2c_marker = None
        if marker:
            try:
                os.remove(marker)
            except OSError:
                pass

    def load_thumbnail(self, url):
        try:
            headers = {"User-Agent": "Mozilla/5.0"}
            response = requests.get(url, timeout=10, headers=headers)
            img_data = BytesIO(response.content)
            pil_img = Image.open(img_data)

            ctk_img = ctk.CTkImage(light_image=pil_img, dark_image=pil_img, size=(100, 70))

            if self.winfo_exists():
                self.thumbnail_ref = ctk_img
                self.after(0, self._safe_update_thumbnail)
        except Exception as e:
            print(f"Thumbnail load error: {e}")

    def _safe_update_thumbnail(self):
        try:
            if self.winfo_exists() and hasattr(self, 'thumbnail_ref'):
                self.thumb_label.configure(image=self.thumbnail_ref, text="")
        except Exception:
            pass

    def _apply_resolved_outtmpl(self, title, runtime_opts):
        """Name the output file after the resolved title instead of the stream name."""
        if not title:
            return
        try:
            from yt_dlp.utils import sanitize_filename
            base = sanitize_filename(str(title), restricted=False)
        except Exception:
            base = str(title)

        base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", base).strip(" .")
        if len(base) > 120:
            base = base[:120].rstrip(" .")
        if not base:
            return

        # Escape literal percent signs so yt-dlp does not treat them as template fields.
        base = base.replace("%", "%%")

        current = self.ydl_opts.get("outtmpl")
        if not current:
            return

        outtmpl = os.path.join(os.path.dirname(current), f"{base}.%(ext)s")
        self.ydl_opts["outtmpl"] = outtmpl
        runtime_opts["outtmpl"] = outtmpl

    def _snapshot_output_dir(self):
        outtmpl = self.ydl_opts.get("outtmpl", "")
        output_dir = os.path.dirname(outtmpl) if outtmpl else ""
        if not output_dir or not os.path.isdir(output_dir):
            return set()
        try:
            return set(os.listdir(output_dir))
        except Exception:
            return set()

    def _collect_final_files(self, hook_files):
        finals = []
        seen = set()

        for path in hook_files:
            full = os.path.abspath(path)
            if full not in seen and os.path.exists(full):
                seen.add(full)
                finals.append(full)

        outtmpl = self.ydl_opts.get("outtmpl", "")
        output_dir = os.path.dirname(outtmpl) if outtmpl else ""
        if output_dir and os.path.isdir(output_dir):
            try:
                pre = getattr(self, "_pre_download_files", set())
                current = set(os.listdir(output_dir))
                new_names = current - pre
            except Exception:
                new_names = set()

            for name in sorted(new_names):
                if re.search(r"\.part|\.f\d+\.", name):
                    continue
                _, ext = os.path.splitext(name)
                if ext.lower() not in (".mp4", ".mov", ".mkv", ".webm"):
                    continue
                full = os.path.abspath(os.path.join(output_dir, name))
                if full not in seen and os.path.exists(full):
                    seen.add(full)
                    finals.append(full)

        return finals

    def on_finish(self):
        messagebox.showinfo("Download Completed!", "Video files have been successfully downloaded.")
        self.update_idletasks()
        self._close_panel()

    def append_log(self, text):
        if not self.winfo_exists():
            return
        self.log_box.configure(state='normal')
        self.log_box.insert(tk.END, text)
        self.log_box.see(tk.END)
        self.log_box.configure(state='disabled')
        self.update_idletasks()

    def toggle_pause(self):
        if not self.is_paused:
            self.is_paused = True
            self.pause_btn.configure(text="Resume", fg_color=COLORS["accent_crimson"], text_color="white")
            self.queue_log("[download] Pausing...\n")
            self._terminate_aria2c()
        else:
            self.is_paused = False
            self.pause_btn.configure(text="Pause", fg_color="transparent", text_color="white")
            self.queue_log("[download] Resuming...\n")
            self.speed_label.configure(text="Speed: Resuming...", text_color=COLORS["text_dim"])
            self.download_thread = threading.Thread(target=self.start_download, daemon=True)
            self.download_thread.start()

    def stop_download(self):
        self.cancel_requested = True
        self.queue_log("[download] Cancel requested...\n")
        self._terminate_aria2c()
        self._close_panel()

    def _close_panel(self):
        if self._closed:
            return
        self._closed = True
        if self.on_finish_callback:
            self.on_finish_callback(self)
        if self.winfo_exists():
            self.destroy()

    def update_progress(self, percentage, speed, size_info):
        if not self.winfo_exists():
            return
        self.progress_bar.set(percentage / 100)
        self.speed_label.configure(text=f'Speed: {speed}')
        self.size_label.configure(text=size_info)
        self.update_idletasks()

    def queue_log(self, text):
        if self.winfo_exists():
            self.after(0, lambda t=text: self.append_log(t))

    def on_progress_update(self, data):
        if self.cancel_requested:
            raise DownloadCancelled()
        if self.is_paused:
            raise DownloadPaused()

        info_dict = data.get("info_dict", {})
        if info_dict:
            new_title = info_dict.get("title")
            # When a URL resolver supplied the real title, keep it (stream names
            # such as "master" must not overwrite it).
            if new_title and not self._fixed_title and new_title != self.title_label.cget("text"):
                self.after(0, lambda t=new_title: self.title_label.configure(text=t))

            new_thumb = info_dict.get("thumbnail")
            if new_thumb and self._last_thumb_url != new_thumb:
                self._last_thumb_url = new_thumb
                threading.Thread(target=self.load_thumbnail, args=(new_thumb,), daemon=True).start()

        status = data.get("status")
        if status == "downloading":
            percentage = self._percent_to_float(data.get("_percent_str"))
            speed = data.get("_speed_str", "N/A")

            downloaded = data.get("_downloaded_bytes_str", "0B")
            total = data.get("_total_bytes_str") or data.get("_total_bytes_estimate_str", "N/A")
            size_info = f"{downloaded} / {total}"

            self.after(0, lambda p=percentage, s=speed, si=size_info: self.update_progress(p, s, si))
        elif status == "finished":
            final_file = data.get("filename")
            if final_file and final_file not in self.downloaded_files:
                self.downloaded_files.append(final_file)

    def _percent_to_float(self, percent_str):
        if not percent_str:
            return 0.0
        cleaned = percent_str.replace("%", "").strip()
        try:
            return float(cleaned)
        except Exception:
            return 0.0

    def normalize_downloaded_audio(self):
        unique_paths = []
        for path in self.downloaded_files:
            if path not in unique_paths:
                unique_paths.append(path)

        for media_path in unique_paths:
            if not os.path.exists(media_path):
                continue

            sample_rate = self.get_audio_sample_rate(media_path)
            if sample_rate == 44100:
                continue

            base, ext = os.path.splitext(media_path)
            temp_path = f"{base}_srfix{ext}"
            ffmpeg_cmd = [
                "ffmpeg", "-y", "-i", media_path,
                "-map", "0", "-c:v", "copy", "-c:a", "aac", "-ar", "44100",
                temp_path
            ]

            creation_flags = 0
            if os.name == 'nt':
                creation_flags = subprocess.CREATE_NO_WINDOW

            try:
                self.queue_log(f"[post] Resampling audio to 44.1kHz: {os.path.basename(media_path)}\n")
                result = subprocess.run(
                    ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, creationflags=creation_flags
                )
                self.queue_log(result.stdout)

                if result.returncode == 0 and os.path.exists(temp_path):
                    os.replace(temp_path, media_path)
                elif os.path.exists(temp_path):
                    os.remove(temp_path)
            except Exception as e:
                self.queue_log(f"[post] Failed to normalize sample rate: {e}\n")
                if os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                    except Exception:
                        pass

    def get_audio_sample_rate(self, media_path):
        probe_cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=sample_rate",
            "-of", "default=noprint_wrappers=1:nokey=1",
            media_path
        ]

        creation_flags = 0
        if os.name == 'nt':
            creation_flags = subprocess.CREATE_NO_WINDOW

        try:
            result = subprocess.run(
                probe_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, creationflags=creation_flags
            )
            if result.returncode != 0:
                return None
            value = result.stdout.strip()
            return int(value) if value.isdigit() else None
        except Exception:
            return None


class DownloadingPanel(ctk.CTkFrame):
    def __init__(self, master, url, ydl_opts, on_finish_callback=None, **kw):
        if 'fg_color' not in kw:
            kw['fg_color'] = COLORS["bg_card"]
        super().__init__(master, corner_radius=12, **kw)

        self.url = url
        self.ydl_opts = dict(ydl_opts)
        self.on_finish_callback = on_finish_callback
        self.cancel_requested = False
        self._closed = False
        self.downloaded_files = []
        self.show_logs = False

        self.info_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.info_frame.pack(fill=tk.X, padx=15, pady=15)

        self.title_label = ctk.CTkLabel(self.info_frame, text="Analyzing audio...", font=FONTS["body_bold"], text_color="white", anchor="w")
        self.title_label.pack(fill=tk.X)

        self.progress_bar = ctk.CTkProgressBar(self.info_frame, orientation="horizontal", height=8, progress_color=COLORS["accent_crimson"])
        self.progress_bar.set(0)
        self.progress_bar.pack(fill=tk.X, pady=(10, 5))

        self.status_frame = ctk.CTkFrame(self.info_frame, fg_color="transparent")
        self.status_frame.pack(fill=tk.X)

        self.speed_label = ctk.CTkLabel(self.status_frame, text="Speed: --", font=FONTS["small"], text_color=COLORS["text_dim"])
        self.speed_label.pack(side=tk.LEFT)

        self.size_label = ctk.CTkLabel(self.status_frame, text="-- / --", font=FONTS["small"], text_color=COLORS["text_dim"])
        self.size_label.pack(side=tk.RIGHT)

        self.actions_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.actions_frame.pack(side=tk.RIGHT, padx=15, pady=(0, 15))

        self.pause_btn = ctk.CTkButton(self.actions_frame, text="Pause", width=80, height=30, fg_color="transparent", border_width=1, border_color="#3E3E42", command=self.toggle_pause)
        self.pause_btn.pack(side=tk.LEFT, padx=(0, 5))

        self.cancel_btn = ctk.CTkButton(self.actions_frame, text="Cancel", width=80, height=30, fg_color="transparent", border_width=1, border_color="#3E3E42", command=self.stop_download)
        self.cancel_btn.pack(side=tk.LEFT, padx=(0, 5))

        self.log_btn = ctk.CTkButton(self.actions_frame, text="Logs", width=80, height=30, fg_color="transparent", border_width=1, border_color="#3E3E42", command=self.toggle_logs)
        self.log_btn.pack(side=tk.LEFT)

        self.log_box = ctk.CTkTextbox(self, height=0, fg_color="#1E1E1E", text_color="#A0A0A0", font=("Consolas", 11), border_color="#3E3E42", border_width=1)
        self.log_box.pack(fill=tk.X, padx=15, pady=(0, 15))
        self.log_box.pack_forget()

        self.is_paused = False
        self.aria2c_marker = None
        self._aria2c_done = threading.Event()
        self.update_idletasks()
        self.download_thread = threading.Thread(target=self.start_download, daemon=True)
        self.download_thread.start()

    def toggle_logs(self):
        self.show_logs = not self.show_logs
        if self.show_logs:
            self.log_box.pack(fill=tk.X, padx=15, pady=(0, 15))
            self.log_box.configure(height=120)
        else:
            self.log_box.pack_forget()

    def start_download(self):
        try:
            runtime_opts = dict(self.ydl_opts)
            runtime_opts["logger"] = YdlPanelLogger(self)
            runtime_opts["progress_hooks"] = [self.on_progress_update]

            self.aria2c_marker = _new_aria2c_marker()
            if not apply_aria2c_options(runtime_opts, self.aria2c_marker):
                self.aria2c_marker = None

            with yt_dlp.YoutubeDL(runtime_opts) as ydl:
                info = ydl.extract_info(self.url, download=False)

                is_playlist = info.get('_type') == 'playlist'
                title = info.get("title", "Unknown Audio")
                if is_playlist:
                    title = f"Playlist: {title}"

                self.after(0, lambda t=title: self.title_label.configure(text=t))

                if self.aria2c_marker:
                    self._start_aria2c_monitor(ydl, info)

                if self.cancel_requested:
                    raise DownloadCancelled()
                if self.is_paused:
                    raise DownloadPaused()

                ydl.download([self.url])

            if self.cancel_requested:
                self.queue_log("[download] Cancelled by user.\n")
                self.after(0, self._close_panel)
                return

            self.normalize_downloaded_audio()
            self.after(0, self.on_finish)
        except (DownloadCancelled, DownloadPaused) as e:
            if isinstance(e, DownloadPaused):
                self.queue_log("[download] Paused by user.\n")
                self.after(0, lambda: self.speed_label.configure(text="Status: Paused", text_color=COLORS["accent_crimson"]))
            else:
                self.queue_log("[download] Cancelled by user.\n")
                self.after(0, self._close_panel)
        except yt_dlp.utils.DownloadError as e:
            # Killing aria2c mid-download surfaces as a DownloadError, which is
            # expected when the user cancelled or paused.
            if self.cancel_requested:
                self.queue_log("[download] Cancelled by user.\n")
                self.after(0, self._close_panel)
            elif self.is_paused:
                self.queue_log("[download] Paused by user.\n")
                self.after(0, lambda: self.speed_label.configure(text="Status: Paused", text_color=COLORS["accent_crimson"]))
            else:
                self.after(0, lambda err=e: messagebox.showerror("Download Error", str(err)))
                self.after(0, self._close_panel)
        except Exception as e:
            self.after(0, lambda err=e: messagebox.showerror("Download Error", str(err)))
            self.after(0, self._close_panel)
        finally:
            self._cleanup_aria2c()

    def _start_aria2c_monitor(self, ydl, info):
        protocols = {info.get("protocol") or ""}
        for fmt in info.get("requested_formats") or []:
            protocols.add(fmt.get("protocol") or "")
        if not protocols & {"http", "https"}:
            return

        try:
            filename = ydl.prepare_filename(info)
        except Exception:
            filename = ""
        out_dir = os.path.dirname(filename) or os.path.dirname(self.ydl_opts.get("outtmpl", ""))
        prefix = os.path.splitext(os.path.basename(filename))[0]

        total_bytes = info.get("filesize") or info.get("filesize_approx") or 0
        requested = info.get("requested_formats") or []
        if requested:
            total_bytes = sum(
                (fmt.get("filesize") or fmt.get("filesize_approx") or 0) for fmt in requested
            )

        done = threading.Event()
        self._aria2c_done = done
        threading.Thread(
            target=_watch_aria2c_progress,
            args=(self, self.aria2c_marker, out_dir, prefix, total_bytes, done),
            daemon=True,
        ).start()

    def _terminate_aria2c(self):
        marker = self.aria2c_marker
        if marker:
            threading.Thread(target=terminate_aria2c, args=(marker,), daemon=True).start()

    def _cleanup_aria2c(self):
        self._aria2c_done.set()
        marker = self.aria2c_marker
        self.aria2c_marker = None
        if marker:
            try:
                os.remove(marker)
            except OSError:
                pass

    def on_finish(self):
        messagebox.showinfo("Download Completed!", "Audio files downloaded successfully.")
        self.update_idletasks()
        self._close_panel()

    def append_log(self, text):
        if not self.winfo_exists():
            return
        self.log_box.configure(state='normal')
        self.log_box.insert(tk.END, text)
        self.log_box.see(tk.END)
        self.log_box.configure(state='disabled')
        self.update_idletasks()

    def toggle_pause(self):
        if not self.is_paused:
            self.is_paused = True
            self.pause_btn.configure(text="Resume", fg_color=COLORS["accent_crimson"], text_color="white")
            self.queue_log("[download] Pausing...\n")
            self._terminate_aria2c()
        else:
            self.is_paused = False
            self.pause_btn.configure(text="Pause", fg_color="transparent", text_color="white")
            self.queue_log("[download] Resuming...\n")
            self.speed_label.configure(text="Speed: Resuming...", text_color=COLORS["text_dim"])
            self.download_thread = threading.Thread(target=self.start_download, daemon=True)
            self.download_thread.start()

    def stop_download(self):
        self.cancel_requested = True
        self.queue_log("[download] Cancel requested...\n")
        self._terminate_aria2c()
        self._close_panel()

    def _close_panel(self):
        if self._closed:
            return
        self._closed = True
        if self.on_finish_callback:
            self.on_finish_callback(self)
        if self.winfo_exists():
            self.destroy()

    def update_progress(self, percentage, speed, size_info):
        if not self.winfo_exists():
            return
        self.progress_bar.set(percentage / 100)
        self.speed_label.configure(text=f'Speed: {speed}')
        self.size_label.configure(text=size_info)
        self.update_idletasks()

    def queue_log(self, text):
        if self.winfo_exists():
            self.after(0, lambda t=text: self.append_log(t))

    def on_progress_update(self, data):
        if self.cancel_requested:
            raise DownloadCancelled()
        if self.is_paused:
            raise DownloadPaused()

        status = data.get("status")
        if status == "downloading":
            percentage = self._percent_to_float(data.get("_percent_str"))
            speed = data.get("_speed_str", "N/A")

            downloaded = data.get("_downloaded_bytes_str", "0B")
            total = data.get("_total_bytes_str") or data.get("_total_bytes_estimate_str", "N/A")
            size_info = f"{downloaded} / {total}"

            self.after(0, lambda p=percentage, s=speed, si=size_info: self.update_progress(p, s, si))
        elif status == "finished":
            final_file = data.get("filename")
            if final_file and final_file not in self.downloaded_files:
                self.downloaded_files.append(final_file)

    def _percent_to_float(self, percent_str):
        if not percent_str:
            return 0.0
        cleaned = percent_str.replace("%", "").strip()
        try:
            return float(cleaned)
        except Exception:
            return 0.0

    def normalize_downloaded_audio(self):
        unique_paths = []
        for path in self.downloaded_files:
            if path not in unique_paths:
                unique_paths.append(path)

        for media_path in unique_paths:
            if not os.path.exists(media_path):
                continue

            sample_rate = self.get_audio_sample_rate(media_path)
            if sample_rate == 44100:
                continue

            base, ext = os.path.splitext(media_path)
            temp_path = f"{base}_srfix{ext}"
            ffmpeg_cmd = [
                "ffmpeg", "-y", "-i", media_path,
                "-map", "0", "-c:v", "copy", "-c:a", "aac", "-ar", "44100",
                temp_path
            ]

            creation_flags = 0
            if os.name == 'nt':
                creation_flags = subprocess.CREATE_NO_WINDOW

            try:
                self.queue_log(f"[post] Resampling audio to 44.1kHz: {os.path.basename(media_path)}\n")
                result = subprocess.run(
                    ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, creationflags=creation_flags
                )
                self.queue_log(result.stdout)

                if result.returncode == 0 and os.path.exists(temp_path):
                    os.replace(temp_path, media_path)
                elif os.path.exists(temp_path):
                    os.remove(temp_path)
            except Exception as e:
                self.queue_log(f"[post] Failed to normalize sample rate: {e}\n")
                if os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                    except Exception:
                        pass

    def get_audio_sample_rate(self, media_path):
        probe_cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=sample_rate",
            "-of", "default=noprint_wrappers=1:nokey=1",
            media_path
        ]

        creation_flags = 0
        if os.name == 'nt':
            creation_flags = subprocess.CREATE_NO_WINDOW

        try:
            result = subprocess.run(
                probe_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, creationflags=creation_flags
            )
            if result.returncode != 0:
                return None
            value = result.stdout.strip()
            return int(value) if value.isdigit() else None
        except Exception:
            return None
