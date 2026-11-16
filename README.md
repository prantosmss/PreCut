![PreCut Logo](assets/precut_banner.png)

# PreCut - Content Workflow Helper 🎬

A modern, dark-themed desktop application designed to streamline video content creation workflows. **PreCut** provides a centralized interface for generating transcripts, downloading high-quality clips, and managing low-resolution proxies with real-time feedback and smart concurrency management.

> **PreCut v2.2.0** — Added the **Walkthrough Downloader** for IGN videos and wiki walkthroughs, with a built-in IGN stream resolver that replaces yt-dlp's retired IGN extractor. Also includes per-page settings for Clips/Proxy/OST/Transcript and the portable `data/` folder layout.

---

## ✨ Features

- **📺 Transcript Generator**: Automatically fetch and clean transcripts from online video sources. Strips WebVTT formatting, timestamps, and redundant tags. Export as **Plain Text** or **Markdown**.
- **📥 Clips Downloader**: Download high-quality video clips with inline quality and codec selectors. Features a custom inline terminal log for real-time `yt-dlp` transparency.
- **🎮 Walkthrough Downloader**: Paste any IGN video or wiki page link and pick a walkthrough video from the inline selector. Resolves the real 1080p HLS stream directly from IGN, bypassing yt-dlp's retired IGN extractor. Files are saved to your project's `Walkthrough/` folder.
- **⚡ Proxy Downloader**: Create lightweight proxies with inline quality selector for faster editing timelines. Downloads the best available H.264 stream, then converts it to **ProRes 422** (`.mov`) via FFmpeg. Files are saved directly to `C:/Proxies`.
- **🎵 OST Downloader**: Extract audio soundtracks from videos. Choose between **MP3, FLAC, and AAC** formats with selectable bitrates (128k–320k). FLAC auto-disables bitrate. Files saved to your project's `OST/` folder.
- **🎵 Unified Audio Sample Rate (44.1 kHz)**: Clips and Proxies use a hybrid strategy that prefers native `44.1 kHz` tracks (`asr=44100`) and only falls back to `ffmpeg` audio resampling if a source resolves to another rate.
- **🎬 Codec Selector**: Choose between H.264 (Compatible), AV1 (Efficient), and VP9 (Highest Quality). Defaults to H.264 for perfect compatibility with Adobe Premiere Pro and DaVinci Resolve.
- **⚡ Optional aria2c Downloads**: Toggle **Use aria2c for direct downloads** in Settings to route HTTP/HTTPS downloads through the bundled `aria2c.exe` (16 connections/segments). HLS/DASH streams (e.g. IGN walkthroughs) always stay on yt-dlp's native downloader. aria2 is GPLv2 — see [THIRD_PARTY.md](THIRD_PARTY.md) for the license and source link.
- **🍪 Toggleable Cookies & EJS Support**: Handle bot-check-gated videos using either a standard cookie file or the new **EJS (External JavaScript)** solver. EJS uses local runtimes to solve challenges on-the-fly, significantly improving bypass reliability.
- **🧠 Smart Runtime Detection**: Built-in system check automatically detects Node.js, Deno, Bun, or QuickJS. The app prevents enabling EJS if no runtime is found and filters the selector to only show installed options, ensuring a foolproof setup.
- **🧠 JS Runtime Integration**: Choose between multiple runtimes in Settings to power the EJS challenge solver. Automatically pulls the latest solver scripts from GitHub to ensure long-term stability.
- **🧠 Native yt-dlp Python API Runtime**: Transcript and download workflows now run through the `yt_dlp` package API, enabling cleaner progress hooks, structured error handling, and more reliable cancellation behavior.
- **⚙️ Persistent Settings & Power User Controls**: Configure global download quality and project paths. Full `yt-dlp` options are now configured directly through the Python API.
- **🏗️ Smart Concurrency**: Built-in protection allows up to 2 concurrent downloads per page with duplicate URL detection to prevent resource waste.
- **🧹 Clean Workspace**: Automated `__pycache__` relocation and temporary file cleanup to keep your project source code pristine.

---
## 📸 Screenshots

| Page | View |
|:---:|:---:|
| ![Home Screen](docs/screenshots/home_screen.png) | ![Settings Page](docs/screenshots/settings.png) |
| **Project Setup** | **Global Configuration** |
| ![Transcript Generator](docs/screenshots/transcript_generator.png) | ![Concurrent Downloads](docs/screenshots/concurrent_download.png) |
| **Transcript Generation** | **Concurrent Download** |
| ![Clip Downloader](docs/screenshots/clips_downloader.png) | ![Clip Success](docs/screenshots/clips_downloader_success.png) |
| **Clip Downloader (Active)** | **Clip Downloader (Completion)** |
| ![Proxy Downloader](docs/screenshots/proxy_downloader.png) | ![Proxy Success](docs/screenshots/proxy_downloader_success.png) |
| **Proxy Downloader (Active)** | **Proxy Downloader (Completion)** |

---

## 🛠️ Tech Stack

- **UI Framework**: Modernized Python `Tkinter` (Custom Dark Theme)
- **Image Processing**: `Pillow`
- **Backend Engine**: `yt_dlp` (Python Package)
- **Storage**: Persistent JSON-based configuration

---

## 📦 Release & Standalone Version

For users who want to run **PreCut** without installing Python or any libraries, a standalone Windows version is available.

1.  **Download** the latest `PreCut` folder from the [Releases](https://github.com/ssspranto/PreCut/releases) page.
2.  **Extract the ZIP** file to a location of your choice.
3.  **Run `PreCut.exe`** directly from the folder.

> [!NOTE]
> As an unsigned binary, Windows Defender may flag the executable on first run. You can safely click "More info" -> "Run anyway" to start the application.

---

## 🚀 Getting Started

### Prerequisites

- **Python 3.10+**
- **yt_dlp Python Package**: Automatically installed via `pip install -r requirements.txt`. No standalone `yt-dlp` executable in PATH is required anymore!
- **FFmpeg**: Required for merging high-quality video/audio streams.
- **aria2c** (optional): bundled at `assets/bin/aria2c.exe`; no separate install is needed. `python build.py` embeds it in the portable executable via PyInstaller's `--add-binary`.

> [!IMPORTANT]
> **FFmpeg must be installed and added to your system environment variables (PATH)**. Without it, high-quality downloads and format merging will fail.

### Installation

1. **Clone the repository**:
   ```bash
   git clone https://github.com/ssspranto/PreCut.git
   cd PreCut
   ```

2. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

3. **Run the application**:
   ```bash
   python src/main.py
   ```

---

## 📂 Project Structure

```text
PreCut/
├── assets/                 # UI Icons and Brand Assets
│   └── bin/aria2c.exe      # Bundled aria2 downloader (GPLv2, see THIRD_PARTY.md)
├── src/
│   ├── main.py             # Entry point and sidebar navigation
│   ├── ui_page.py          # Page base class and shared utilities
│   ├── ui_theme.py         # Colors and fonts
│   ├── components.py       # DownloadCard and DownloadingPanel widgets
│   ├── config.py           # AppConfig and format templates
│   ├── utils.py            # Asset paths, dependencies, hover effects
│   ├── page_home.py        # Project folder selection
│   ├── page_transcript.py  # Transcript Generator
│   ├── page_clips.py       # Clips Downloader
│   ├── page_walkthrough.py # Walkthrough Downloader (IGN)
│   ├── ign_dl.py           # IGN page/stream resolver
│   ├── page_proxy.py       # Proxy Downloader
│   ├── page_ost.py         # OST Audio Downloader
│   └── page_settings.py    # Cookies and EJS settings
├── requirements.txt        # External dependencies
└── README.md
```

---

## 📝 Configuration

PreCut stores its data outside the source folder to ensure a portable and clean development environment:
- **Settings Path**: `~/Documents/PreCut/data/settings.json`
- **Cache Path**: `~/Documents/PreCut/data/pycache/`
- **Cookie Flags in Settings**:
  - `use_cookies` (default: `false`)
  - `cookie_file` (default: empty)
- **Downloader Settings**:
  - `use_aria2c` (default: `false`) — use the bundled `assets/bin/aria2c.exe` for direct HTTP/HTTPS downloads

---

## 🤝 Contributing

Contributions are welcome! Feel free to open an issue or submit a pull request for any UI improvements or backend feature additions.

## 📄 License

Distributed under the MIT License. See `LICENSE` for more information.

---

## ⚖️ Legal Disclaimer

### Fair Use Notice
This application is provided for **educational and personal research purposes only**. It is designed to facilitate content creation workflows by allowing users to process and manage video assets they have the legal right to access.

**PreCut** does not encourage or facilitate the unauthorized distribution of copyrighted material. Users are solely responsible for ensuring their use of this tool complies with local laws and the terms of service of any content platforms involved. The developers of PreCut assume no liability for misuse of this software.
